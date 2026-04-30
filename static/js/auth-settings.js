/* ============================================================
   ZMM Auth — Users, Groups, Tokens settings panel
   ============================================================
   Renders into an element with id="auth-settings-host".
   Call window.initAuthSettings() when the Settings tab is shown.
   ============================================================ */

(function () {
    'use strict';

    var HOST_ID = 'auth-settings-host';
    var state = {
        users: [],
        groups: [],
        scopes: [],
        tokens: [],
        view: 'users',     // 'users' | 'groups' | 'tokens'
    };

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function refresh() {
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');
        try {
            if (isAdmin) {
                var [u, g, s, t] = await Promise.all([
                    fetch('/api/auth/users').then(r => r.json()),
                    fetch('/api/auth/groups').then(r => r.json()),
                    fetch('/api/auth/scopes').then(r => r.json()),
                    fetch('/api/auth/tokens').then(r => r.json()),
                ]);
                state.users = u.users || [];
                state.groups = g.groups || [];
                state.scopes = s.scopes || [];
                state.tokens = t.tokens || [];
            } else {
                // Non-admins see only their own tokens + scope reference
                var [s2, t2] = await Promise.all([
                    fetch('/api/auth/scopes').then(r => r.json()),
                    fetch('/api/auth/tokens').then(r => r.json()),
                ]);
                state.scopes = s2.scopes || [];
                state.tokens = t2.tokens || [];
                state.users = [];
                state.groups = [];
            }
        } catch (e) {
            console.error('[auth-settings] refresh failed', e);
        }
        render();
    }

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');

        var tabs = isAdmin
            ? ['users', 'groups', 'tokens']
            : ['tokens'];

        host.innerHTML =
        '<div class="card mb-3">' +
          '<div class="card-header d-flex justify-content-between align-items-center">' +
            '<strong><i class="fas fa-users-cog"></i> Users, Groups & Tokens</strong>' +
            (auth && auth.whoami() ?
              '<span class="text-muted small">Signed in as <code>' + escape(auth.whoami().username) + '</code> ' +
              '<button class="btn btn-link btn-sm p-0 ms-2" id="auth-logout-link">log out</button></span>' : '') +
          '</div>' +
          '<div class="card-body">' +
            '<ul class="nav nav-tabs mb-3">' +
              tabs.map(function (t) {
                  return '<li class="nav-item">' +
                    '<a class="nav-link' + (state.view === t ? ' active' : '') + '" ' +
                    'data-tab="' + t + '" href="#">' +
                    t.charAt(0).toUpperCase() + t.slice(1) + '</a></li>';
              }).join('') +
            '</ul>' +
            '<div id="auth-tab-body"></div>' +
          '</div>' +
        '</div>';

        var ll = document.getElementById('auth-logout-link');
        if (ll) ll.onclick = function (e) { e.preventDefault(); auth.logout(); };

        document.querySelectorAll('[data-tab]').forEach(function (a) {
            a.onclick = function (e) {
                e.preventDefault();
                state.view = a.getAttribute('data-tab');
                render();
            };
        });

        var body = document.getElementById('auth-tab-body');
        if (state.view === 'users') body.innerHTML = renderUsers();
        else if (state.view === 'groups') body.innerHTML = renderGroups();
        else body.innerHTML = renderTokens();

        bindActions();
    }

    // ----------------------------------------------------------
    // Users tab
    // ----------------------------------------------------------
    function renderUsers() {
        var rows = state.users.map(function (u) {
            return '<tr>' +
              '<td><strong>' + escape(u.username) + '</strong>' +
                (u.disabled ? ' <span class="badge bg-secondary">disabled</span>' : '') +
                (u.has_password ? '' : ' <span class="badge bg-warning text-dark">no password</span>') +
              '</td>' +
              '<td>' + (u.groups || []).map(function (g) {
                  return '<span class="badge bg-info me-1">' + escape(g) + '</span>';
              }).join('') + '</td>' +
              '<td><small>' + (u.effective_scopes || []).slice(0, 4).map(escape).join(', ') +
                  ((u.effective_scopes || []).length > 4 ? ' …' : '') +
              '</small></td>' +
              '<td><small class="text-muted">' + escape(u.description || '') + '</small></td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-primary me-1" data-action="user-edit" data-id="' + escape(u.username) + '">' +
                  '<i class="fas fa-edit"></i></button>' +
                '<button class="btn btn-sm btn-outline-danger" data-action="user-delete" data-id="' + escape(u.username) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="user-new">' +
            '<i class="fas fa-plus"></i> New User</button></div>' +
            '<table class="table table-sm align-middle">' +
            '<thead class="table-light"><tr><th>Username</th><th>Groups</th>' +
            '<th>Scopes (effective)</th><th>Description</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    // ----------------------------------------------------------
    // Groups tab
    // ----------------------------------------------------------
    function renderGroups() {
        var rows = state.groups.map(function (g) {
            return '<tr>' +
              '<td><strong>' + escape(g.name) + '</strong></td>' +
              '<td>' + (g.scopes || []).map(function (s) {
                  return '<span class="badge bg-secondary me-1">' + escape(s) + '</span>';
              }).join('') + '</td>' +
              '<td><small class="text-muted">' + escape(g.description || '') + '</small></td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-primary me-1" data-action="group-edit" data-id="' + escape(g.name) + '">' +
                  '<i class="fas fa-edit"></i></button>' +
                '<button class="btn btn-sm btn-outline-danger" data-action="group-delete" data-id="' + escape(g.name) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="group-new">' +
            '<i class="fas fa-plus"></i> New Group</button></div>' +
            '<table class="table table-sm align-middle">' +
            '<thead class="table-light"><tr><th>Name</th><th>Scopes</th>' +
            '<th>Description</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    // ----------------------------------------------------------
    // Tokens tab
    // ----------------------------------------------------------
    function renderTokens() {
        var rows = state.tokens.map(function (t) {
            var status = t.revoked
                ? '<span class="badge bg-danger">revoked</span>'
                : (t.expires_at && t.expires_at * 1000 < Date.now()
                    ? '<span class="badge bg-secondary">expired</span>'
                    : '<span class="badge bg-success">active</span>');
            var lastUsed = t.last_used_at
                ? new Date(t.last_used_at * 1000).toLocaleString()
                : '—';
            var expires = t.expires_at
                ? new Date(t.expires_at * 1000).toLocaleDateString()
                : 'never';
            return '<tr>' +
              '<td><strong>' + escape(t.label) + '</strong>' +
                (t.device_id ? '<br><small class="text-muted">device: ' + escape(t.device_id) + '</small>' : '') +
              '</td>' +
              '<td>' + escape(t.user) + '</td>' +
              '<td>' + (t.scopes || []).map(function (s) {
                  return '<span class="badge bg-secondary me-1">' + escape(s) + '</span>';
              }).join('') + '</td>' +
              '<td><small>' + lastUsed + '</small></td>' +
              '<td><small>' + expires + '</small></td>' +
              '<td>' + status + '</td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-danger" data-action="token-revoke" data-id="' + escape(t.id) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="token-new">' +
            '<i class="fas fa-plus"></i> Issue Token</button></div>' +
            '<table class="table table-sm align-middle">' +
            '<thead class="table-light"><tr><th>Label</th><th>User</th>' +
            '<th>Scopes</th><th>Last Used</th><th>Expires</th><th>Status</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>' +
            (state.tokens.length === 0
              ? '<p class="text-muted text-center">No tokens issued yet.</p>'
              : '');
    }

    // ----------------------------------------------------------
    // Actions
    // ----------------------------------------------------------
    function bindActions() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        host.querySelectorAll('[data-action]').forEach(function (el) {
            el.onclick = function () {
                var a = el.getAttribute('data-action');
                var id = el.getAttribute('data-id');
                if (a === 'user-new') openUserModal(null);
                else if (a === 'user-edit') openUserModal(id);
                else if (a === 'user-delete') deleteUser(id);
                else if (a === 'group-new') openGroupModal(null);
                else if (a === 'group-edit') openGroupModal(id);
                else if (a === 'group-delete') deleteGroup(id);
                else if (a === 'token-new') openTokenModal();
                else if (a === 'token-revoke') revokeToken(id);
            };
        });
    }

    async function deleteUser(username) {
        if (!confirm('Delete user "' + username + '"? This revokes all their tokens.')) return;
        var r = await fetch('/api/auth/users/' + encodeURIComponent(username),
            { method: 'DELETE' });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            alert('Delete failed: ' + (e.detail || r.status));
            return;
        }
        await refresh();
    }

    async function deleteGroup(name) {
        if (!confirm('Delete group "' + name + '"? Members will lose its scopes.')) return;
        var r = await fetch('/api/auth/groups/' + encodeURIComponent(name),
            { method: 'DELETE' });
        if (!r.ok) { alert('Delete failed'); return; }
        await refresh();
    }

    async function revokeToken(id) {
        if (!confirm('Revoke this token? The device using it will lose access immediately.')) return;
        var r = await fetch('/api/auth/tokens/' + encodeURIComponent(id),
            { method: 'DELETE' });
        if (!r.ok) { alert('Revoke failed'); return; }
        await refresh();
    }

    // ----------------------------------------------------------
    // User modal
    // ----------------------------------------------------------
    function openUserModal(username) {
        var existing = username
            ? state.users.find(function (u) { return u.username === username; })
            : null;
        var u = existing || {
            username: '', groups: [], extra_scopes: [],
            disabled: false, description: '', has_password: false,
        };

        var groupCheckboxes = state.groups.map(function (g) {
            var checked = (u.groups || []).indexOf(g.name) !== -1;
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="ugrp-' + escape(g.name) + '" value="' + escape(g.name) + '"' +
                (checked ? ' checked' : '') + '>' +
              '<label class="form-check-label" for="ugrp-' + escape(g.name) + '">' +
                escape(g.name) + ' <small class="text-muted">' + escape(g.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="userEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header">' +
                '<h5 class="modal-title">' + (existing ? 'Edit' : 'New') + ' User</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body">' +
                '<div class="mb-3"><label class="form-label">Username</label>' +
                  '<input id="uname" class="form-control" value="' + escape(u.username) + '"' +
                    (existing ? ' readonly' : '') + '></div>' +
                '<div class="mb-3"><label class="form-label">Password ' +
                  (existing ? '(leave blank to keep)' : '') + '</label>' +
                  '<input id="upass" type="password" class="form-control" autocomplete="new-password"></div>' +
                '<div class="mb-3"><label class="form-label">Groups</label>' +
                  '<div>' + groupCheckboxes + '</div></div>' +
                '<div class="mb-3"><label class="form-label">Extra scopes (comma-separated)</label>' +
                  '<input id="uscopes" class="form-control" value="' +
                    escape((u.extra_scopes || []).join(', ')) + '">' +
                  '<div class="form-text">Direct grants beyond group membership.</div></div>' +
                '<div class="mb-3"><label class="form-label">Description</label>' +
                  '<input id="udesc" class="form-control" value="' + escape(u.description || '') + '"></div>' +
                (existing ? '<div class="form-check"><input id="udisabled" type="checkbox" class="form-check-input"' +
                    (u.disabled ? ' checked' : '') + '><label class="form-check-label">Disabled</label></div>' : '') +
                '<div id="user-edit-error" class="alert alert-danger mt-3" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-primary" id="user-save-btn">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('userEditModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('userEditModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('user-save-btn').onclick = async function () {
            var groups = [];
            document.querySelectorAll('#userEditModal input[type=checkbox][id^=ugrp-]').forEach(function (cb) {
                if (cb.checked) groups.push(cb.value);
            });
            var scopes = document.getElementById('uscopes').value
                .split(',').map(function (s) { return s.trim(); })
                .filter(Boolean);
            var body = {
                groups: groups,
                extra_scopes: scopes,
                description: document.getElementById('udesc').value,
            };
            var pw = document.getElementById('upass').value;
            if (pw) body.password = pw;
            if (existing) {
                var dis = document.getElementById('udisabled');
                if (dis) body.disabled = dis.checked;
                var r = await fetch('/api/auth/users/' + encodeURIComponent(u.username),
                    { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r.ok) {
                    var e = await r.json().catch(function () { return {}; });
                    showErr('user-edit-error', e.detail || 'Update failed');
                    return;
                }
            } else {
                body.username = document.getElementById('uname').value.trim();
                if (pw) body.password = pw;
                var r2 = await fetch('/api/auth/users',
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r2.ok) {
                    var e2 = await r2.json().catch(function () { return {}; });
                    showErr('user-edit-error', e2.detail || 'Create failed');
                    return;
                }
            }
            modal.hide();
            await refresh();
        };
    }

    // ----------------------------------------------------------
    // Group modal
    // ----------------------------------------------------------
    function openGroupModal(name) {
        var existing = name
            ? state.groups.find(function (g) { return g.name === name; })
            : null;
        var g = existing || { name: '', scopes: [], description: '' };

        var scopeOpts = state.scopes.map(function (s) {
            var checked = (g.scopes || []).indexOf(s.name) !== -1;
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="gscp-' + escape(s.name) + '" value="' + escape(s.name) + '"' +
                (checked ? ' checked' : '') + '>' +
              '<label class="form-check-label" for="gscp-' + escape(s.name) + '">' +
                '<code>' + escape(s.name) + '</code> ' +
                '<small class="text-muted">' + escape(s.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="groupEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">' +
                (existing ? 'Edit' : 'New') + ' Group</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<div class="mb-3"><label class="form-label">Name</label>' +
                  '<input id="gname" class="form-control" value="' + escape(g.name) + '"' +
                    (existing ? ' readonly' : '') + '></div>' +
                '<div class="mb-3"><label class="form-label">Description</label>' +
                  '<input id="gdesc" class="form-control" value="' + escape(g.description || '') + '"></div>' +
                '<div class="mb-3"><label class="form-label">Scopes</label>' +
                  '<div>' + scopeOpts + '</div></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-primary" id="group-save-btn">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('groupEditModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('groupEditModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('group-save-btn').onclick = async function () {
            var scopes = [];
            document.querySelectorAll('#groupEditModal input[id^=gscp-]').forEach(function (cb) {
                if (cb.checked) scopes.push(cb.value);
            });
            var body = {
                scopes: scopes,
                description: document.getElementById('gdesc').value,
            };
            if (existing) {
                var r = await fetch('/api/auth/groups/' + encodeURIComponent(g.name),
                    { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r.ok) { alert('Update failed'); return; }
            } else {
                body.name = document.getElementById('gname').value.trim();
                var r2 = await fetch('/api/auth/groups',
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r2.ok) { alert('Create failed'); return; }
            }
            modal.hide();
            await refresh();
        };
    }

    // ----------------------------------------------------------
    // Token issue modal
    // ----------------------------------------------------------
    function openTokenModal() {
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');
        var meUsername = auth && auth.whoami() ? auth.whoami().username : '';

        var userOpts = isAdmin
            ? state.users.map(function (u) {
                  return '<option value="' + escape(u.username) + '"' +
                      (u.username === meUsername ? ' selected' : '') + '>' +
                      escape(u.username) + '</option>';
              }).join('')
            : '<option value="' + escape(meUsername) + '" selected>' + escape(meUsername) + '</option>';

        var scopeOpts = state.scopes.map(function (s) {
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="tscp-' + escape(s.name) + '" value="' + escape(s.name) + '">' +
              '<label class="form-check-label" for="tscp-' + escape(s.name) + '">' +
                '<code>' + escape(s.name) + '</code> ' +
                '<small class="text-muted">' + escape(s.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="tokenIssueModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">Issue Token</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<div class="row g-3">' +
                  '<div class="col-md-6"><label class="form-label">For user</label>' +
                    '<select id="tuser" class="form-select">' + userOpts + '</select></div>' +
                  '<div class="col-md-6"><label class="form-label">Label</label>' +
                    '<input id="tlabel" class="form-control" placeholder="e.g. Sean\'s Pixel"></div>' +
                  '<div class="col-md-6"><label class="form-label">Device ID (optional)</label>' +
                    '<input id="tdevice" class="form-control" placeholder="opaque identifier"></div>' +
                  '<div class="col-md-6"><label class="form-label">Expires in (days)</label>' +
                    '<input id="texp" type="number" min="1" max="3650" class="form-control" placeholder="leave blank for no expiry"></div>' +
                  '<div class="col-12"><label class="form-label">Scopes</label>' +
                    '<div class="form-text">Leave all unchecked to inherit the user\'s full scope set.</div>' +
                    '<div>' + scopeOpts + '</div>' +
                    '<div class="mt-2 alert alert-info small">' +
                      '<strong>Mobile presence app:</strong> only check ' +
                      '<code>presence:write</code> AND set custom scope <code>presence:write:&lt;user_id&gt;</code> below for the tightest token.' +
                    '</div>' +
                    '<input id="tcustom" class="form-control mt-2" placeholder="custom scope, e.g. presence:write:sean">' +
                  '</div>' +
                '</div>' +
                '<div id="token-issue-error" class="alert alert-danger mt-3" style="display:none;"></div>' +
                '<div id="token-issue-result" class="mt-3" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Close</button>' +
                '<button class="btn btn-primary" id="token-issue-btn">Issue</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('tokenIssueModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('tokenIssueModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('token-issue-btn').onclick = async function () {
            var scopes = [];
            document.querySelectorAll('#tokenIssueModal input[id^=tscp-]').forEach(function (cb) {
                if (cb.checked) scopes.push(cb.value);
            });
            var custom = document.getElementById('tcustom').value.trim();
            if (custom) scopes.push(custom);

            var body = {
                username: document.getElementById('tuser').value,
                label: document.getElementById('tlabel').value.trim(),
                device_id: document.getElementById('tdevice').value.trim() || null,
            };
            if (scopes.length > 0) body.scopes = scopes;
            var exp = parseInt(document.getElementById('texp').value);
            if (!isNaN(exp) && exp > 0) body.expires_in_days = exp;

            var r = await fetch('/api/auth/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                var e = await r.json().catch(function () { return {}; });
                showErr('token-issue-error', e.detail || 'Failed');
                return;
            }
            var d = await r.json();
            // Show plaintext token ONCE
            var box = document.getElementById('token-issue-result');
            box.style.display = '';
            box.innerHTML =
              '<div class="alert alert-warning"><strong>Token issued — copy it now, it will not be shown again.</strong></div>' +
              '<div class="input-group">' +
                '<input id="token-plain" class="form-control font-monospace" readonly value="' + escape(d.token) + '">' +
                '<button class="btn btn-outline-primary" id="token-copy-btn"><i class="fas fa-copy"></i> Copy</button>' +
              '</div>';
            document.getElementById('token-copy-btn').onclick = function () {
                var inp = document.getElementById('token-plain');
                inp.select();
                document.execCommand('copy');
            };
            document.getElementById('token-issue-btn').disabled = true;
            await refresh();
        };
    }

    function showErr(id, msg) {
        var el = document.getElementById(id);
        if (el) { el.textContent = msg; el.style.display = ''; }
    }

    // ----------------------------------------------------------
    // Public init
    // ----------------------------------------------------------
    window.initAuthSettings = function () {
        if (!window.zmmAuth || !window.zmmAuth.whoami()) {
            // Wait until logged in
            window.zmmAuth.onChange(function (p) {
                if (p) refresh();
            });
            return;
        }
        refresh();
    };
})();