"""
Traffic-Guard - Virtual Waiting Room Middleware
===========================================================
Production-ready FastAPI backend for traffic surge protection.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jwt as pyjwt
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Path Configuration & File Loader
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Traffic-Guard")


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
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "5000"))

JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"

HEARTBEAT_TTL_SEC = 30  # Redis TTL for heartbeat session (30 seconds)

# ---------------------------------------------------------------------------
# Redis Keys
# ---------------------------------------------------------------------------
KEY_WAITING = "queue:waiting"  # ZSET  (score=timestamp, member=user_id)
KEY_ACTIVE = "queue:active"  # SET   (member=user_id)
KEY_ACTIVE_EXPIRY = "queue:active:expiry"  # ZSET  (score=expiry_epoch, member=user_id)
KEY_HEARTBEAT_PREFIX = "queue:heartbeat:"  # STRING (val=status, EX=30s)
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

redis_client: aioredis.Redis = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------
class DownstreamMetrics(BaseModel):
    server_id: str
    cpu_usage: float = Field(ge=0, le=100)
    active_connections: int = Field(ge=0)
    http_5xx_rate: float = Field(ge=0, le=1)


class ExpireTokenRequest(BaseModel):
    user_id: str


class HeartbeatRequest(BaseModel):
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
# Helpers
# ---------------------------------------------------------------------------
async def _issue_jwt(user_id: str) -> str:
    r = redis_client
    token_ttl = int(await r.hget(KEY_CONFIG, "token_ttl_sec") or 60)
    now = int(time.time())
    payload = {
        "user_id": user_id,
        "iat": now,
        "exp": now + token_ttl,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def _cfg(field: str) -> str:
    val = await redis_client.hget(KEY_CONFIG, field)
    if val is None:
        return DEFAULT_CONFIG.get(field, "0")
    return val if isinstance(val, str) else val.decode()


async def _cfg_int(field: str) -> int:
    return int(await _cfg(field))


async def _cfg_bool(field: str) -> bool:
    return (await _cfg(field)).lower() == "true"


async def _ensure_key_type(key: str, expected_type: str):
    try:
        current_type = await redis_client.type(key)
        if isinstance(current_type, bytes):
            current_type = current_type.decode()
        if current_type not in (expected_type, "none"):
            logger.warning(
                "⚠️ Key type mismatch for '%s': expected %s, found %s. Re-creating.",
                key,
                expected_type,
                current_type,
            )
            await redis_client.delete(key)
    except Exception as e:
        logger.error("❌ Error validating key type for %s: %s", key, e)


async def _shift_to_active(user_id: str, active_ttl: int) -> str:
    r = redis_client
    now = time.time()
    pipe = r.pipeline()
    pipe.zrem(KEY_WAITING, user_id)
    pipe.sadd(KEY_ACTIVE, user_id)
    pipe.zadd(KEY_ACTIVE_EXPIRY, {user_id: now + active_ttl})
    pipe.set(f"{KEY_HEARTBEAT_PREFIX}{user_id}", "active", ex=HEARTBEAT_TTL_SEC)
    pipe.incr(KEY_PASS_COUNT)
    await pipe.execute()

    logger.info("🟢 [State Shift] User '%s' shifted to ACTIVE (TTL: %ds)", user_id, active_ttl)
    return await _issue_jwt(user_id)


async def _cleanup_user_session(user_id: str):
    r = redis_client
    pipe = r.pipeline()
    pipe.zrem(KEY_WAITING, user_id)
    pipe.srem(KEY_ACTIVE, user_id)
    pipe.zrem(KEY_ACTIVE_EXPIRY, user_id)
    pipe.delete(f"{KEY_HEARTBEAT_PREFIX}{user_id}")
    await pipe.execute()
    logger.info("🗑 [Explicit Cleanup] Removed user '%s' session completely", user_id)


# ---------------------------------------------------------------------------
# Background Worker with Detailed Event Logs (정합성 보장)
# ---------------------------------------------------------------------------
async def _worker_loop():
    r = redis_client
    logger.info("🚀 Background worker started")
    while True:
        try:
            polling_interval = await _cfg_int("polling_interval")
            await asyncio.sleep(max(polling_interval, 1))
            now = time.time()

            # 1. Active User Expiry Reclaim
            expired = await r.zrangebyscore(KEY_ACTIVE_EXPIRY, "-inf", now)
            if expired:
                pipe = r.pipeline()
                for uid in expired:
                    uid_str = uid if isinstance(uid, str) else uid.decode()
                    pipe.srem(KEY_ACTIVE, uid_str)
                    pipe.zrem(KEY_ACTIVE_EXPIRY, uid_str)
                    pipe.delete(f"{KEY_HEARTBEAT_PREFIX}{uid_str}")
                await pipe.execute()
                logger.info(
                    "⌛ [Worker: Expiry Reclaim] Expired %d user(s) (Sample: %s)",
                    len(expired),
                    expired[:3],
                )

            # 2. Ghost Session Cleanup using ZSCAN
            cursor = 0
            ghost_users = []
            while True:
                cursor, items = await r.zscan(KEY_WAITING, cursor=cursor, count=100)
                if items:
                    pipe = r.pipeline()
                    for uid, _ in items:
                        pipe.exists(f"{KEY_HEARTBEAT_PREFIX}{uid}")
                    exists_results = await pipe.execute()
                    for (uid, _), exists in zip(items, exists_results):
                        if not exists:
                            ghost_users.append(uid)
                if cursor == 0:
                    break

            if ghost_users:
                pipe = r.pipeline()
                for ghost_id in ghost_users:
                    pipe.zrem(KEY_WAITING, ghost_id)
                    pipe.srem(KEY_ACTIVE, ghost_id)
                    pipe.zrem(KEY_ACTIVE_EXPIRY, ghost_id)
                await pipe.execute()
                logger.info(
                    "👻 [Worker: Ghost Cleanup] Purged %d ghost session(s) without heartbeat (Sample: %s)",
                    len(ghost_users),
                    ghost_users[:3],
                )

            # 3. Dynamic TPS Throttling
            if await _cfg_bool("auto_throttling_enabled"):
                cpu_raw = await r.hget(KEY_DOWNSTREAM, "cpu_usage")
                if cpu_raw is not None:
                    cpu = float(cpu_raw)
                    base = await _cfg_int("base_batch_size")
                    current_batch = await _cfg_int("current_batch_size")
                    if cpu >= 90:
                        new_batch = max(1, int(base * 0.1))
                    elif cpu >= 80:
                        new_batch = max(1, int(base * 0.5))
                    elif cpu < 70:
                        new_batch = base
                    else:
                        new_batch = current_batch

                    if new_batch != current_batch:
                        await r.hset(KEY_CONFIG, "current_batch_size", str(new_batch))
                        logger.info(
                            "⚡ [Worker: Auto Throttle] Downstream CPU: %.1f%% -> Batch size adjusted: %d -> %d",
                            cpu,
                            current_batch,
                            new_batch,
                        )

            # 4. Admit Waiting Users (Atomic Batch Shift)
            if not await _cfg_bool("is_paused"):
                batch_size = await _cfg_int("current_batch_size")
                active_ttl = await _cfg_int("active_ttl_sec")
                if batch_size > 0:
                    admitted = await r.zpopmin(KEY_WAITING, batch_size)
                    if admitted:
                        pipe = r.pipeline()
                        for member, _ in admitted:
                            pipe.sadd(KEY_ACTIVE, member)
                            pipe.zadd(KEY_ACTIVE_EXPIRY, {member: now + active_ttl})
                            pipe.set(
                                f"{KEY_HEARTBEAT_PREFIX}{member}", "active", ex=HEARTBEAT_TTL_SEC
                            )
                        pipe.incrby(KEY_PASS_COUNT, len(admitted))
                        await pipe.execute()

                        sample_users = [m[0] for m in admitted[:3]]
                        logger.info(
                            "🎟️ [Worker: Batch Admit] Admitted %d user(s) from Queue to Active (Sample: %s)",
                            len(admitted),
                            sample_users,
                        )

        except asyncio.CancelledError:
            logger.info("🛑 Background worker cancelled")
            break
        except Exception:
            logger.exception("❌ Error in background worker loop")
            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Lifespan Context Manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        max_connections=REDIS_MAX_CONNECTIONS,
        decode_responses=True,
    )
    logger.info(
        "🔌 Connected to Redis Pool at %s:%s (Max Connections: %d)",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_MAX_CONNECTIONS,
    )

    await _ensure_key_type(KEY_WAITING, "zset")
    await _ensure_key_type(KEY_ACTIVE, "set")
    await _ensure_key_type(KEY_ACTIVE_EXPIRY, "zset")

    existing = await redis_client.hgetall(KEY_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        if k not in existing:
            await redis_client.hset(KEY_CONFIG, k, v)

    logger.info("⚙️ Runtime configuration initialized: %s", await redis_client.hgetall(KEY_CONFIG))

    worker_task = asyncio.create_task(_worker_loop())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await redis_client.aclose()
    logger.info("🔌 Redis connection pool closed")


# ---------------------------------------------------------------------------
# FastAPI App Engine
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Traffic-Guard - Virtual Waiting Room",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Public & Session API
# ---------------------------------------------------------------------------
@app.get("/queue/status")
async def queue_status(user_id: str = Query(..., min_length=1)):
    r = redis_client

    if await r.sismember(KEY_BLACKLIST, user_id):
        logger.warning("🚫 [API Access Denied] Blacklisted user attempt: %s", user_id)
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    await r.set(f"{KEY_HEARTBEAT_PREFIX}{user_id}", "checking", ex=HEARTBEAT_TTL_SEC)

    # 1. 이미 Active 상태인 경우
    if await r.sismember(KEY_ACTIVE, user_id):
        token = await _issue_jwt(user_id)
        await r.set(f"{KEY_HEARTBEAT_PREFIX}{user_id}", "active", ex=HEARTBEAT_TTL_SEC)
        logger.info("✅ [API Status] User '%s' is already ACTIVE (Token Issued)", user_id)
        return {"status": "ALLOWED", "token": token}

    bypass_threshold = await _cfg_int("bypass_threshold")
    active_count = await r.scard(KEY_ACTIVE)
    waiting_count = await r.zcard(KEY_WAITING)

    # 2. Bypass 임계값 이하인 경우 즉시 통과
    if active_count < bypass_threshold and waiting_count == 0:
        active_ttl = await _cfg_int("active_ttl_sec")
        token = await _shift_to_active(user_id, active_ttl)
        logger.info(
            "⚡ [API Bypass] User '%s' allowed immediately (Active: %d / Bypass limit: %d)",
            user_id,
            active_count,
            bypass_threshold,
        )
        return {"status": "ALLOWED", "token": token}

    # 3. 대기열 등록
    rank = await r.zrank(KEY_WAITING, user_id)
    if rank is None:
        await r.zadd(KEY_WAITING, {user_id: time.time()})
        rank = await r.zrank(KEY_WAITING, user_id)
        logger.info(
            "⏳ [API Waiting] User '%s' registered into waiting queue (Rank: %d)",
            user_id,
            (rank or 0) + 1,
        )

    current_batch = await _cfg_int("current_batch_size")
    polling_interval = await _cfg_int("polling_interval")
    estimated_seconds = (
        round(((rank + 1) / current_batch) * polling_interval, 1)
        if current_batch > 0 and rank is not None
        else 0.0
    )

    return {
        "status": "WAITING",
        "rank": (rank or 0) + 1,
        "estimated_seconds": estimated_seconds,
    }


@app.post("/api/heartbeat")
@app.post("/queue/heartbeat")
async def receive_heartbeat(body: HeartbeatRequest):
    r = redis_client
    user_id = body.user_id
    heartbeat_key = f"{KEY_HEARTBEAT_PREFIX}{user_id}"

    is_active = await r.sismember(KEY_ACTIVE, user_id)
    rank = await r.zrank(KEY_WAITING, user_id)

    if not is_active and rank is None:
        user_status = "unknown"
        logger.debug("❓ [Heartbeat] Received heartbeat for unknown user: %s", user_id)
    else:
        user_status = "active" if is_active else "waiting"
        await r.set(heartbeat_key, user_status, ex=HEARTBEAT_TTL_SEC)
        if is_active:
            active_ttl = await _cfg_int("active_ttl_sec")
            await r.zadd(KEY_ACTIVE_EXPIRY, {user_id: time.time() + active_ttl})
        logger.debug(
            "💓 [Heartbeat] Renewed session for user '%s' (Status: %s)", user_id, user_status
        )

    return {
        "status": "ok",
        "user_id": user_id,
        "user_status": user_status,
        "ttl": HEARTBEAT_TTL_SEC,
    }


@app.post("/api/waiting/leave")
@app.post("/api/logout")
async def explicit_leave_or_logout(request: Request):
    user_id = None
    try:
        raw_bytes = await request.body()
        if raw_bytes:
            raw_str = raw_bytes.decode("utf-8").strip()
            try:
                parsed = json.loads(raw_str)
                if isinstance(parsed, dict):
                    user_id = parsed.get("user_id")
            except Exception:
                user_id = raw_str
    except Exception as e:
        logger.warning("⚠️ Error parsing leave/logout request body: %s", e)

    if not user_id:
        return JSONResponse(status_code=400, content={"detail": "user_id is required"})

    await _cleanup_user_session(user_id)
    return {"status": "ok", "user_id": user_id}


# ---------------------------------------------------------------------------
# Client Queue WebSocket (정합성 및 자원 누수 완전 방지 적용)
# ---------------------------------------------------------------------------
@app.websocket("/ws/queue/status")
async def ws_queue_status(websocket: WebSocket, user_id: str = Query(..., min_length=1)):
    await websocket.accept()
    r = redis_client
    logger.info("🔌 [WS Client Connect] User '%s' connected via WebSocket", user_id)

    try:
        if await r.sismember(KEY_BLACKLIST, user_id):
            logger.warning("🚫 [WS Denied] Blacklisted user '%s' closed", user_id)
            await websocket.send_json({"status": "DENIED", "detail": "Blacklisted"})
            await websocket.close(code=1008)
            return

        # 최초 대기열 등록 확인
        pipe = r.pipeline()
        pipe.set(f"{KEY_HEARTBEAT_PREFIX}{user_id}", "waiting", ex=HEARTBEAT_TTL_SEC)
        pipe.sismember(KEY_ACTIVE, user_id)
        pipe.zrank(KEY_WAITING, user_id)
        _, initial_active, initial_rank = await pipe.execute()

        if not initial_active and initial_rank is None:
            await r.zadd(KEY_WAITING, {user_id: time.time()})
            logger.info("⏳ [WS Queue Entry] User '%s' added to queue via WS", user_id)

        while True:
            pipe = r.pipeline()
            pipe.set(f"{KEY_HEARTBEAT_PREFIX}{user_id}", "waiting", ex=HEARTBEAT_TTL_SEC)
            pipe.sismember(KEY_ACTIVE, user_id)
            pipe.scard(KEY_ACTIVE)
            pipe.zcard(KEY_WAITING)
            pipe.zrank(KEY_WAITING, user_id)
            _, is_active, active_count, waiting_count, rank = await pipe.execute()

            # 1. Active 상태 감지 시 통과
            if is_active:
                token = await _issue_jwt(user_id)
                logger.info(
                    "✅ [WS Allowed] User '%s' promoted to ACTIVE. Sending token & closing WS.",
                    user_id,
                )
                await websocket.send_json({"status": "ALLOWED", "token": token})
                await websocket.close()
                break

            bypass_threshold = await _cfg_int("bypass_threshold")

            # 2. Bypass 임계 조건 만족 시 통과
            if active_count < bypass_threshold and waiting_count <= 1:
                active_ttl = await _cfg_int("active_ttl_sec")
                token = await _shift_to_active(user_id, active_ttl)
                logger.info("⚡ [WS Bypass Shift] User '%s' shifted to ACTIVE immediately", user_id)
                await websocket.send_json({"status": "ALLOWED", "token": token})
                await websocket.close()
                break

            if rank is None:
                await asyncio.sleep(0.5)
                continue

            current_batch = await _cfg_int("current_batch_size")
            polling_interval = await _cfg_int("polling_interval")
            estimated_seconds = (
                round(((rank + 1) / current_batch) * polling_interval, 1)
                if current_batch > 0
                else 0.0
            )

            await websocket.send_json(
                {"status": "WAITING", "rank": rank + 1, "estimated_seconds": estimated_seconds}
            )
            await asyncio.sleep(polling_interval)

    except WebSocketDisconnect:
        logger.info(
            "🔌 [WS Client Disconnect] User '%s' disconnected. Cleaning session...", user_id
        )
    except Exception:
        logger.exception("❌ WS Error for user '%s'", user_id)
    finally:
        # Client disconnect 발생 시 대기열 누수를 막기 위한 정합성 보장 cleanup
        is_active = await r.sismember(KEY_ACTIVE, user_id)
        if not is_active:
            await r.zrem(KEY_WAITING, user_id)
            await r.delete(f"{KEY_HEARTBEAT_PREFIX}{user_id}")
            logger.info("🧹 [WS Cleanup] Safely removed disconnected user '%s' from queue", user_id)


# ---------------------------------------------------------------------------
# Downstream Feedback & Control APIs
# ---------------------------------------------------------------------------
@app.post("/downstream/metrics")
async def receive_downstream_metrics(body: DownstreamMetrics):
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
    logger.debug(
        "📊 Received downstream metrics from server '%s' (CPU: %.1f%%)",
        body.server_id,
        body.cpu_usage,
    )
    return {"status": "ok"}


@app.post("/queue/expire-token")
async def expire_token(body: ExpireTokenRequest):
    await _cleanup_user_session(body.user_id)
    return {"status": "ok", "user_id": body.user_id}


@app.post("/admin/config")
async def update_config(body: AdminConfig):
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
        logger.info("🛠️ [Admin Config Update] Changed configurations: %s", mapping)
    return {"status": "ok", "updated": mapping}


@app.post("/admin/queue/flush")
async def flush_queue():
    r = redis_client
    pipe = r.pipeline()
    pipe.delete(KEY_WAITING)
    pipe.delete(KEY_ACTIVE)
    pipe.delete(KEY_ACTIVE_EXPIRY)
    pipe.set(KEY_PASS_COUNT, 0)
    await pipe.execute()
    logger.warning("🚨 [Admin Action] Flushed all queues and metrics!")
    return {"status": "ok"}


@app.post("/admin/blacklist")
async def add_blacklist(body: BlacklistRequest):
    r = redis_client
    added = []
    if body.user_id:
        await r.sadd(KEY_BLACKLIST, body.user_id)
        added.append(f"user:{body.user_id}")
    if body.ip:
        await r.sadd(KEY_BLACKLIST, body.ip)
        added.append(f"ip:{body.ip}")
    logger.info("🚫 [Admin Action] Blacklisted targets: %s", added)
    return {"status": "ok", "blacklisted": added}


@app.websocket("/ws/admin/metrics")
async def ws_admin_metrics(websocket: WebSocket):
    await websocket.accept()
    r = redis_client
    logger.info("💻 [Admin WS Connect] Admin Dashboard connected to real-time metrics stream")
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
        logger.info("💻 [Admin WS Disconnect] Admin Dashboard disconnected")
    except Exception:
        logger.exception("❌ Admin WS Stream Error")


# ---------------------------------------------------------------------------
# Page Render Endpoints
# ---------------------------------------------------------------------------
@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard():
    return HTMLResponse(content=load_template("dashboard.html"))


@app.get("/queue/page", response_class=HTMLResponse)
async def queue_page():
    return HTMLResponse(content=load_template("queue_3.html"))


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        backlog=2048,
        limit_concurrency=2000,
        log_level="info",
    )
