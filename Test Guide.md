제공해주신 프로젝트 문서(`Downstream_Guideline.md`, `README.md`)의 최신 인터페이스 및 요구사항(JWT 검증 헤더, `GET /queue/status`, `POST /queue/expire-token`, `POST /downstream/metrics`, Admin API 등)에 맞춰 현행화된 **Traffic-Guard PoC 테스트 가이드라인**입니다.

---

# 🚦 Traffic-Guard PoC 테스트 가이드라인

본 가이드라인은 가상 대기실 미들웨어 **Traffic-Guard**의 기능성, 동적 TPS 제어(Auto-Throttling), 하류(Downstream) 시스템 보호 및 대량 동시 접속 처리 성능을 체계적으로 검증하기 위한 PoC 테스트 계획서입니다.

---

## 1. 테스트 개요

- **목적**: 대량 트래픽 유입 시 하류 시스템 보호, JWT 기반 대기열 통과 검증, 동적 TPS 조절, 토큰 만료(슬롯 반환) 및 관제 제어 기능의 정상 작동 여부 검증

- **대상 시스템**: Traffic-Guard FastAPI 웹소켓/HTTP 서버, Redis 데이터 구조, Nginx 프록시, 관리자 대시보드

- **테스트 범위**: 우회 통과(Bypass), 대기열 순번 부여 및 소진, JWT 토큰 발급/만료, Downstream CPU 부하 연동, 블랙리스트 및 제어 기능

---

## 2. 테스트 환경 및 사전 조건

### 2.1 소프트웨어 및 인프라 요구사항

| 항목        | 사양 / 요구사항 | 비고                  |
| ----------- | --------------- | --------------------- |
| **Runtime** | Python 3.10+    | FastAPI, Uvicorn 실행 |

|
| **In-Memory DB** | Redis 7.0+ | Port 6379 (ZSET, HASH, SET 활용)

|
| **Gateway** | Nginx | Port 80 (단일 도메인 프록시 구성)

|
| **의존성 패키지** | `fastapi`, `uvicorn`, `redis`, `pyjwt`, `websockets`, `pytest`, `fakeredis` | `requirements.txt` 기반

|

### 2.2 실행 절차

1. **Docker Compose 전체 환경 실행**

```bash
docker compose up -d

```

2. **접속 엔드포인트 확인**

- 관리자 대시보드: `http://localhost/admin/dashboard`

- 클라이언트 대기열 상태 조회: `http://localhost/queue/status?user_id={USER_ID}`

---

## 3. 테스트 시나리오 요약표

| ID        | 시나리오명                      | 핵심 검증 내용                                        | API / 엔드포인트                                       | 우선순위 |
| --------- | ------------------------------- | ----------------------------------------------------- | ------------------------------------------------------ | -------- |
| **TC-01** | 우회 통과 (Bypass)              | Threshold 이하 유입 시 즉시 `ALLOWED` 및 JWT 발급     | `GET /queue/status`<br>                                | High     |
| **TC-02** | 대기열 전환 및 순번 부여        | Threshold 초과 시 `WAITING` 상태 및 순번/ETA 반환     | `GET /queue/status`<br>                                | High     |
| **TC-03** | 순번 소진 및 JWT 통과           | 배치 워커에 의한 순차 승인 및 JWT 서명 검증           | `GET /queue/status`<br>                                | High     |
| **TC-04** | 동적 TPS 조절 (Throttling)      | Downstream CPU 부하(80%↑/90%↑) 수신 시 배치 크기 감속 | `POST /downstream/metrics`<br>                         | High     |
| **TC-05** | 토큰 명시적 만료 (슬롯 반환)    | 비즈니스 처리 완료 후 슬롯 회수 및 다음 대기자 진입   | `POST /queue/expire-token`<br>                         | Medium   |
| **TC-06** | 블랙리스트 및 초기화 제어       | 차단 유저 403 거부 및 전체 대기열 초기화(Flush)       | `POST /admin/blacklist`, `POST /admin/queue/flush`<br> | Medium   |
| **TC-07** | Automated Unit/Integration Test | Pytest 기반의 automated test suite 검증               | `pytest test_main.py`<br>                              | High     |

---

## 4. 상세 테스트 절차 및 검증 항목

### TC-01: 우회 통과 (Bypass Mode) 검증

1. **테스트 조건**: `bypass_threshold = 500`, 현재 활성 유저 < 500

2. **테스트 절차**:

```bash
curl -s "http://localhost/queue/status?user_id=test-user-01"

```

3. **기대 결과**:

- 응답 status가 `ALLOWED`로 반환됨

- HS256 알고리즘으로 서명된 JWT 토큰(`token`)이 함께 발급됨

- 대시보드의 `Active Users` 및 `Total Passed` 카운트가 1 증가함

### TC-02: 대기열 진입 및 순번 부여 검증

1. **테스트 조건**: 관리자 API를 통해 `bypass_threshold`를 `0`으로 설정하여 강제 대기열 모드 활성화

```bash
curl -X POST http://localhost/admin/config \
  -H "Content-Type: application/json" \
  -d '{"bypass_threshold": 0}'[cite: 4]

```

2. **테스트 절차**: 순서대로 3명의 유저 요청 전송

```bash
curl -s "http://localhost/queue/status?user_id=user-A"
curl -s "http://localhost/queue/status?user_id=user-B"
curl -s "http://localhost/queue/status?user_id=user-C"

```

