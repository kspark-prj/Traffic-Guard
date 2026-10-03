/**
 * Traffic-Guard Client SDK (traffic_guard_client.js)
 * Production Ready - Fixed Race Conditions & Beacon Handling
 */
(function (window) {
    'use strict';

    const CONFIG = {
        STORAGE_KEY: 'tg_user_id',
        COOKIE_KEY: 'tg_user_id',
        HEARTBEAT_INTERVAL_MS: 10000,
        HEARTBEAT_ENDPOINT: '/api/heartbeat',
        LEAVE_ENDPOINT: '/api/waiting/leave',
        LOGOUT_ENDPOINT: '/api/logout',
    };

    let heartbeatTimer = null;
    let currentUserId = null;
    let beaconSuppressed = false;
    let leaveSent = false;
    let consecutiveErrors = 0;

    function getOrCreateUserId() {
        if (currentUserId) return currentUserId;

        const urlParams = new URLSearchParams(window.location.search);
        let userId = urlParams.get('user_id');

        if (!userId) {
            try {
                userId = localStorage.getItem(CONFIG.STORAGE_KEY);
            } catch (e) {
                console.warn('[Traffic-Guard] localStorage 읽기 불가:', e);
            }
        }

        if (!userId) {
            const match = document.cookie.match(
                new RegExp('(?:^|; )' + CONFIG.COOKIE_KEY + '=([^;]*)'),
            );
            if (match) {
                userId = decodeURIComponent(match[1]);
            }
        }

        if (!userId) {
            if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                userId = 'usr_' + crypto.randomUUID().replace(/-/g, '').substring(0, 16);
            } else {
                userId =
                    'usr_' + Date.now().toString(36) + Math.random().toString(36).substring(2, 9);
            }
        }

        try {
            localStorage.setItem(CONFIG.STORAGE_KEY, userId);
        } catch (e) {}

        const expires = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toUTCString();
        const isSecure = location.protocol === 'https:' ? '; Secure' : '';
        document.cookie = `${CONFIG.COOKIE_KEY}=${encodeURIComponent(userId)}; expires=${expires}; path=/; SameSite=Lax${isSecure}`;

        currentUserId = userId;
        return userId;
    }

    async function sendHeartbeat() {
        const userId = getOrCreateUserId();
        try {
            const response = await fetch(CONFIG.HEARTBEAT_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId }),
            });
            if (response.ok) {
                consecutiveErrors = 0;
            } else {
                consecutiveErrors++;
            }
        } catch (err) {
            consecutiveErrors++;
            console.warn(`[Traffic-Guard] Heartbeat 실패 (${consecutiveErrors}회 연속):`, err);
        }
    }

    function startHeartbeat() {
        beaconSuppressed = false;
        leaveSent = false;

        if (heartbeatTimer) return;
        sendHeartbeat();
        heartbeatTimer = setInterval(sendHeartbeat, CONFIG.HEARTBEAT_INTERVAL_MS);
        console.log('[Traffic-Guard] 하트비트 가동');
    }

    function stopHeartbeat() {
        if (heartbeatTimer) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
            console.log('[Traffic-Guard] 하트비트 중지');
        }
    }

    function sendLeaveBeacon() {
        if (beaconSuppressed || leaveSent) return;

        const userId = currentUserId || getOrCreateUserId();
        if (!userId) return;

        leaveSent = true;
        const payload = JSON.stringify({ user_id: userId });
        const endpoint = CONFIG.LEAVE_ENDPOINT;

        if (navigator.sendBeacon) {
            const blob = new Blob([payload], { type: 'application/json' });
            if (navigator.sendBeacon(endpoint, blob)) return;
        }

        try {
            fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: payload,
                keepalive: true,
            });
        } catch (e) {
            console.error('[Traffic-Guard] 이탈 전송 실패:', e);
        }
    }

    function suppressBeacon() {
        beaconSuppressed = true;
    }

    function explicitLeave() {
        stopHeartbeat();
        sendLeaveBeacon();
        suppressBeacon(); // 이후 unloading 이벤트에서 중복 비콘 방지
    }

    async function logout() {
        const userId = getOrCreateUserId();
        explicitLeave();
        try {
            await fetch(CONFIG.LOGOUT_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId }),
            });
        } catch (e) {
            console.error('[Traffic-Guard] 로그아웃 API 오류:', e);
        } finally {
            try {
                localStorage.removeItem(CONFIG.STORAGE_KEY);
            } catch (e) {}
            document.cookie = `${CONFIG.COOKIE_KEY}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
            currentUserId = null;
        }
    }

    function setupUnloadListeners() {
        window.addEventListener('pagehide', function (event) {
            if (!event.persisted) sendLeaveBeacon();
        });

        window.addEventListener('beforeunload', function () {
            sendLeaveBeacon();
        });

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible' && heartbeatTimer) {
                sendHeartbeat();
            }
        });
    }

    getOrCreateUserId();
    startHeartbeat();
    setupUnloadListeners();

    window.TrafficGuard = {
        getUserId: getOrCreateUserId,
        sendHeartbeat,
        startHeartbeat,
        stopHeartbeat,
        leave: explicitLeave,
        suppressBeacon,
        logout,
    };
})(window);
