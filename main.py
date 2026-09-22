"""
Traffic-Guard - Virtual Waiting Room Middleware
===========================================================
Production-ready FastAPI backend for traffic surge protection.

Features:
  - Variable virtual waiting room with automatic bypass/queue switching
  - Active user TTL-based expiry management
  - Downstream load-aware dynamic TPS throttling
  - Real-time admin dashboard with WebSocket streaming
  - Real-time client waiting room with WebSocket streaming
  - JWT-based queue passage verification tokens
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jwt as pyjwt
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Path Configuration & File Loader
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"


def load_template(filename: str) -> str:
    """Load HTML template file from the templates directory."""
    file_path = TEMPLATES_DIR / filename
    if not file_path.exists():
        logger.error("Template file not found: %s", file_path)
        return f"<h1>500 - Template Error: {filename} not found</h1>"
    return file_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Traffic-Guard")

# ---------------------------------------------------------------------------
# Redis Keys
# ---------------------------------------------------------------------------
KEY_WAITING = "queue:waiting"  # ZSET  (score=timestamp, member=user_id)
KEY_ACTIVE = "queue:active"  # SET   (member=user_id)
KEY_ACTIVE_EXPIRY = "queue:active:expiry"  # ZSET  (score=expiry_epoch, member=user_id)
KEY_CONFIG = "queue:config"  # HASH
KEY_DOWNSTREAM = "queue:metrics:downstream"  # HASH
KEY_PASS_COUNT = "queue:metrics:pass_count"  # STRING (counter)
KEY_BLACKLIST = "queue:blacklist"  # SET

# ---------------------------------------------------------------------------
# Default Config Values
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "bypass_threshold": "500",
    "active_ttl_sec": "300",
    "base_batch_size": "50",
    "current_batch_size": "50",
    "polling_interval": "1",
    "token_ttl_sec": "60",
    "is_paused": "false",
    "auto_throttling_enabled": "true",
}

# ---------------------------------------------------------------------------
# Global Redis client
# ---------------------------------------------------------------------------
redis_client: aioredis.Redis = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Pydantic Models (v2)
# ---------------------------------------------------------------------------
class DownstreamMetrics(BaseModel):
    server_id: str
    cpu_usage: float = Field(ge=0, le=100)
    active_connections: int = Field(ge=0)
    http_5xx_rate: float = Field(ge=0, le=1)


class ExpireTokenRequest(BaseModel):
    user_id: str


class AdminConfig(BaseModel):
    bypass_threshold: int | None = None
    active_ttl_sec: int | None = None
    base_batch_size: int | None = None
    polling_interval: int | None = None
    is_paused: bool | None = None
    token_ttl_sec: int | None = None
    auto_throttling_enabled: bool | None = None


class BlacklistRequest(BaseModel):
    user_id: str | None = None
    ip: str | None = None


# ---------------------------------------------------------------------------
# Helper: JWT Token
# ---------------------------------------------------------------------------
async def _issue_jwt(user_id: str) -> str:
    """Issue a signed JWT token for an authorised user."""
    r = redis_client
    token_ttl = int(await r.hget(KEY_CONFIG, "token_ttl_sec") or 60)
    now = int(time.time())
    payload = {
        "user_id": user_id,
        "iat": now,
        "exp": now + token_ttl,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Helper: Get config values
# ---------------------------------------------------------------------------
async def _cfg(field: str) -> str:
    val = await redis_client.hget(KEY_CONFIG, field)
    if val is None:
        return DEFAULT_CONFIG.get(field, "0")
    return val if isinstance(val, str) else val.decode()


async def _cfg_int(field: str) -> int:
    return int(await _cfg(field))


async def _cfg_bool(field: str) -> bool:
    return (await _cfg(field)).lower() == "true"


# ---------------------------------------------------------------------------
# Helper: Ensure Key Type Integrity
# ---------------------------------------------------------------------------
async def _ensure_key_type(key: str, expected_type: str):
    """WrongType 오류 방지를 위해 Redis 키의 타입을 검증하고 다를 경우 초기화합니다."""
    try:
        current_type = await redis_client.type(key)
        if current_type not in (expected_type, "none"):
            logger.warning(
                "Key type mismatch for '%s': expected %s, found %s. Deleting key.",
                key,
                expected_type,
                current_type,
            )
            await redis_client.delete(key)
    except Exception as e:
        logger.error("Error validating key type for %s: %s", key, e)


# ---------------------------------------------------------------------------
# Background Worker
# ---------------------------------------------------------------------------
async def _worker_loop():
    """Background worker: expires active users & admits waiting users."""
    r = redis_client
    logger.info("Background worker started")
    while True:
        try:
            polling_interval = await _cfg_int("polling_interval")
            await asyncio.sleep(max(polling_interval, 1))

            now = time.time()

            # --- 1. Active User Expiry Reclaim ---
            expired = await r.zrangebyscore(KEY_ACTIVE_EXPIRY, "-inf", now)
            if expired:
                pipe = r.pipeline()
                for uid in expired:
                    uid_str = uid if isinstance(uid, str) else uid.decode()
                    pipe.srem(KEY_ACTIVE, uid_str)
                    pipe.zrem(KEY_ACTIVE_EXPIRY, uid_str)
                await pipe.execute()
                logger.info("Expired %d active user(s)", len(expired))

            # --- 2. Dynamic TPS Throttling ---
            auto_throttling = await _cfg_bool("auto_throttling_enabled")
            if auto_throttling:
                cpu_raw = await r.hget(KEY_DOWNSTREAM, "cpu_usage")
                if cpu_raw is not None:
                    cpu = float(cpu_raw)
                    base = await _cfg_int("base_batch_size")
                    if cpu >= 90:
                        new_batch = max(1, int(base * 0.1))
                    elif cpu >= 80:
                        new_batch = max(1, int(base * 0.5))
                    elif cpu < 70:
                        new_batch = base
                    else:
                        new_batch = await _cfg_int("current_batch_size")
                    await r.hset(KEY_CONFIG, "current_batch_size", str(new_batch))

            # --- 3. Admit Waiting Users ---
            is_paused = await _cfg_bool("is_paused")
            if not is_paused:
                batch_size = await _cfg_int("current_batch_size")
                active_ttl = await _cfg_int("active_ttl_sec")
                if batch_size > 0:
                    admitted = await r.zpopmin(KEY_WAITING, batch_size)
                    if admitted:
                        now_ts = time.time()
                        pipe = r.pipeline()
                        for member, _score in admitted:
                            uid_str = member if isinstance(member, str) else member.decode()
                            pipe.sadd(KEY_ACTIVE, uid_str)
                            pipe.zadd(KEY_ACTIVE_EXPIRY, {uid_str: now_ts + active_ttl})
                        pipe.incrby(KEY_PASS_COUNT, len(admitted))
                        await pipe.execute()
                        logger.debug("Admitted %d user(s) from waiting queue", len(admitted))

        except asyncio.CancelledError:
            logger.info("Background worker cancelled")
            break
        except Exception:
            logger.exception("Worker loop error")
            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )
    logger.info("Connected to Redis at %s:%s", REDIS_HOST, REDIS_PORT)

    # 데이터 타입 검증 및 잘못된 데이터 타입 초기화
    await _ensure_key_type(KEY_WAITING, "zset")
    await _ensure_key_type(KEY_ACTIVE, "set")
    await _ensure_key_type(KEY_ACTIVE_EXPIRY, "zset")

    existing = await redis_client.hgetall(KEY_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        if k not in existing:
            await redis_client.hset(KEY_CONFIG, k, v)
    logger.info("Config initialised: %s", await redis_client.hgetall(KEY_CONFIG))

    worker_task = asyncio.create_task(_worker_loop())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await redis_client.aclose()
    logger.info("Redis connection closed")


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Traffic-Guard - Virtual Waiting Room",
    version="1.0.0",
    lifespan=lifespan,
)


# ========================== A. Public API ==================================


@app.get("/queue/status")
async def queue_status(user_id: str = Query(..., min_length=1)):
    """REST endpoint for checking queue status."""
    r = redis_client

    if await r.sismember(KEY_BLACKLIST, user_id):
        return JSONResponse(
            status_code=403,
            content={"detail": "Access denied: user is blacklisted"},
        )

    if await r.sismember(KEY_ACTIVE, user_id):
        token = await _issue_jwt(user_id)
        return {"status": "ALLOWED", "token": token}

    bypass_threshold = await _cfg_int("bypass_threshold")
    active_count = await r.scard(KEY_ACTIVE)
    waiting_count = await r.zcard(KEY_WAITING)

    if active_count < bypass_threshold and waiting_count == 0:
        active_ttl = await _cfg_int("active_ttl_sec")
        now = time.time()
        pipe = r.pipeline()
        pipe.sadd(KEY_ACTIVE, user_id)
        pipe.zadd(KEY_ACTIVE_EXPIRY, {user_id: now + active_ttl})
        pipe.incr(KEY_PASS_COUNT)
        await pipe.execute()
        token = await _issue_jwt(user_id)
        return {"status": "ALLOWED", "token": token}

    rank = await r.zrank(KEY_WAITING, user_id)
    if rank is None:
        await r.zadd(KEY_WAITING, {user_id: time.time()})
        rank = await r.zrank(KEY_WAITING, user_id)

    current_batch = await _cfg_int("current_batch_size")
    polling_interval = await _cfg_int("polling_interval")
    estimated_seconds = 0.0
    if current_batch > 0 and rank is not None:
        estimated_seconds = round(((rank + 1) / current_batch) * polling_interval, 1)

    return {
        "status": "WAITING",
        "rank": (rank or 0) + 1,
        "estimated_seconds": estimated_seconds,
    }


# =================== B. Client Queue WebSocket ==============================


@app.websocket("/ws/queue/status")
async def ws_queue_status(websocket: WebSocket, user_id: str = Query(..., min_length=1)):
    """Stream real-time queue status to individual user clients via WebSocket."""
    await websocket.accept()
    r = redis_client

    try:
        if await r.sismember(KEY_BLACKLIST, user_id):
            await websocket.send_json(
                {"status": "DENIED", "detail": "Access denied: user is blacklisted"}
            )
            await websocket.close(code=1008)
            return

        if not await r.sismember(KEY_ACTIVE, user_id):
            rank = await r.zrank(KEY_WAITING, user_id)
            if rank is None:
                await r.zadd(KEY_WAITING, {user_id: time.time()})

        while True:
            if await r.sismember(KEY_ACTIVE, user_id):
                token = await _issue_jwt(user_id)
                await websocket.send_json({"status": "ALLOWED", "token": token})
                await websocket.close()
                break

            bypass_threshold = await _cfg_int("bypass_threshold")
            active_count = await r.scard(KEY_ACTIVE)
            waiting_count = await r.zcard(KEY_WAITING)

            if active_count < bypass_threshold and waiting_count <= 1:
                active_ttl = await _cfg_int("active_ttl_sec")
                now = time.time()
                pipe = r.pipeline()
                # ZSET 명령어인 zrem을 확실하게 적용하여 파이프라인 오류 방지
                pipe.zrem(KEY_WAITING, user_id)
                pipe.sadd(KEY_ACTIVE, user_id)
                pipe.zadd(KEY_ACTIVE_EXPIRY, {user_id: now + active_ttl})
                pipe.incr(KEY_PASS_COUNT)
                await pipe.execute()

                token = await _issue_jwt(user_id)
                await websocket.send_json({"status": "ALLOWED", "token": token})
                await websocket.close()
                break

            rank = await r.zrank(KEY_WAITING, user_id)

            if rank is None:
                await asyncio.sleep(0.5)
                continue

            current_batch = await _cfg_int("current_batch_size")
            polling_interval = await _cfg_int("polling_interval")

            estimated_seconds = 0.0
            if current_batch > 0:
                estimated_seconds = round(((rank + 1) / current_batch) * polling_interval, 1)

            await websocket.send_json(
                {"status": "WAITING", "rank": rank + 1, "estimated_seconds": estimated_seconds}
            )

            await asyncio.sleep(polling_interval)

    except WebSocketDisconnect:
        logger.info("Client WebSocket disconnected for user: %s", user_id)
    except Exception:
        logger.exception("WebSocket error for user: %s", user_id)


# =================== C. Downstream Feedback API ===========================


@app.post("/downstream/metrics")
async def receive_downstream_metrics(body: DownstreamMetrics):
    """Receive health metrics from downstream servers."""
    r = redis_client
    await r.hset(
        KEY_DOWNSTREAM,
        mapping={
            "server_id": body.server_id,
            "cpu_usage": str(body.cpu_usage),
            "active_connections": str(body.active_connections),
            "http_5xx_rate": str(body.http_5xx_rate),
        },
    )
    return {"status": "ok"}


@app.post("/queue/expire-token")
async def expire_token(body: ExpireTokenRequest):
    """Explicitly release an active user's slot."""
    r = redis_client
    pipe = r.pipeline()
    pipe.srem(KEY_ACTIVE, body.user_id)
    pipe.zrem(KEY_ACTIVE_EXPIRY, body.user_id)
    await pipe.execute()
    logger.info("Explicitly expired token for user %s", body.user_id)
    return {"status": "ok", "user_id": body.user_id}


