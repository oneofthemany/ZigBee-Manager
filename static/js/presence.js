/* ============================================================
   ZigBee Matter Manager — PWA Geolocation / Presence Tracking
   ============================================================
   Drop-in module that:
     - Reads the user's PWA-presence prefs from localStorage
     - Starts navigator.geolocation.watchPosition() while the page
       is visible (browser geolocation only fires in the foreground)
     - Debounces fixes and reports them to /api/presence/users/{user_id}/fix
     - Surfaces transitions via the existing toast system
   ============================================================ */

(function () {
    'use strict';

    var PREFS_KEY = 'zbm-presence-prefs';

    // Defaults — updated via Settings UI
    var defaultPrefs = {
        enabled: false,
        userId: '',                    // must match a configured user
        highAccuracy: false,           // false = lower battery drain
        minIntervalMs: 60 * 1000,      // throttle reports
        minDistanceM: 25               // ignore tiny fixes < this many metres movement
    };

    var watchId = null;
    var lastReport = { ts: 0, lat: null, lon: null, presence: null };
    var visibilityHooked = false;

    // ----------------------------------------------------------
    // Prefs
    // ----------------------------------------------------------
    function getPrefs() {
        try {
            var raw = localStorage.getItem(PREFS_KEY);
            if (raw) return Object.assign({}, defaultPrefs, JSON.parse(raw));
        } catch (e) {}
        return Object.assign({}, defaultPrefs);
    }

    function savePrefs(prefs) {
        try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {}
    }

    // ----------------------------------------------------------
    // Distance helper (haversine, metres)
    // ----------------------------------------------------------
    function haversineM(lat1, lon1, lat2, lon2) {
        if (lat1 == null || lon1 == null) return Infinity;
        var R = 6371000;
        var toRad = function (x) { return x * Math.PI / 180; };
        var dLat = toRad(lat2 - lat1);
        var dLon = toRad(lon2 - lon1);
        var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2);
        return 2 * R * Math.asin(Math.sqrt(a));
    }

    // ----------------------------------------------------------
    // Reporting
    // ----------------------------------------------------------
    async function reportFix(coords) {
        var prefs = getPrefs();
        if (!prefs.enabled || !prefs.userId) return;

        var now = Date.now();

        // Throttle by time
        if (now - lastReport.ts < prefs.minIntervalMs) return;

        // Throttle by distance
        var moved = haversineM(lastReport.lat, lastReport.lon,
                               coords.latitude, coords.longitude);
        if (lastReport.lat != null && moved < prefs.minDistanceM) return;

        var body = {
            lat: coords.latitude,
            lon: coords.longitude,
            accuracy: coords.accuracy,
            timestamp: Math.floor(now / 1000)
        };

        try {
            var token = (window.zmmAuth && window.zmmAuth.bearerToken) || null;
            var headers = { 'Content-Type': 'application/json' };
            if (token) headers['Authorization'] = 'Bearer ' + token;

            var resp = await fetch('/api/presence/users/' + encodeURIComponent(prefs.userId) + '/fix', {
                method: 'POST',
                headers: headers,
                credentials: 'same-origin',          // also send the cookie
                body: JSON.stringify(body),
            });

            if (!resp.ok) {
                console.warn('[Presence] fix rejected', resp.status);
                return;
            }
            var data = await resp.json();
            lastReport = {
                ts: now,
                lat: coords.latitude,
                lon: coords.longitude,
                presence: data.presence
            };
            // Surface transitions only — not every periodic fix
            if (data.presence && data.presence !== lastReport.presence) {
                if (window.toast) {
                    var msg = 'Presence: ' + data.presence;
                    if (data.presence === 'home') window.toast.success(msg);
                    else window.toast.info(msg);
                }
            }
        } catch (e) {
            console.warn('[Presence] report failed', e);
        }
    }

    // ----------------------------------------------------------
    // watchPosition lifecycle
    // ----------------------------------------------------------
    function start() {
        if (!('geolocation' in navigator)) {
            console.warn('[Presence] Geolocation not supported');
            return false;
        }
        if (watchId !== null) return true;

        var prefs = getPrefs();
        watchId = navigator.geolocation.watchPosition(
            function (pos) { reportFix(pos.coords); },
            function (err) { console.warn('[Presence] geo error', err.code, err.message); },
            {
                enableHighAccuracy: !!prefs.highAccuracy,
                maximumAge: 30 * 1000,
                timeout: 30 * 1000
            }
        );
        console.log('[Presence] watchPosition started, id=' + watchId);
        return true;
    }

    function stop() {
        if (watchId !== null) {
            try { navigator.geolocation.clearWatch(watchId); } catch (e) {}
            watchId = null;
            console.log('[Presence] watchPosition stopped');
        }
    }

    function restart() { stop(); start(); }

    function hookVisibility() {
        if (visibilityHooked) return;
        visibilityHooked = true;
        document.addEventListener('visibilitychange', function () {
            var prefs = getPrefs();
            if (!prefs.enabled) return;
            if (document.visibilityState === 'visible') start();
            // We deliberately leave watchPosition running when hidden;
            // the browser will throttle/stop it on its own. Forcibly
            // stopping here causes a thrashing pattern on tab switches.
        });
    }

    // ----------------------------------------------------------
    // One-shot fix (e.g. for "Use my current location" button)
    // ----------------------------------------------------------
    function getCurrentPosition() {
        return new Promise(function (resolve, reject) {
            if (!('geolocation' in navigator)) {
                reject(new Error('Geolocation not supported'));
                return;
            }
            navigator.geolocation.getCurrentPosition(
                function (pos) {
                    resolve({
                        lat: pos.coords.latitude,
                        lon: pos.coords.longitude,
                        accuracy: pos.coords.accuracy
                    });
                },
                function (err) { reject(err); },
                { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 }
            );
        });
    }

    // ----------------------------------------------------------
    // Permission helper
    // ----------------------------------------------------------
    async function requestPermission() {
        if (!('permissions' in navigator)) {
            // Fallback: just trigger a one-shot fix to prompt
            try {
                await getCurrentPosition();
                return 'granted';
            } catch (e) { return 'denied'; }
        }
        try {
            var status = await navigator.permissions.query({ name: 'geolocation' });
            if (status.state === 'granted') return 'granted';
            if (status.state === 'denied') return 'denied';
            // 'prompt' — trigger an explicit getCurrentPosition to surface UI
            try {
                await getCurrentPosition();
                return 'granted';
            } catch (e) { return 'denied'; }
        } catch (e) { return 'unknown'; }
    }

    // ----------------------------------------------------------
    // Public API
    // ----------------------------------------------------------
    window.zmmPresence = {
        getPrefs: getPrefs,
        savePrefs: function (p) {
            savePrefs(p);
            // React to enable/disable changes immediately
            if (p && p.enabled) start(); else stop();
        },
        start: start,
        stop: stop,
        restart: restart,
        getCurrentPosition: getCurrentPosition,
        requestPermission: requestPermission,
        // Read-only snapshot for debug UI
        status: function () {
            return {
                watching: watchId !== null,
                lastReport: Object.assign({}, lastReport)
            };
        }
    };

    // ----------------------------------------------------------
    // Bootstrap
    // ----------------------------------------------------------
    document.addEventListener('DOMContentLoaded', function () {
        hookVisibility();
        var prefs = getPrefs();
        if (prefs.enabled && prefs.userId) {
            // Only start if the page is currently visible to honour
            // the "foreground only" reality of browser geolocation
            if (document.visibilityState === 'visible') start();
        }
    });
})();