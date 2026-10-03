import asyncio
import time

import httpx

BASE_URL = "http://localhost:8000"
NUM_USERS = 600  # bypass_threshold(500) 초과 범위


async def simulate_user(client: httpx.AsyncClient, user_id: str, delay: float):
    # 각 유저별 소폭의 시차 부여 (0.01초 단위 간격 진입)
    await asyncio.sleep(delay)

    for attempt in range(3):  # 서버 순간 튕김 대비 최대 3회 재시도
        try:
            resp = await client.get(f"{BASE_URL}/queue/status", params={"user_id": user_id})
            data = resp.json()
            status = data.get("status")
            rank = data.get("rank", 0)

            print(f"[{user_id}] Status: {status} | Rank: {rank}")
            break
        except Exception as e:
            if attempt == 2:
                print(f"[{user_id}] ❌ 최종 실패: {e}")
            await asyncio.sleep(0.5)

    # 대기열 등록 후 세션 유지용 하트비트 전송
    for _ in range(15):
        await asyncio.sleep(2)
        try:
            await client.post(f"{BASE_URL}/api/heartbeat", json={"user_id": user_id})
        except Exception:
            pass


async def main():
    limits = httpx.Limits(max_keepalive_connections=1000, max_connections=1000)
    timeout = httpx.Timeout(15.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        print(f"🚀 가상 유저 {NUM_USERS}명 순차 동시 진입 시작 (Ramp-up 적용)...")
        start_time = time.time()

        # 0.01초 간격으로 유저 생성 요청 배치
        tasks = [
            simulate_user(client, f"test_user_{i}", delay=i * 0.01) for i in range(1, NUM_USERS + 1)
        ]
        await asyncio.gather(*tasks)

        print(f"⏱️ 테스트 완료 (소요 시간: {round(time.time() - start_time, 2)}초)")


if __name__ == "__main__":
    asyncio.run(main())
