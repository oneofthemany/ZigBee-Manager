/* ============================================================
   ZigBee Matter Manager — PWA + Browser Notifications
   ============================================================ */

(function () {
    'use strict';

    var PREFS_KEY = 'zbm-notification-prefs';

    // Default notification preferences
    var defaultPrefs = {
        enabled: false,
        deviceOffline: true,
        deviceOnline: false,
        lowBattery: true,
        thermostatReached: true,
        suppressMinutes: 5  // Don't repeat same notification within N minutes
    };

    var notifHistory = {}; // Track sent notifications to avoid spam
    var previousStates = {}; // Track previous device states for diff

    // ----------------------------------------------------------
    // 1. SERVICE WORKER REGISTRATION (PWA)
    // ----------------------------------------------------------

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) {
            console.log('[PWA] Service workers not supported');
            return;
        }

        var host = window.location.hostname;
        var isLocalhost = (host === 'localhost' || host === '127.0.0.1' || host === '::1');
if (window.location.protocol !== 'https:' && !isLocalhost) {
    console.log('[PWA] Skipping SW registration — requires HTTPS or localhost');
    return;
}

        navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then(function (reg) {
                console.log('[PWA] Service worker registered, scope:', reg.scope);
                setInterval(function () {
                    reg.update();
                }, 60 * 60 * 1000);
            })
            .catch(function (err) {
                console.warn('[PWA] Service worker registration failed:', err);
            });
    }

    // ----------------------------------------------------------
    // 2. PLATFORM DETECTION & PREFERENCES
    // ----------------------------------------------------------

    function getPrefs() {
        try {
            var stored = localStorage.getItem(PREFS_KEY);
            if (stored) return JSON.parse(stored);
        } catch (e) {}
        return Object.assign({}, defaultPrefs);
    }

    function savePrefs(prefs) {
        localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    }

    function isIOS() {
        return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
               (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    function isStandalone() {
        return window.matchMedia('(display-mode: standalone)').matches ||
               window.navigator.standalone === true;
    }

    function isAndroid() {
        return /Android/.test(navigator.userAgent);
    }

    function getNotificationSupport() {
        // Full native support
        if ('Notification' in window && 'serviceWorker' in navigator) {
            if (isIOS() && !isStandalone()) {
                return 'ios-browser'; // iOS Safari — needs PWA install first
            }
            return 'full';
        }
        // No support at all
        if (!('Notification' in window)) {
            return 'none';
        }
        // Basic support (no SW)
        return 'basic';
    }

    // ----------------------------------------------------------
    // 2b. NOTIFICATION PERMISSION
    // ----------------------------------------------------------

    async function requestPermission() {
        var support = getNotificationSupport();

        if (support === 'none') {
            if (window.toast) window.toast.warning('Notifications are not supported in this browser');
            return false;
        }

        if (support === 'ios-browser') {
            if (window.toast) {
                window.toast.info(
                    'On iOS, notifications only work when the app is installed to your home screen. ' +
                    'Tap the Share button → "Add to Home Screen", then enable notifications from within the app.',
                    { duration: 10000 }
                );
            }
            return false;
        }

        if (Notification.permission === 'granted') return true;

        if (Notification.permission === 'denied') {
            if (window.toast) {
                window.toast.error(
                    'Notifications are blocked. Open your browser settings for this site and allow notifications.',
                    { duration: 8000 }
                );
            }
            return false;
        }

        var result = await Notification.requestPermission();
        return result === 'granted';
    }

    // ----------------------------------------------------------
    // 3. SEND NOTIFICATION
    // ----------------------------------------------------------

    function sendNotification(title, body, tag, options) {
        var prefs = getPrefs();
        if (!prefs.enabled) return;

        // Suppress duplicate notifications within the cooldown window
        var key = tag || (title + ':' + body);
        var now = Date.now();
        var suppressMs = (prefs.suppressMinutes || 5) * 60 * 1000;

        if (notifHistory[key] && (now - notifHistory[key]) < suppressMs) {
            return; // Too recent, skip
        }
        notifHistory[key] = now;

        var nativeSupported = ('Notification' in window) && Notification.permission === 'granted';

        if (nativeSupported) {
            // Use service worker notification if available (works in background)
            if (navigator.serviceWorker && navigator.serviceWorker.controller) {
                navigator.serviceWorker.ready.then(function (reg) {
                    reg.showNotification(title, {
                        body: body,
                        icon: '/static/images/zigbee-manager-logo.png',
                        badge: '/static/images/zigbee-manager-logo.png',
                        tag: tag || 'zbm-' + Date.now(),
                        vibrate: [100, 50, 100],
                        requireInteraction: options && options.persistent || false,
                        data: options && options.data || {}
                    });
                });
            } else {
                // Fallback to basic Notification API
                try {
                    new Notification(title, {
                        body: body,
                        icon: '/static/images/zigbee-manager-logo.png',
                        tag: tag || 'zbm-' + Date.now()
                    });
                } catch (e) {
                    // Some mobile browsers throw on new Notification()
                    sendInAppNotification(title, body, tag);
                }
            }
        } else {
            // In-app fallback for browsers without notification support
            sendInAppNotification(title, body, tag);
        }
    }

    /**
     * In-app notification fallback — uses the toast system + audio ping
     * Works on ALL browsers including iOS Safari without PWA install
     */
    function sendInAppNotification(title, body, tag) {
        if (!window.toast) return;

        // Map notification types to toast types
        var type = 'info';
        var lower = (title + ' ' + body).toLowerCase();
        if (lower.match(/offline|error|fail/)) type = 'error';
        else if (lower.match(/online|reached|success/)) type = 'success';
        else if (lower.match(/battery|warning|low/)) type = 'warning';

        window.toast[type](title + ': ' + body, { duration: 8000 });

        // Play a subtle notification sound if available
        try {
            var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.type = 'sine';
            osc.frequency.value = type === 'error' ? 440 : type === 'warning' ? 523 : 659;
            gain.gain.value = 0.08;
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
            osc.start(audioCtx.currentTime);
            osc.stop(audioCtx.currentTime + 0.3);
        } catch (e) {
            // Audio not available, silent fallback
        }
    }

    // ----------------------------------------------------------
    // 4. DEVICE STATE MONITORING
    // ----------------------------------------------------------

    function checkDeviceState(ieee, newState, deviceName) {
        var prefs = getPrefs();
        if (!prefs.enabled) return;

        var prev = previousStates[ieee] || {};
        var name = deviceName || ieee.slice(-8);

        // --- Device offline detection ---
        if (prefs.deviceOffline && prev.available === true && newState.available === false) {
            sendNotification(
                'Device Offline',
                name + ' has gone offline',
                'offline-' + ieee,
                { persistent: false }
            );
        }

        // --- Device online detection ---
        if (prefs.deviceOnline && prev.available === false && newState.available === true) {
            sendNotification(
                'Device Online',
                name + ' is back online',
                'online-' + ieee
            );
        }

        // --- Low battery warning ---
        if (prefs.lowBattery) {
            var battery = newState.battery || newState.battery_percentage;
            var prevBattery = prev.battery || prev.battery_percentage;

            if (battery !== undefined && battery <= 15) {
                // Only notify once when crossing the threshold
                if (prevBattery === undefined || prevBattery > 15) {
                    sendNotification(
                        'Low Battery',
                        name + ' battery is at ' + battery + '%',
                        'battery-' + ieee,
                        { persistent: true }
                    );
                }
            }
        }

        // --- Thermostat target reached ---
        if (prefs.thermostatReached) {
            var target = newState.occupied_heating_setpoint || newState.heating_setpoint;
            var current = newState.internal_temperature || newState.temperature || newState.local_temperature;
            var prevCurrent = prev.internal_temperature || prev.temperature || prev.local_temperature;

            if (target && current && prevCurrent) {
                var targetNum = Number(target);
                var currentNum = Number(current);
                var prevNum = Number(prevCurrent);

                // Notify when temperature crosses the target threshold (within 0.3°C)
                if (prevNum < targetNum - 0.3 && currentNum >= targetNum - 0.3) {
                    sendNotification(
                        'Target Temperature Reached',
                        name + ' has reached ' + currentNum.toFixed(1) + '°C (target: ' + targetNum.toFixed(1) + '°C)',
                        'temp-reached-' + ieee
                    );
                }
            }
        }

        // Store current state for next comparison
        previousStates[ieee] = Object.assign({}, prev, newState);
    }

    // Expose for the WebSocket handler to call
    window.zbmCheckDeviceState = checkDeviceState;
    window.zbmSendNotification = sendNotification;

    // ----------------------------------------------------------
    // 5. HOOK INTO WEBSOCKET UPDATES
    // ----------------------------------------------------------

    function hookWebSocket() {
        // Patch the global handleDeviceUpdate if it exists
        // We watch for state.deviceCache changes via MutationObserver on the table
        // as a simpler hook that doesn't require modifying existing modules

        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) {
            setTimeout(hookWebSocket, 1000);
            return;
        }

        // Use a polling approach to check for state changes
        // This works because devices.js updates state.deviceCache on every WS message
        setInterval(function () {
            if (!window.state || !window.state.deviceCache) return;

            var cache = window.state.deviceCache;
            Object.keys(cache).forEach(function (ieee) {
                var device = cache[ieee];
                if (!device || !device.state) return;

                var stateWithMeta = Object.assign({}, device.state, {
                    available: device.available
                });

                checkDeviceState(ieee, stateWithMeta, device.friendly_name);
            });
        }, 5000); // Check every 5 seconds
    }

    // ----------------------------------------------------------
    // 6. NOTIFICATION BELL + SETTINGS PANEL
    // ----------------------------------------------------------

    function createNotificationBell() {
        var navbar = document.querySelector('.navbar .d-flex.align-items-center.gap-3');
        if (!navbar) return;

        var prefs = getPrefs();

        var btn = document.createElement('button');
        btn.id = 'zbm-notif-bell';
        btn.className = 'btn btn-sm btn-outline-light border-0';
        btn.title = 'Notification settings';
        btn.style.cssText = 'font-size: 1rem; padding: 0.25rem 0.5rem; opacity: 0.8; transition: opacity 0.2s; position: relative;';
        btn.innerHTML = prefs.enabled
            ? '<i class="fas fa-bell"></i>'
            : '<i class="fas fa-bell-slash"></i>';
        btn.onmouseenter = function () { this.style.opacity = '1'; };
        btn.onmouseleave = function () { this.style.opacity = '0.8'; };

        btn.addEventListener('click', function () {
            openNotificationSettings();
        });

        // Insert before the theme toggle if present, otherwise before pairing group
        var themeBtn = document.getElementById('themeToggleBtn');
        if (themeBtn) {
            navbar.insertBefore(btn, themeBtn);
        } else {
            var pairingGroup = navbar.querySelector('.btn-group');
            if (pairingGroup) {
                navbar.insertBefore(btn, pairingGroup);
            } else {
                navbar.appendChild(btn);
            }
        }
    }

    function updateBellIcon() {
        var btn = document.getElementById('zbm-notif-bell');
        if (!btn) return;
        var prefs = getPrefs();
        btn.innerHTML = prefs.enabled
            ? '<i class="fas fa-bell"></i>'
            : '<i class="fas fa-bell-slash"></i>';
    }

    function openNotificationSettings() {
        // Remove existing modal if present
        var existing = document.getElementById('zbm-notif-modal');
        if (existing) existing.remove();

        var prefs = getPrefs();
        var support = getNotificationSupport();
        var permissionStatus = ('Notification' in window) ? Notification.permission : 'unsupported';

        // Build platform-specific status alert
        var statusAlert = '';
        if (support === 'ios-browser') {
            statusAlert =
                '<div class="alert alert-warning small mb-3">' +
                    '<i class="fas fa-mobile-alt me-1"></i>' +
                    '<strong>iOS detected</strong> — notifications require the app to be installed on your home screen.' +
                    '<br><br>' +
                    '<strong>How to install:</strong>' +
                    '<ol class="mb-0 mt-1" style="padding-left: 1.2rem;">' +
                        '<li>Tap the <i class="fas fa-share-square"></i> <strong>Share</strong> button in Safari</li>' +
                        '<li>Scroll down and tap <strong>"Add to Home Screen"</strong></li>' +
                        '<li>Open the app from your home screen</li>' +
                        '<li>Come back here and enable notifications</li>' +
                    '</ol>' +
                '</div>';
        } else if (support === 'none') {
            statusAlert =
                '<div class="alert alert-danger small mb-3">' +
                    '<i class="fas fa-times-circle me-1"></i>' +
                    'Notifications are not supported in this browser. ' +
                    'In-app alerts (toasts with sound) will be used as a fallback.' +
                '</div>';
        } else if (permissionStatus === 'denied') {
            statusAlert =
                '<div class="alert alert-danger small mb-3">' +
                    '<i class="fas fa-times-circle me-1"></i>' +
                    'Notifications are <strong>blocked</strong> by your browser.' +
                    '<br><small>' + (isAndroid() ?
                        'Open Chrome menu → Settings → Site settings → Notifications → Allow for this site' :
                        'Open browser settings for this site and allow notifications') +
                    '</small>' +
                '</div>';
        } else if (permissionStatus === 'granted') {
            statusAlert =
                '<div class="alert alert-success small mb-3">' +
                    '<i class="fas fa-check-circle me-1"></i>' +
                    'Notifications are <strong>enabled</strong>' +
                    (isStandalone() ? ' — running as installed app' : '') +
                '</div>';
        } else {
            statusAlert =
                '<div class="alert alert-info small mb-3">' +
                    '<i class="fas fa-info-circle me-1"></i>' +
                    'Browser permission: <strong>' + permissionStatus + '</strong> — you\'ll be prompted when you enable notifications' +
                '</div>';
        }

        // In-app fallback notice
        var fallbackNotice = '';
        if (support !== 'full' && support !== 'basic') {
            fallbackNotice =
                '<div class="alert alert-info small mb-3">' +
                    '<i class="fas fa-bell me-1"></i>' +
                    '<strong>In-app mode:</strong> Notifications will appear as toast alerts with a sound ping when the app is open.' +
                '</div>';
        }

        var modal = document.createElement('div');
        modal.id = 'zbm-notif-modal';
        modal.className = 'modal fade';
        modal.tabIndex = -1;
        modal.innerHTML =
            '<div class="modal-dialog">' +
                '<div class="modal-content">' +
                    '<div class="modal-header">' +
                        '<h5 class="modal-title"><i class="fas fa-bell me-2"></i>Notification Settings</h5>' +
                        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
                    '</div>' +
                    '<div class="modal-body">' +

                        // Platform-specific status
                        statusAlert +
                        fallbackNotice +

                        // Master toggle
                        '<div class="form-check form-switch mb-3 pb-3 border-bottom">' +
                            '<input class="form-check-input" type="checkbox" id="zbm-notif-enabled" ' + (prefs.enabled ? 'checked' : '') + '>' +
                            '<label class="form-check-label fw-bold" for="zbm-notif-enabled">Enable notifications</label>' +
                        '</div>' +

                        // Individual toggles
                        '<div id="zbm-notif-options" style="' + (prefs.enabled ? '' : 'opacity:0.5;pointer-events:none;') + '">' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-offline" ' + (prefs.deviceOffline ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-offline">' +
                                    '<i class="fas fa-plug text-danger me-1"></i> Device goes offline' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-online" ' + (prefs.deviceOnline ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-online">' +
                                    '<i class="fas fa-plug text-success me-1"></i> Device comes online' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-battery" ' + (prefs.lowBattery ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-battery">' +
                                    '<i class="fas fa-battery-quarter text-warning me-1"></i> Low battery warning (&lt;15%)' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-3">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-thermostat" ' + (prefs.thermostatReached ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-thermostat">' +
                                    '<i class="fas fa-thermometer-half text-info me-1"></i> Thermostat target reached' +
                                '</label>' +
                            '</div>' +

                            // Cooldown
                            '<div class="mb-3">' +
                                '<label class="form-label small fw-bold">Suppress duplicates for</label>' +
                                '<select class="form-select form-select-sm" id="zbm-notif-suppress">' +
                                    '<option value="1" ' + (prefs.suppressMinutes === 1 ? 'selected' : '') + '>1 minute</option>' +
                                    '<option value="5" ' + (prefs.suppressMinutes === 5 ? 'selected' : '') + '>5 minutes</option>' +
                                    '<option value="15" ' + (prefs.suppressMinutes === 15 ? 'selected' : '') + '>15 minutes</option>' +
                                    '<option value="30" ' + (prefs.suppressMinutes === 30 ? 'selected' : '') + '>30 minutes</option>' +
                                    '<option value="60" ' + (prefs.suppressMinutes === 60 ? 'selected' : '') + '>1 hour</option>' +
                                '</select>' +
                            '</div>' +
                        '</div>' +

                        // Test button
                        '<button class="btn btn-outline-primary btn-sm w-100" id="zbm-notif-test">' +
                            '<i class="fas fa-paper-plane me-1"></i> Send test notification' +
                        '</button>' +

                    '</div>' +
                    '<div class="modal-footer">' +
                        '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>' +
                    '</div>' +
                '</div>' +
            '</div>';

        document.body.appendChild(modal);

        // Bind events
        var masterToggle = document.getElementById('zbm-notif-enabled');
        var optionsDiv = document.getElementById('zbm-notif-options');

        masterToggle.addEventListener('change', async function () {
            if (this.checked) {
                var support = getNotificationSupport();

                if (support === 'full' || support === 'basic') {
                    // Try to get native permission
                    var granted = await requestPermission();
                    if (!granted) {
                        // Fall back to in-app mode silently
                        if (window.toast) window.toast.info('Using in-app notifications (toast alerts with sound)');
                    }
                } else if (support === 'ios-browser') {
                    // iOS without PWA — allow in-app mode
                    if (window.toast) {
                        window.toast.info(
                            'Notifications will appear as in-app alerts. Install to home screen for native notifications.',
                            { duration: 6000 }
                        );
                    }
                }
                // Always allow enabling (in-app fallback works everywhere)
            }
            optionsDiv.style.opacity = this.checked ? '1' : '0.5';
            optionsDiv.style.pointerEvents = this.checked ? 'auto' : 'none';
            saveCurrentPrefs();
        });

        // Save on any toggle change
        ['zbm-notif-offline', 'zbm-notif-online', 'zbm-notif-battery', 'zbm-notif-thermostat', 'zbm-notif-suppress'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', saveCurrentPrefs);
        });

        // Test button
        document.getElementById('zbm-notif-test').addEventListener('click', async function () {
            var granted = await requestPermission();
            if (granted) {
                sendNotification(
                    'Test Notification',
                    'ZigBee Manager notifications are working!',
                    'test-' + Date.now()
                );
                if (window.toast) window.toast.success('Test notification sent!');
            }
        });

        var bsModal = new bootstrap.Modal(modal);
        bsModal.show();

        // Cleanup on close
        modal.addEventListener('hidden.bs.modal', function () {
            modal.remove();
        });
    }

    function saveCurrentPrefs() {
        var prefs = {
            enabled: document.getElementById('zbm-notif-enabled').checked,
            deviceOffline: document.getElementById('zbm-notif-offline').checked,
            deviceOnline: document.getElementById('zbm-notif-online').checked,
            lowBattery: document.getElementById('zbm-notif-battery').checked,
            thermostatReached: document.getElementById('zbm-notif-thermostat').checked,
            suppressMinutes: parseInt(document.getElementById('zbm-notif-suppress').value) || 5
        };
        savePrefs(prefs);
        updateBellIcon();
    }

    // ----------------------------------------------------------
    // 7. INIT
    // ----------------------------------------------------------

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            registerServiceWorker();
            setTimeout(createNotificationBell, 300);
            setTimeout(hookWebSocket, 2000);
        });
    } else {
        registerServiceWorker();
        setTimeout(createNotificationBell, 300);
        setTimeout(hookWebSocket, 2000);
    }

})();