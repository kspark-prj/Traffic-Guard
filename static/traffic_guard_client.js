/**
 * Traffic-Guard Client SDK (traffic_guard_client.js)
 * ===================================================
 * 웹 기반 Traffic-Guard 대기열 및 세션 관리 클라이언트 라이브러리
 *
 * 핵심 역할:
 * 1. 식별자(Unique ID) 생성 및 영구 보관 (Cookie, localStorage)
 * 2. 10초 주기 하트비트(Heartbeat) 전송 → 백엔드 Redis TTL 30초 갱신
 * 3. 페이지 이탈/탭 닫기(beforeunload, pagehide) 시 navigator.sendBeacon 즉시 세션 정리
 * 4. 명시적 로그아웃(logout) API 연동 및 식별자 삭제
 *
 * 주의:
 * - 정상 리다이렉트(대기 → 액티브 전환 후 서비스 페이지 이동) 시에는
 *   suppressBeacon()을 호출하여 beforeunload 이탈 처리를 비활성화해야 합니다.
 *   그렇지 않으면 beforeunload에서 sendBeacon이 실행되어 방금 이관한 액티브 세션이 삭제됩니다.
 */

(function (window) {
    'use strict';

    const CONFIG = {
        STORAGE_KEY: 'tg_user_id',
        COOKIE_KEY: 'tg_user_id',
        HEARTBEAT_INTERVAL_MS: 10000,      // 10초 주기 하트비트
        HEARTBEAT_ENDPOINT: '/api/heartbeat',
        LEAVE_ENDPOINT: '/api/waiting/leave',
        LOGOUT_ENDPOINT: '/api/logout',
    };

    let heartbeatTimer = null;
    let currentUserId = null;

    /**
     * [이탈 억제 플래그]
     * 정상적인 페이지 전환(예: 대기 완료 후 서비스 페이지 리다이렉트)에서
     * beforeunload / pagehide 이벤트가 발생하더라도 이탈 요청을 보내지 않도록 제어합니다.
     *
     * - true : sendLeaveBeacon()이 호출되어도 요청을 보내지 않음 (정상 전환)
     * - false: sendLeaveBeacon()이 호출되면 즉시 /api/waiting/leave로 이탈 요청 전송
     */
    let beaconSuppressed = false;

    // =====================================================================
    // 1. 식별자(Unique ID) 관리
    // =====================================================================

    /**
     * 유니크 아이디를 조회하거나 신규 생성합니다.
     *
     * 탐색 우선순위: URL ?user_id= → localStorage → Cookie
     * 없으면 crypto.randomUUID() 기반으로 생성하여 localStorage 및 Cookie에 영구 보관합니다.
     * 페이지 새로고침이나 MPA 환경에서 다른 페이지로 이동해도 동일한 ID가 유지됩니다.
     */
    function getOrCreateUserId() {
        if (currentUserId) return currentUserId;

        // A. URL Query String 에서 확인
        const urlParams = new URLSearchParams(window.location.search);
        let userId = urlParams.get('user_id');

        // B. localStorage 에서 확인
        if (!userId) {
            try {
                userId = localStorage.getItem(CONFIG.STORAGE_KEY);
            } catch (e) {
                console.warn('[Traffic-Guard] localStorage 읽기 실패:', e);
            }
        }

        // C. Cookie 에서 확인
        if (!userId) {
            const match = document.cookie.match(
                new RegExp('(?:^|; )' + CONFIG.COOKIE_KEY + '=([^;]*)')
            );
            if (match) {
                userId = decodeURIComponent(match[1]);
            }
        }

        // D. 신규 생성 (UUID v4 기반, 불가 시 timestamp+random fallback)
        if (!userId) {
            if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                userId = 'usr_' + crypto.randomUUID().replace(/-/g, '').substring(0, 16);
            } else {
                userId =
                    'usr_' +
                    Date.now().toString(36) +
                    Math.random().toString(36).substring(2, 9);
            }
        }

        // 영구 저장소 기록 (localStorage & Cookie 동시 저장)
        try {
            localStorage.setItem(CONFIG.STORAGE_KEY, userId);
        } catch (e) {
            /* private-browsing 등에서 실패할 수 있음 */
        }

        const expires = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toUTCString();
        document.cookie =
            CONFIG.COOKIE_KEY +
            '=' +
            encodeURIComponent(userId) +
            '; expires=' +
            expires +
            '; path=/; SameSite=Lax';

        currentUserId = userId;
        return userId;
    }

    // =====================================================================
    // 2. Web Heartbeat (10초 주기)
    // =====================================================================

    /**
     * 백엔드 /api/heartbeat 로 유니크 아이디를 전송합니다.
     * 서버는 수신 즉시 Redis Heartbeat Key(queue:heartbeat:{uid})의
     * 만료 시간(TTL)을 30초로 재설정(EXPIRE)합니다.
     *
     * MPA 환경에서 페이지 이동 시 발생하는 수백 ms~1초의 하트비트 공백은
     * 30초의 Redis TTL 여유값으로 충분히 흡수되어 세션이 유지됩니다.
     */
    async function sendHeartbeat() {
        const userId = getOrCreateUserId();
        try {
            const response = await fetch(CONFIG.HEARTBEAT_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId }),
            });
            if (response.ok) {
                const data = await response.json();
                console.debug('[Traffic-Guard] Heartbeat 갱신 성공:', data);
            }
        } catch (err) {
            console.warn('[Traffic-Guard] Heartbeat 전송 실패:', err);
        }
    }

    /** 하트비트 타이머 시작 (중복 호출 방지) */
    function startHeartbeat() {
        if (heartbeatTimer) return;
        sendHeartbeat(); // 진입 즉시 1회 전송
        heartbeatTimer = setInterval(sendHeartbeat, CONFIG.HEARTBEAT_INTERVAL_MS);
        console.log('[Traffic-Guard] 하트비트 타이머 시작 (10초 주기)');
    }

    /** 하트비트 타이머 정지 */
    function stopHeartbeat() {
        if (heartbeatTimer) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
            console.log('[Traffic-Guard] 하트비트 타이머 정지');
        }
    }

    // =====================================================================
    // 3. 이탈 처리 (sendBeacon / fetch keepalive)
    // =====================================================================

    /**
     * 탭 닫기 또는 페이지 이탈 시 /api/waiting/leave 로 삭제 요청을 전송합니다.
     * - navigator.sendBeacon()을 우선 사용 (브라우저 종료 직전에도 전달 보장)
     * - 미지원 시 fetch({keepalive: true}) fallback
     *
     * [중요] beaconSuppressed === true 이면 요청을 보내지 않습니다.
     * 정상 리다이렉트(대기 완료 → 서비스 페이지) 직전에 suppressBeacon()을 호출하세요.
     */
    function sendLeaveBeacon() {
        // 이탈 억제 상태면 아무것도 하지 않음 (정상 리다이렉트 중)
        if (beaconSuppressed) {
            console.debug('[Traffic-Guard] sendBeacon 억제 상태 — 이탈 요청 건너뜀');
            return;
        }

        const userId = currentUserId;
        if (!userId) return;

        const payload = JSON.stringify({ user_id: userId });
        const endpoint = CONFIG.LEAVE_ENDPOINT;

        // navigator.sendBeacon 우선 사용
        if (navigator.sendBeacon) {
            const blob = new Blob([payload], { type: 'application/json' });
            const sent = navigator.sendBeacon(endpoint, blob);
            if (sent) return;
        }

        // Fallback: fetch with keepalive
        try {
            fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: payload,
                keepalive: true,
            });
        } catch (e) {
            console.error('[Traffic-Guard] 이탈 요청 전송 실패:', e);
        }
    }

    /**
     * beforeunload/pagehide 이벤트에서의 sendBeacon 전송을 억제합니다.
     *
     * 용도: 대기 완료 → 서비스 페이지 리다이렉트 직전에 호출하여
     * 방금 등록한 액티브 세션이 삭제되지 않도록 합니다.
     */
    function suppressBeacon() {
        beaconSuppressed = true;
        console.log('[Traffic-Guard] sendBeacon 억제 활성화 (정상 전환 모드)');
    }

    // =====================================================================
    // 4. 명시적 로그아웃
    // =====================================================================

    /**
     * 하트비트를 중단하고 /api/logout API를 호출하여 서버 세션을 정리한 후,
     * 클라이언트 측 저장된 식별자(localStorage, Cookie)도 삭제합니다.
     */
    async function logout() {
        const userId = getOrCreateUserId();
        stopHeartbeat();
        suppressBeacon(); // 로그아웃 처리 중 beforeunload 이중 호출 방지
        try {
            await fetch(CONFIG.LOGOUT_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId }),
            });
            console.log('[Traffic-Guard] 명시적 로그아웃 완료');
        } catch (e) {
            console.error('[Traffic-Guard] 로그아웃 API 오류:', e);
        } finally {
            try {
                localStorage.removeItem(CONFIG.STORAGE_KEY);
            } catch (e) {
                /* ignore */
            }
            document.cookie =
                CONFIG.COOKIE_KEY + '=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
            currentUserId = null;
        }
    }

    // =====================================================================
    // 5. 브라우저 이탈 이벤트 등록
    // =====================================================================

    function setupUnloadListeners() {
        // pagehide: bfcache 대응 — 실제 종료/이탈 시(persisted=false)만 처리
        window.addEventListener('pagehide', function (event) {
            if (!event.persisted) {
                sendLeaveBeacon();
            }
        });

        // beforeunload: 탭 닫기, 브라우저 종료, 주소창 이동 등
        window.addEventListener('beforeunload', function () {
            sendLeaveBeacon();
        });
    }

    // =====================================================================
    // 자동 초기화 (스크립트 로드 시 즉시 실행)
    // =====================================================================
    getOrCreateUserId();
    startHeartbeat();
    setupUnloadListeners();

    // =====================================================================
    // 전역 네임스페이스 노출
    // =====================================================================
    window.TrafficGuard = {
        getUserId: getOrCreateUserId,
        sendHeartbeat: sendHeartbeat,
        startHeartbeat: startHeartbeat,
        stopHeartbeat: stopHeartbeat,
        leave: sendLeaveBeacon,
        suppressBeacon: suppressBeacon,
        logout: logout,
    };
})(window);
