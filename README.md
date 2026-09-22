# Traffic-Guard

**가상 대기열(Virtual Waiting Room) 미들웨어**

트래픽 급증으로부터 서비스를 보호하는 FastAPI 기반 가상 대기열 관리 시스템입니다.

---

## ✨ 주요 기능

| 기능                                | 설명                                                                      |
| :---------------------------------- | :------------------------------------------------------------------------ |
| **가변 가상 대기열**                | 트래픽 상황에 따라 Bypass / Active Queue 모드 자동 전환                   |
| **Active User TTL & 하트비트**      | 30초 Redis TTL 하트비트를 통한 실시간 세션 감지 및 슬롯 회수              |
| **고스트 세션 자동 Cleanup**        | 하트비트 미수신 사용자의 대기/활성 상태 자동 제거                         |
| **명시적 이탈/로그아웃 처리**       | `sendBeacon` / `fetch(keepalive)`를 통한 탭 닫기·이탈 시 즉시 데이터 정단 |
| **다운스트림 부하 인식 동적 TPS**   | 하류 서버 CPU 사용률에 따른 배치 크기 자동 조절                           |
| **실시간 관리자 대시보드**          | WebSocket 기반 실시간 지표 및 런타임 제어 UI                              |
| **실시간 클라이언트 대기열 페이지** | WebSocket 기반 순번 업데이트 및 자동 상태 전환                            |
| **JWT 통과 검증 토큰**              | 대기열 통과 시 서명된 JWT 토큰 발급                                       |
| **블랙리스트**                      | 특정 사용자 ID/IP 접근 차단                                               |

---

## 🏗️ 기술 스택