# ======================== D. Admin API =====================================


@app.post("/admin/config")
async def update_config(body: AdminConfig):
    """Update queue runtime configuration."""
    r = redis_client
    mapping = {}
    if body.bypass_threshold is not None:
        mapping["bypass_threshold"] = str(body.bypass_threshold)
    if body.active_ttl_sec is not None:
        mapping["active_ttl_sec"] = str(body.active_ttl_sec)
    if body.base_batch_size is not None:
        mapping["base_batch_size"] = str(body.base_batch_size)
        mapping["current_batch_size"] = str(body.base_batch_size)
    if body.polling_interval is not None:
        mapping["polling_interval"] = str(body.polling_interval)
    if body.is_paused is not None:
        mapping["is_paused"] = str(body.is_paused).lower()
    if body.token_ttl_sec is not None:
        mapping["token_ttl_sec"] = str(body.token_ttl_sec)
    if body.auto_throttling_enabled is not None:
        mapping["auto_throttling_enabled"] = str(body.auto_throttling_enabled).lower()
    if mapping:
        await r.hset(KEY_CONFIG, mapping=mapping)
    return {"status": "ok", "updated": mapping}


@app.post("/admin/queue/flush")
async def flush_queue():
    """Immediately clear all queue data structures."""
    r = redis_client
    pipe = r.pipeline()
    pipe.delete(KEY_WAITING)
    pipe.delete(KEY_ACTIVE)
    pipe.delete(KEY_ACTIVE_EXPIRY)
    pipe.set(KEY_PASS_COUNT, 0)
    await pipe.execute()
    logger.warning("All queues flushed by admin")
    return {"status": "ok", "message": "All queues flushed"}


