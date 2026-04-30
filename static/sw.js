/* ============================================================
   ZigBee Matter Manager — Service Worker (PWA)
   ============================================================ */

var CACHE_NAME = 'zbm-v1';

// App shell files to cache on install
var APP_SHELL = [
    '/',
    '/static/index.html',
    '/static/css/styles.css',
    '/static/css/mesh.css',
    '/static/css/debug.css',
    '/static/css/groups.css',
    '/static/css/mqtt-explorer.css',
    '/static/css/mobile.css',
    '/static/css/dark-mode.css',
    '/static/css/toasts.css',
    '/static/css/device-status.css',
    '/static/images/zigbee-manager-logo.png',
    '/static/js/presence.js',
    '/static/js/presence-settings.js'
];

// Install: cache app shell
self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            console.log('[SW] Caching app shell');
            return cache.addAll(APP_SHELL).catch(function (err) {
                // Don't fail install if some assets can't be cached
                console.warn('[SW] Some assets failed to cache:', err);
            });
        })
    );
    // Activate immediately
    self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys().then(function (names) {
            return Promise.all(
                names.filter(function (name) {
                    return name !== CACHE_NAME;
                }).map(function (name) {
                    console.log('[SW] Removing old cache:', name);
                    return caches.delete(name);
                })
            );
        })
    );
    // Take control of all pages immediately
    self.clients.claim();
});

// Fetch: network-first for API, cache-first for static assets
self.addEventListener('fetch', function (event) {
    var url = new URL(event.request.url);

    // Skip non-GET requests
    if (event.request.method !== 'GET') return;

    // Skip WebSocket upgrade requests
    if (url.protocol === 'ws:' || url.protocol === 'wss:') return;

    // API calls: network-first (always try server, fallback to cache)
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(event.request).then(function (response) {
                return response;
            }).catch(function () {
                return caches.match(event.request);
            })
        );
        return;
    }

    // Static assets: cache-first with network update
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(function (cached) {
                var fetchPromise = fetch(event.request).then(function (response) {
                    // Update cache with fresh version
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function (cache) {
                            cache.put(event.request, clone);
                        });
                    }
                    return response;
                }).catch(function () {
                    // Network failed, cached version already returned
                });

                return cached || fetchPromise;
            })
        );
        return;
    }

    // Main page: network-first
    event.respondWith(
        fetch(event.request).catch(function () {
            return caches.match('/') || caches.match('/static/index.html');
        })
    );
});

// Push notification handling
self.addEventListener('push', function (event) {
    var data = { title: 'ZigBee Manager', body: 'Device update', icon: '/static/images/zigbee-manager-logo.png' };

    try {
        if (event.data) {
            data = event.data.json();
        }
    } catch (e) {
        if (event.data) {
            data.body = event.data.text();
        }
    }

    event.waitUntil(
        self.registration.showNotification(data.title || 'ZigBee Manager', {
            body: data.body || '',
            icon: data.icon || '/static/images/zigbee-manager-logo.png',
            badge: '/static/images/zigbee-manager-logo.png',
            tag: data.tag || 'zbm-notification',
            data: data.data || {},
            vibrate: [100, 50, 100],
            requireInteraction: data.requireInteraction || false
        })
    );
});

// Notification click: focus or open the app
self.addEventListener('notificationclick', function (event) {
    event.notification.close();

    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clients) {
            // Focus existing window if open
            for (var i = 0; i < clients.length; i++) {
                if (clients[i].url.includes(self.location.origin)) {
                    return clients[i].focus();
                }
            }
            // Otherwise open new window
            return self.clients.openWindow('/');
        })
    );
});