- **Backend**: Python 3.10+, [FastAPI](https://fastapi.tiangolo.com/)
- **Queue Store**: [Redis](https://redis.io/) (Sorted Set, Set, Key-TTL, Hash 활용)
- **Token**: [PyJWT](https://pyjwt.readthedocs.io/)
- **Server**: [Uvicorn](https://www.uvicorn.org/)
- **Container**: Docker Compose

---

## 📁 프로젝트 구조

```text
fastGateway/
├── main.py                  # 메인 애플리케이션 (API, WebSocket, Background Worker)
├── simulate_traffic.py      # 동시 접속 부하 테스트 스크립트
├── docker-compose.yaml      # Redis 컨테이너 설정
├── Downstream_Guideline.md  # 다운스트림 연동 가이드
├── Test Guide.md            # 테스트 가이드
├── static/
│   └── traffic_guard_client.js # 클라이언트용 SDK (Heartbeat & sendBeacon 자동화)
└── templates/
    ├── dashboard.html       # 관리자 대시보드 UI
    └── queue.html           # 사용자 대기열 페이지 UI
```

````

---

## 🚀 빠른 시작

### 1. 사전 요구사항

- Python 3.10+
- Docker & Docker Compose (Redis 실행용)

### 2. Redis 실행

```bash
docker-compose up -d

```

Redis가 `localhost:6379`에서 실행됩니다.

### 3. Python 의존성 설치

```bash
pip install fastapi uvicorn redis pyjwt websockets pydantic

```

### 4. 서버 실행

```bash
python main.py

```

서버가 `http://0.0.0.0:8000`에서 시작됩니다 (hot-reload 활성화).

### 5. 접속 확인

| URL                                     | 설명                         |
| --------------------------------------- | ---------------------------- |
| `http://localhost:8000/docs`            | Swagger API 문서 (자동 생성) |
| `http://localhost:8000/admin/dashboard` | 관리자 대시보드              |
| `http://localhost:8000/queue/page`      | 사용자 대기열 페이지         |

---

## 🔧 환경 변수

| 변수         | 기본값                                  | 설명                                      |
| ------------ | --------------------------------------- | ----------------------------------------- |
| `REDIS_HOST` | `localhost`                             | Redis 호스트 주소                         |
| `REDIS_PORT` | `6379`                                  | Redis 포트                                |
| `JWT_SECRET` | `super-secret-key-change-in-production` | JWT 서명 비밀키 (**운영 시 반드시 변경**) |

---

## ⚙️ 런타임 설정 (Redis 기반)

Admin API (`POST /admin/config`)를 통해 런타임에 동적으로 변경 가능합니다.

| 항목                      | 기본값  | 설명                                                |
| ------------------------- | ------- | --------------------------------------------------- |
| `bypass_threshold`        | `500`   | 이 수 이하의 활성 사용자일 때 대기열 없이 바로 통과 |
| `active_ttl_sec`          | `300`   | 활성 사용자 슬롯 유지 시간 (초)                     |
| `base_batch_size`         | `50`    | 기본 배치 크기 (한 주기당 입장 인원)                |
| `current_batch_size`      | `50`    | 현재 적용 중인 배치 크기 (동적 조절됨)              |
| `polling_interval`        | `1`     | 백그라운드 워커 실행 주기 (초)                      |
| `token_ttl_sec`           | `60`    | 발급된 JWT 토큰 유효 시간 (초)                      |
| `is_paused`               | `false` | `true`로 설정 시 대기열 입장 일시 중지              |
| `auto_throttling_enabled` | `true`  | 다운스트림 CPU에 따른 자동 TPS 조절 활성화          |

---

## 📡 API 명세

### A. Public & Session API — 대기열 상태 조회

#### `GET /queue/status?user_id={user_id}`

사용자의 대기열 상태를 조회하고 하트비트 세션을 생성합니다.

**응답 예시 (바로 통과):**

```json
{
    "status": "ALLOWED",
    "token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**응답 예시 (대기 중):**

```json
{
    "status": "WAITING",
    "rank": 42,
    "estimated_seconds": 8.4
}
```

---

### B. Heartbeat & Session Cleanup APIs

#### `POST /api/heartbeat` 또는 `POST /queue/heartbeat`

클라이언트 SDK에서 10초 주기로 보낸 하트비트를 수신하여 Redis Key TTL(30초)을 연장합니다.

**요청 Body:**

```json
{
    "user_id": "user_123"
}
```

#### `POST /api/waiting/leave` 또는 `POST /api/logout`

명시적 로그아웃 또는 탭 닫기(`sendBeacon`) 시 호출되어 사용자 세션을 즉시 정단합니다.

**요청 Body:** (Plain text 또는 JSON 모두 지원)

```json
{
    "user_id": "user_123"
}
```

---

### C. Client WebSocket — 실시간 대기열 상태

#### `WS /ws/queue/status?user_id={user_id}`

WebSocket 연결을 통해 대기 순번을 실시간 수신합니다. 수신 중 자동으로 하트비트 TTL을 갱신하며, 입장 허용 시 JWT 토큰 수신 후 연결이 종료됩니다.

---

### D. Downstream Feedback API

#### `POST /downstream/metrics`

다운스트림 서버의 CPU 사용률 등 메트릭을 수신합니다.

#### `POST /queue/expire-token`

활성 사용자의 슬롯을 명시적으로 해제합니다.

---

### E. Admin API & WebSocket

- `POST /admin/config`: 런타임 설정 업데이트
- `POST /admin/queue/flush`: 대기열/액티브 데이터 전체 초기화
- `POST /admin/blacklist`: 사용자 ID 또는 IP 블랙리스트 추가
- `WS /ws/admin/metrics`: 관리자 대시보드 실시간 메트릭 스트리밍 (1초 간격)

---

## 🔄 세션 라이프사이클 및 자동 Cleanup 로직

1. **하트비트 유지**: 클라이언트 SDK가 10초마다 `/api/heartbeat`를 전송하여 Redis Key (`queue:heartbeat:{user_id}`)의 TTL을 30초로 유지합니다.
2. **고스트 세션 청소**: 브라우저 강제 종료 등으로 하트비트가 30초간 끊기면 백그라운드 워커가 이를 탐지하여 `queue:waiting`, `queue:active`, `queue:active:expiry`에서 해당 유저를 완전 제거합니다.
3. **명시적 이탈 처리**: 탭 닫기/이탈 시 `sendBeacon`이 `/api/waiting/leave`를 호출하여 즉시 세션을 정리합니다. 대기 완결 후 페이지 이동 시에는 SDK의 Beacon 연동을 억제(`suppressBeacon`)하여 오인 삭제를 방지합니다.

---

## 🗄️ Redis 데이터 구조

| Key                         | Type       | 용도                                       |
| --------------------------- | ---------- | ------------------------------------------ |
| `queue:waiting`             | Sorted Set | 대기 사용자 (score = 진입 타임스탬프)      |
| `queue:active`              | Set        | 현재 활성 사용자                           |
| `queue:active:expiry`       | Sorted Set | 활성 사용자 만료 시각 (score = 만료 epoch) |
| `queue:heartbeat:{user_id}` | String     | 유저 세션 하트비트 (TTL = 30s)             |
| `queue:config`              | Hash       | 런타임 설정값                              |
| `queue:metrics:downstream`  | Hash       | 다운스트림 서버 메트릭                     |
| `queue:metrics:pass_count`  | String     | 총 통과 사용자 수 (카운터)                 |
| `queue:blacklist`           | Set        | 차단된 사용자/IP 목록                      |

---

## 📊 시퀀스 다이어그램

### 사용자 대기열 진입, 하트비트 유지 및 이탈 흐름

```mermaid
sequenceDiagram
    actor Client
    participant SDK as JS SDK
    participant FP as Traffic-Guard
    participant Redis

    Note over Client, Redis: 1. 대기열 상태 확인 & 하트비트 등록

    Client->>FP: GET /queue/status?user_id=user_123
    FP->>Redis: SET queue:heartbeat:user_123 "checking" EX 30

    alt Active < bypass_threshold (Bypass 모드)
        FP->>Redis: SADD queue:active / ZADD queue:active:expiry
        FP-->>Client: {"status": "ALLOWED", "token": "JWT..."}
    else Active >= bypass_threshold (Queue 모드)
        FP->>Redis: ZADD queue:waiting (timestamp, user_123)
        FP-->>Client: {"status": "WAITING", "rank": 43, ...}
    end

    Note over Client, Redis: 2. 주기적 Heartbeat 연장 (10초 주기)

    loop 매 10초
        SDK->>FP: POST /api/heartbeat {"user_id": "user_123"}
        FP->>Redis: SET queue:heartbeat:user_123 "active/waiting" EX 30
    end

    Note over Client, Redis: 3. 백그라운드 워커 — 고스트 세션 자동 삭제

    loop 매 polling_interval 초
        FP->>Redis: EXISTS queue:heartbeat:{user_id}
        alt Heartbeat Key 미존재 (30초 타임아웃)
            FP->>Redis: ZREM queue:waiting / SREM queue:active / ZREM queue:active:expiry
        end
    end

    Note over Client, Redis: 4. 브라우저 이탈 / 탭 닫기 (sendBeacon)

    Client->>SDK: 탭 닫기 (beforeunload)
    SDK->>FP: POST /api/waiting/leave (sendBeacon)
    FP->>Redis: DEL queue:heartbeat:user_123 & ZREM/SREM 세션 삭제


````