@app.post("/admin/blacklist")
async def add_blacklist(body: BlacklistRequest):
    """Add a user_id or IP to the blacklist."""
    r = redis_client
    added = []
    if body.user_id:
        await r.sadd(KEY_BLACKLIST, body.user_id)
        added.append(f"user:{body.user_id}")
    if body.ip:
        await r.sadd(KEY_BLACKLIST, body.ip)
        added.append(f"ip:{body.ip}")
    return {"status": "ok", "blacklisted": added}


# ===================== E. Admin WebSocket Metrics ==========================


@app.websocket("/ws/admin/metrics")
async def ws_admin_metrics(websocket: WebSocket):
    """Stream real-time queue metrics to admin dashboard."""
    await websocket.accept()
    r = redis_client
    try:
        while True:
            waiting = await r.zcard(KEY_WAITING)
            active = await r.scard(KEY_ACTIVE)
            bypass_threshold = await _cfg_int("bypass_threshold")
            current_batch = await _cfg_int("current_batch_size")
            base_batch = await _cfg_int("base_batch_size")
            is_paused = await _cfg_bool("is_paused")
            auto_throttling = await _cfg_bool("auto_throttling_enabled")
            active_ttl = await _cfg_int("active_ttl_sec")

            cpu_raw = await r.hget(KEY_DOWNSTREAM, "cpu_usage")
            cpu_usage = float(cpu_raw) if cpu_raw else 0.0
            conns_raw = await r.hget(KEY_DOWNSTREAM, "active_connections")
            active_connections = int(conns_raw) if conns_raw else 0
            err_raw = await r.hget(KEY_DOWNSTREAM, "http_5xx_rate")
            http_5xx_rate = float(err_raw) if err_raw else 0.0

            pass_count = await r.get(KEY_PASS_COUNT) or "0"

            mode = "BYPASS" if active < bypass_threshold else "ACTIVE_QUEUE"

            await websocket.send_json(
                {
                    "waiting_count": waiting,
                    "active_count": active,
                    "mode": mode,
                    "bypass_threshold": bypass_threshold,
                    "current_tps": current_batch,
                    "base_batch_size": base_batch,
                    "active_ttl_sec": active_ttl,
                    "is_paused": is_paused,
                    "auto_throttling_enabled": auto_throttling,
                    "cpu_usage": cpu_usage,
                    "active_connections": active_connections,
                    "http_5xx_rate": http_5xx_rate,
                    "total_passed": int(pass_count),
                    "timestamp": time.time(),
                }
            )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("Admin WebSocket disconnected")
    except Exception:
        logger.exception("WebSocket error")


# ===================== F. UI Endpoints (HTML Page Serving) ================


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard():
    """Serve the admin real-time dashboard UI."""
    return HTMLResponse(content=load_template("dashboard.html"))


@app.get("/queue/page", response_class=HTMLResponse)
async def queue_page():
    """Serve the user-facing queue waiting page UI."""
    return HTMLResponse(content=load_template("queue.html"))


# ===================== G. Main Execution Entrypoint =======================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
