# backend_verify.py
"""
Backend API Server with Queue JWT Verification Middleware
=========================================================
대기열 서버(main.py)에서 발급한 JWT 토큰을 검증하여
보호된 API 요청을 처리하는 백엔드 서버입니다.
"""

import os

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (main.py와 설정값 동기화)
# ---------------------------------------------------------------------------
# main.py의 JWT_SECRET 환경변수 또는 기본값과 동일해야 합니다.
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"

app = FastAPI(
    title="Business Backend API",
    description="Traffic-Guard JWT 토큰을 검증하는 메인 비즈니스 서버입니다.",
    version="1.0.0",
)

security = HTTPBearer()


# ---------------------------------------------------------------------------
# JWT Verification Dependency
# ---------------------------------------------------------------------------
def verify_active_session(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    HTTP Header (Authorization: Bearer <TOKEN>)의 JWT를 검증하고 user_id를 추출합니다.
    """
    token = credentials.credentials
    try:
        # 1. JWT 서명 검증 및 만료 시간(exp) 자동 확인
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("user_id")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="토큰에 user_id 정보가 존재하지 않습니다.",
            )
        return user_id

    except jwt.ExpiredSignatureError:
        # 토큰 만료 시 -> 프론트엔드에서 대기열 재입장 처리 필요
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="세션이 만료되었습니다. 대기열에 다시 입장해주세요.",
        )
    except jwt.PyJWTError:
        # 서명 불일치 또는 잘못된 토큰 형식
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 인증 토큰입니다."
        )


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------
class OrderRequest(BaseModel):
    item_id: str
    quantity: int


# ---------------------------------------------------------------------------
# Business API Endpoints (보호된 API)
# ---------------------------------------------------------------------------
@app.post("/api/v1/orders")
async def create_order(order: OrderRequest, user_id: str = Depends(verify_active_session)):
    """
    대기열을 통과한 유저만 접근 가능한 주문 처리 API
    """
    # verify_active_session을 통과했으므로 유효한 active user_id가 보장됨
    return {
        "status": "success",
        "message": "주문이 성공적으로 접수되었습니다.",
        "data": {"user_id": user_id, "item_id": order.item_id, "quantity": order.quantity},
    }


@app.get("/api/v1/user/profile")
async def get_profile(user_id: str = Depends(verify_active_session)):
    """
    유저 프로필 조회 API
    """
    return {"status": "success", "user_id": user_id}


# ---------------------------------------------------------------------------
# Server Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # main.py(8000번 포트)와 충돌을 피하기 위해 8001번 포트로 실행합니다.
    uvicorn.run("backend_verify:app", host="0.0.0.0", port=8001, reload=True)