3. **기대 결과**:

- 응답 status가 `WAITING`으로 반환됨

- 접속 순서에 맞춰 `rank`(순번) 및 `estimated_seconds`(예상 대기시간)가 정렬되어 전달됨

### TC-03: 배치 워커 순번 소진 및 토큰 발급 검증

1. **테스트 조건**: TC-02 상태 유지 (`base_batch_size = 50`, `polling_interval = 1`)

2. **테스트 절차**:
3. 대기 중인 `user-A` ID로 1초 간격 재조회 (`GET /queue/status?user_id=user-A`)

4. 대시보드(`/admin/dashboard`) 및 백그라운드 워커 로그 확인

5. **기대 결과**:

- 주기적인 배치 승인 처리에 따라 `WAITING` 상태가 `ALLOWED`로 전환되고 JWT 토큰이 발급됨

- 대시보드의 `Waiting` 수치가 감소하고 `Active Users` 수치가 증가함

### TC-04: Downstream CPU 부하 연동 및 동적 TPS 조절 검증

1. **테스트 조건**: 대기열 유저가 존재하는 상태

2. **테스트 절차**:
3. Downstream 메트릭 전송 API를 통해 CPU 85% 상태 수신

```bash
curl -X POST http://localhost/downstream/metrics \
  -H "Content-Type: application/json" \
  -d '{"server_id":"ds-node-01","cpu_usage":85.0,"active_connections":200,"http_5xx_rate":0.01}'[cite: 3, 4]

```

2. 대시보드의 `Current TPS` 및 CPU Gauge 수치 확인

3. CPU 95% 상태로 변경하여 전송

4. **기대 결과**:

- CPU 85% 수신 시: 입장 처리 속도가 `base_batch_size`의 50%로 자동 감속

- CPU 95% 수신 시: 입장 처리 속도가 10%로 강력 감속되어 Downstream 과부하 방지 작동

- CPU < 70% 재전송 시: 입장 속도가 100%로 자동 복구됨

### TC-05: 명시적 토큰 만료 (슬롯 반환) 검증

1. **테스트 조건**: `user-A`가 `ALLOWED` 상태로 진입한 상태

2. **테스트 절차**:
3. 하류 비즈니스 로직(예매/결제) 완료 상황을 가상하여 슬롯 반환 API 호출

```bash
curl -X POST http://localhost/queue/expire-token \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user-A"}'[cite: 3, 4]

```

3. **기대 결과**:

- 응답으로 `{"status": "ok", "user_id": "user-A"}` 반환

- `queue:active` 집합에서 해당 유저가 즉시 제거되어 대기 중인 다음 유저의 빠른 진입 공간(슬롯) 확보

### TC-06: 블랙리스트 차단 및 전체 대기열 초기화(Flush) 검증

1. **테스트 절차**:
1. 블랙리스트 등록 API 호출

```bash
curl -X POST http://localhost/admin/blacklist \
  -H "Content-Type: application/json" \
  -d '{"user_id":"bad-user"}'[cite: 4]

```

2. 차단된 유저로 상태 조회 시도 (`GET /queue/status?user_id=bad-user`)

3. 대기열 전체 초기화 API 호출

```bash
curl -X POST http://localhost/admin/queue/flush[cite: 4]

```

2. **기대 결과**:

- `bad-user` 요청 시 `403 Forbidden` 반환

- `flush` 호출 시 Redis 내 `queue:waiting`, `queue:active` 등 모든 대기열 관련 데이터 구조가 즉시 비워짐

### TC-07: 자동화 단위/통합 테스트 (Pytest)

1. **테스트 절차**:

```bash
# 로컬 또는 컨테이너 내부 실행 (fakeredis 기반)
pytest test_main.py -v[cite: 4]

```

2. **기대 결과**:

- `test_bypass_direct_admission`, `test_downstream_cpu_throttling`, `test_expire_token` 등 제공된 9개 핵심 테스트케이스가 전체 PASS 처리됨

---

## 5. 성공 / 실패 판정 기준 (Pass/Fail Criteria)

| 평가 항목       | 성공 기준 (Pass)                   | 실패 기준 (Fail) |
| --------------- | ---------------------------------- | ---------------- |
| **기능 정확성** | - Bypass/Queue 모드 자동 전환 성공 |

<br>

<br>- 통과자에게 정합성 있는 JWT 발급

<br>

<br>- `expire-token` 호출 시 슬롯 즉시 해제

| - 순번 무한 대기 또는 누락 발생<br>

<br>- JWT 서명 검증 실패 또는 토큰 미발급

|
| **동적 제어** | - CPU 80%↑ 시 50%, 90%↑ 시 10% 자동 감속

<br>

<br>- CPU 70%↓ 회복 시 100% 자동 복구

| - 하류 과부하 수신에도 TPS 감속이 작동하지 않음

|
| **보안 및 접근 제어** | - 블랙리스트 유저 `403` 차단

<br>

<br>- Nginx 프록시를 통한 단일 도메인 요청 정상 처리

| - 차단 유저의 대기열 진입 허용<br>

<br>- CORS 에러 발생

|
| **자동화 테스트** | - `pytest test_main.py` 실행 시 100% Success

| - 테스트 케이스 실패 발생

|
