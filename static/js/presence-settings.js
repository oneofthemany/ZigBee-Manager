/* ============================================================
   Presence Users Settings UI
   ============================================================
   Renders into a DOM container of your choice (id='presence-settings-host'
   by default). Drop the markup in index.html and call initPresenceSettings().
   ============================================================ */

(function () {
    'use strict';

    var HOST_ID = 'presence-settings-host';

    var users = [];

    async function fetchUsers() {
        try {
            var r = await fetch('/api/presence/users');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            var data = await r.json();
            users = data.users || [];
        } catch (e) {
            console.error('[Presence] failed to fetch users', e);
            users = [];
        }
    }

    function fmtState(state) {
        if (!state) return '<span class="badge bg-secondary">unknown</span>';
        var p = state.presence || 'unknown';
        var color = p === 'home' ? 'success'
                  : p === 'away' ? 'secondary'
                  : 'warning';
        return '<span class="badge bg-' + color + '">' + p + '</span>';
    }

    function fmtDistance(d) {
        if (d == null) return '—';
        if (d < 1000) return Math.round(d) + ' m';
        return (d / 1000).toFixed(2) + ' km';
    }

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;

        var prefs = (window.zmmPresence && window.zmmPresence.getPrefs())
                    || { enabled: false, userId: '', highAccuracy: false };

        var rows = users.map(function (u) {
            return '' +
                '<tr>' +
                  '<td>' + escape(u.display_name) + '</td>' +
                  '<td><code>' + escape(u.user_id) + '</code></td>' +
                  '<td>' + fmtState(u.state) + '</td>' +
                  '<td>' + fmtDistance(u.state && u.state.distance_m) + '</td>' +
                  '<td>' + (u.home_lat != null ? u.home_lat.toFixed(5) + ', ' + u.home_lon.toFixed(5) : '<em>not set</em>') + '</td>' +
                  '<td>' + Math.round(u.radius_m) + ' m</td>' +
                  '<td class="text-end">' +
                    '<button class="btn btn-sm btn-outline-primary me-1" data-action="edit" data-id="' + escape(u.user_id) + '">' +
                      '<i class="bi bi-pencil"></i></button>' +
                    '<button class="btn btn-sm btn-outline-danger" data-action="delete" data-id="' + escape(u.user_id) + '">' +
                      '<i class="bi bi-trash"></i></button>' +
                  '</td>' +
                '</tr>';
        }).join('');

        host.innerHTML = '' +
        '<div class="card mb-3">' +
          '<div class="card-header d-flex justify-content-between align-items-center">' +
            '<strong><i class="bi bi-geo-alt"></i> Presence — Users</strong>' +
            '<button class="btn btn-sm btn-primary" id="presence-add-btn">' +
              '<i class="bi bi-plus-lg"></i> Add User</button>' +
          '</div>' +
          '<div class="card-body p-0">' +
            (users.length === 0
              ? '<div class="text-center text-muted py-4">No users configured. Add one to start tracking presence.</div>'
              : '<table class="table table-sm mb-0"><thead class="table-light"><tr>' +
                  '<th>Name</th><th>ID</th><th>State</th><th>Distance</th><th>Home</th><th>Radius</th><th></th>' +
                '</tr></thead><tbody>' + rows + '</tbody></table>') +
          '</div>' +
        '</div>' +
        // This-device opt-in
        '<div class="card mb-3">' +
          '<div class="card-header"><strong><i class="bi bi-phone"></i> This Device — Location Tracking</strong></div>' +
          '<div class="card-body">' +
            '<p class="text-muted small mb-3">' +
              'Only enable this on a phone that should report your presence. ' +
              'Browser geolocation only works while the page is open in the foreground.' +
            '</p>' +
            '<div class="row g-3">' +
              '<div class="col-md-5">' +
                '<label class="form-label small fw-bold">Report as user</label>' +
                '<select class="form-select form-select-sm" id="presence-this-user">' +
                  '<option value="">— select —</option>' +
                  users.map(function (u) {
                    return '<option value="' + escape(u.user_id) + '"' +
                      (prefs.userId === u.user_id ? ' selected' : '') + '>' +
                      escape(u.display_name) + ' (' + escape(u.user_id) + ')</option>';
                  }).join('') +
                '</select>' +
              '</div>' +
              '<div class="col-md-3">' +
                '<label class="form-label small fw-bold">High accuracy</label>' +
                '<div class="form-check form-switch">' +
                  '<input class="form-check-input" type="checkbox" id="presence-high-accuracy"' +
                    (prefs.highAccuracy ? ' checked' : '') + '>' +
                  '<label class="form-check-label small text-muted">Battery-heavy</label>' +
                '</div>' +
              '</div>' +
              '<div class="col-md-4">' +
                '<label class="form-label small fw-bold">Tracking</label>' +
                '<div class="form-check form-switch">' +
                  '<input class="form-check-input" type="checkbox" id="presence-enabled"' +
                    (prefs.enabled ? ' checked' : '') + '>' +
                  '<label class="form-check-label small" id="presence-enabled-label">' +
                    (prefs.enabled ? 'Enabled' : 'Disabled') + '</label>' +
                '</div>' +
              '</div>' +
            '</div>' +
            '<div class="mt-3 small text-muted" id="presence-status"></div>' +
          '</div>' +
        '</div>';

        bindHandlers();
        refreshStatus();
    }

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function bindHandlers() {
        var addBtn = document.getElementById('presence-add-btn');
        if (addBtn) addBtn.onclick = function () { openEditor(null); };

        document.querySelectorAll('[data-action="edit"]').forEach(function (b) {
            b.onclick = function () { openEditor(b.getAttribute('data-id')); };
        });
        document.querySelectorAll('[data-action="delete"]').forEach(function (b) {
            b.onclick = function () { deleteUser(b.getAttribute('data-id')); };
        });

        var sel = document.getElementById('presence-this-user');
        var hi  = document.getElementById('presence-high-accuracy');
        var en  = document.getElementById('presence-enabled');
        var lbl = document.getElementById('presence-enabled-label');

        function syncPrefs() {
            if (!window.zmmPresence) return;
            window.zmmPresence.savePrefs({
                enabled: !!(en && en.checked),
                userId: sel ? sel.value : '',
                highAccuracy: !!(hi && hi.checked),
                minIntervalMs: 60000,
                minDistanceM: 25
            });
            if (lbl) lbl.textContent = (en && en.checked) ? 'Enabled' : 'Disabled';
            refreshStatus();
        }

        if (sel) sel.onchange = syncPrefs;
        if (hi)  hi.onchange  = syncPrefs;
        if (en)  en.onchange  = function () {
            if (en.checked) {
                window.zmmPresence.requestPermission().then(function (state) {
                    if (state !== 'granted') {
                        en.checked = false;
                        if (window.toast) window.toast.error(
                            'Location permission denied. Allow it in your browser settings.'
                        );
                    }
                    syncPrefs();
                });
            } else {
                syncPrefs();
            }
        };
    }

    function refreshStatus() {
        var el = document.getElementById('presence-status');
        if (!el || !window.zmmPresence) return;
        var s = window.zmmPresence.status();
        el.textContent = s.watching
            ? 'Watching position. Last fix: ' +
              (s.lastReport.ts ? new Date(s.lastReport.ts).toLocaleTimeString() : '—')
            : 'Not currently watching.';
    }

    // ----------------------------------------------------------
    // Editor modal
    // ----------------------------------------------------------
    function openEditor(userId) {
        var existing = userId
            ? users.find(function (u) { return u.user_id === userId; })
            : null;

        var u = existing || {
            user_id: '', display_name: '',
            home_lat: null, home_lon: null,
            radius_m: 100, hysteresis_m: 30,
            stale_after_s: 1800, min_accuracy_m: 250,
            enabled: true,
            owntracks_user: '', owntracks_device: ''
        };

        var modalHtml =
        '<div class="modal fade" id="presenceEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header">' +
                '<h5 class="modal-title">' + (existing ? 'Edit' : 'Add') + ' Presence User</h5>' +
                '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body">' +
                '<div class="row g-3">' +
                  field('user_id', 'User ID', u.user_id, 'col-md-6',
                        existing ? 'readonly' : '',
                        'Lowercase, alphanumeric/underscore. Stable identifier.') +
                  field('display_name', 'Display Name', u.display_name, 'col-md-6') +
                  field('home_lat', 'Home Latitude', u.home_lat, 'col-md-5', '', '', 'number', 'any') +
                  field('home_lon', 'Home Longitude', u.home_lon, 'col-md-5', '', '', 'number', 'any') +
                  '<div class="col-md-2 d-flex align-items-end">' +
                    '<button class="btn btn-outline-primary btn-sm w-100" id="presence-use-current">' +
                      '<i class="bi bi-crosshair"></i> Use my location</button>' +
                  '</div>' +
                  field('radius_m', 'Geofence radius (m)', u.radius_m, 'col-md-4', '', '', 'number') +
                  field('hysteresis_m', 'Leave-hysteresis (m)', u.hysteresis_m, 'col-md-4', '', '', 'number') +
                  field('min_accuracy_m', 'Min accuracy (m)', u.min_accuracy_m, 'col-md-4', '', '', 'number') +
                  '<div class="col-12"><hr><h6 class="mb-2">OwnTracks (optional)</h6>' +
                    '<p class="text-muted small">Set if you also use the OwnTracks mobile app for reliable background tracking.</p>' +
                  '</div>' +
                  field('owntracks_user', 'OwnTracks user', u.owntracks_user || '', 'col-md-6') +
                  field('owntracks_device', 'OwnTracks device', u.owntracks_device || '', 'col-md-6') +
                '</div>' +
                '<div id="presence-edit-error" class="alert alert-danger mt-3" style="display:none"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button type="button" class="btn btn-primary" id="presence-save">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('presenceEditModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        var modalEl = document.getElementById('presenceEditModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('presence-use-current').onclick = async function () {
            try {
                var pos = await window.zmmPresence.getCurrentPosition();
                document.getElementById('field-home_lat').value = pos.lat.toFixed(6);
                document.getElementById('field-home_lon').value = pos.lon.toFixed(6);
                if (window.toast) {
                    window.toast.success('Captured location (±' + Math.round(pos.accuracy) + ' m)');
                }
            } catch (e) {
                if (window.toast) window.toast.error('Could not get location: ' + (e.message || e));
            }
        };

        document.getElementById('presence-save').onclick = async function () {
            var body = {
                user_id: val('user_id') || u.user_id,
                display_name: val('display_name'),
                home_lat: numOrNull('home_lat'),
                home_lon: numOrNull('home_lon'),
                radius_m: parseFloat(val('radius_m')) || 100,
                hysteresis_m: parseFloat(val('hysteresis_m')) || 30,
                stale_after_s: u.stale_after_s,
                min_accuracy_m: parseFloat(val('min_accuracy_m')) || 250,
                enabled: true,
                owntracks_user: val('owntracks_user') || null,
                owntracks_device: val('owntracks_device') || null
            };

            try {
                var r = await fetch('/api/presence/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!r.ok) {
                    var err = await r.json().catch(function () { return {}; });
                    showError(err.detail || ('HTTP ' + r.status));
                    return;
                }
                modal.hide();
                await fetchUsers();
                render();
                if (window.toast) window.toast.success('Presence user saved');
            } catch (e) {
                showError(e.message || String(e));
            }
        };
    }

    function field(id, label, value, col, readonly, hint, type, step) {
        return '' +
        '<div class="' + (col || 'col-md-6') + '">' +
          '<label class="form-label small fw-bold">' + label + '</label>' +
          '<input id="field-' + id + '" type="' + (type || 'text') + '"' +
            (step ? ' step="' + step + '"' : '') +
            ' class="form-control form-control-sm" value="' + escape(value == null ? '' : value) + '"' +
            (readonly ? ' readonly' : '') + '>' +
          (hint ? '<div class="form-text small">' + hint + '</div>' : '') +
        '</div>';
    }

    function val(id) {
        var el = document.getElementById('field-' + id);
        return el ? el.value.trim() : '';
    }

    function numOrNull(id) {
        var v = val(id);
        if (v === '') return null;
        var n = parseFloat(v);
        return isFinite(n) ? n : null;
    }

    function showError(msg) {
        var el = document.getElementById('presence-edit-error');
        if (el) { el.textContent = msg; el.style.display = ''; }
    }

    async function deleteUser(userId) {
        if (!confirm('Delete user "' + userId + '"? This removes the virtual presence device and any rules using it become orphaned.')) return;
        try {
            var r = await fetch('/api/presence/users/' + encodeURIComponent(userId), { method: 'DELETE' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            await fetchUsers();
            render();
            if (window.toast) window.toast.success('User deleted');
        } catch (e) {
            if (window.toast) window.toast.error('Delete failed: ' + (e.message || e));
        }
    }

    // ----------------------------------------------------------
    // Public init
    // ----------------------------------------------------------
    window.initPresenceSettings = async function () {
        await fetchUsers();
        render();
    };

    // Live updates from WebSocket
    window.handlePresenceUpdate = function (payload) {
        // Find user by ieee and patch state
        var ieee = payload && payload.ieee;
        if (!ieee) return;
        var u = users.find(function (x) { return ('user::' + x.user_id) === ieee; });
        if (!u) return;
        u.state = payload.state || u.state;
        u.last_seen = payload.last_seen || u.last_seen;
        // Cheap re-render — only the table changes frequently
        render();
    };
})();