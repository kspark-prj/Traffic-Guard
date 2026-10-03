import asyncio
import json

import websockets

SERVER_URL = "ws://localhost:8000/ws/queue/status?user_id="
TOTAL_USERS = 500


async def simulate_user(user_index: int):
    user_id = f"sim_user_{user_index:03d}"
    url = f"{SERVER_URL}{user_id}"

    try:
        async with websockets.connect(url) as ws:
            print(f"[{user_id}] 대기열 진입")
            while True:
                msg = await ws.recv()
                data = json.loads(msg)

                status = data.get("status")
                if status == "WAITING":
                    rank = data.get("rank")
                    eta = data.get("estimated_seconds")
                    print(f"[{user_id}] 대기 중... 현재 순번: {rank}번 (예상: {eta}초)")
                elif status == "ALLOWED":
                    token = data.get("token")
                    print(f"✅ [{user_id}] 입장 성공! (발급 Token: {token[:15]}...)")

                    # [핵심 수정] 입장 성공 후 바로 종료하지 않고,
                    # 일정 시간(예: 60초)동안 머물며 세션을 유지합니다.
                    print(f"[{user_id}] 서비스 이용 중 (60초간 세션 유지)...")
                    await asyncio.sleep(60)
                    break
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[{user_id}] 에러 발생: {e}")


async def main():
    print(f"=== {TOTAL_USERS}명의 동시 접속 모의 테스트 시작 ===")
    tasks = [simulate_user(i) for i in range(1, TOTAL_USERS + 1)]
    await asyncio.gather(*tasks)
    print("=== 테스트 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
