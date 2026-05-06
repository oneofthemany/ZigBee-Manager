/* ============================================================
   ZMM Auth — login UX with MFA + lockout-aware error handling
   ============================================================
   Replaces the previous static/js/auth.js. Backwards compatible
   with the same window.zmmAuth public API; adds MFA flow.

   First-run note:
   When no admin user exists yet (setup wizard creates the first
   admin), we don't auto-pop the login modal — the wizard handles
   onboarding instead. After submitAccount() in the wizard, callers
   should invoke window.zmmAuth.refresh() to pick up the freshly
   issued session cookie and propagate the principal to listeners.
   ============================================================ */

(function () {
    'use strict';

    var state = {
        ready: false,
        principal: null,
        mfaChallenge: null,    // outstanding MFA challenge during 2-step
        rememberPref: true,
    };
    var listeners = [];

    // ----------------------------------------------------------
    // API
    // ----------------------------------------------------------
    async function whoami() {
        try {
            var r = await fetch('/api/auth/whoami', { credentials: 'same-origin' });
            if (!r.ok) return null;
            var d = await r.json();
            if (d.authenticated) {
                return {
                    username: d.username,
                    scopes: d.scopes || [],
                    auth_method: d.auth_method,
                    mfa: d.mfa || null,
                    is_lan: !!d.is_lan,
                };
            }
            return null;
        } catch (e) { return null; }
    }

    function notify() {
        listeners.forEach(function (fn) {
            try { fn(state.principal); } catch (e) { console.error(e); }
        });
    }

    /**
     * Re-fetches /api/auth/whoami and updates internal state.
     * Use after an out-of-band login (e.g. setup wizard creating
     * the first admin via /api/setup/create-admin) to propagate
     * the new principal to all onChange listeners without a
     * full page reload.
     */
    async function refresh() {
        var p = await whoami();
        state.principal = p;
        state.ready = true;
        notify();
        return p;
    }

    /**
     * Step 1: password login.  If MFA is required, returns
     * { mfaRequired:true, challenge:'...' } and the caller must
     * call submitMfa() with the code.
     */
    async function login(username, password, remember) {
        state.rememberPref = !!remember;
        var r = await fetch('/api/auth/login', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: username, password: password, remember: state.rememberPref,
            }),
        });
        if (r.status === 423) {
            var lockData = await r.json().catch(function () { return {}; });
            var err = new Error(lockData.detail || 'Account locked');
            err.locked = true;
            err.retryAfter = parseInt(r.headers.get('Retry-After') || '0', 10);
            throw err;
        }
        if (r.status === 403) {
            var fbody = await r.json().catch(function () { return {}; });
            var ferr = new Error(fbody.detail || 'Forbidden');
            ferr.lanOnly = true;
            throw ferr;
        }
        if (!r.ok) {
            var err1 = await r.json().catch(function () { return {}; });
            throw new Error(err1.detail || ('Login failed: ' + r.status));
        }
        var d = await r.json();
        if (d.mfa_required) {
            state.mfaChallenge = d.challenge;
            return { mfaRequired: true, challenge: d.challenge };
        }
        // Single-factor success
        state.principal = {
            username: d.username, scopes: d.scopes || [],
            auth_method: 'cookie',
        };
        state.mfaChallenge = null;
        notify();
        return { success: true };
    }

    /**
     * Step 2: present TOTP or recovery code against the challenge.
     */
    async function submitMfa(code) {
        if (!state.mfaChallenge) {
            throw new Error('No active MFA challenge');
        }
        var r = await fetch('/api/auth/login/mfa', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                challenge: state.mfaChallenge,
                code: code,
                remember: state.rememberPref,
            }),
        });
        if (r.status === 423) {
            var lockData = await r.json().catch(function () { return {}; });
            var err = new Error(lockData.detail || 'Account locked');
            err.locked = true;
            throw err;
        }
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            throw new Error(e.detail || ('MFA failed: ' + r.status));
        }
        var d = await r.json();
        state.principal = {
            username: d.username, scopes: d.scopes || [],
            auth_method: 'cookie',
        };
        state.mfaChallenge = null;
        notify();
        return { success: true };
    }

    async function logout() {
        try {
            await fetch('/api/auth/logout',
                { method: 'POST', credentials: 'same-origin' });
        } catch (e) {}
        state.principal = null;
        state.mfaChallenge = null;
        notify();
        showLoginModal();
    }

    function hasScope(scope) {
        if (!state.principal) return false;
        return scopeMatches(scope, state.principal.scopes);
    }

    function scopeMatches(required, granted) {
        var reqParts = required.split(':');
        for (var i = 0; i < granted.length; i++) {
            var g = granted[i];
            if (g === 'admin') return true;
            if (g === required) return true;
            var gp = g.split(':');
            if (gp.length > reqParts.length) continue;
            var ok = true;
            for (var j = 0; j < gp.length; j++) {
                if (gp[j] === '*') continue;
                if (gp[j] !== reqParts[j]) { ok = false; break; }
            }
            if (ok && gp.length === reqParts.length) return true;
            if (ok && gp.length < reqParts.length && gp[gp.length - 1] === '*')
                return true;
        }
        return false;
    }

    function onAuthChange(fn) {
        listeners.push(fn);
        if (state.ready) fn(state.principal);
    }

    // ----------------------------------------------------------
    // Login modal — handles both password step + MFA step
    // ----------------------------------------------------------
    function showLoginModal() {
        var prev = document.getElementById('zmmLoginModal');
        if (prev) prev.remove();

        var html =
        '<div class="modal fade" id="zmmLoginModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">' +
          '<div class="modal-dialog modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header">' +
                '<h5 class="modal-title"><i class="fas fa-lock me-2"></i>ZMM Sign In</h5>' +
              '</div>' +
              '<div class="modal-body" id="zmm-login-body">' +
                renderPasswordStep() +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';
        document.body.insertAdjacentHTML('beforeend', html);

        var modalEl = document.getElementById('zmmLoginModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        bindPasswordStep(modal, modalEl);
    }

    function renderPasswordStep() {
        return '' +
        '<div class="mb-3"><label class="form-label">Username</label>' +
          '<input id="zmm-login-user" class="form-control" autocomplete="username" autofocus></div>' +
        '<div class="mb-3"><label class="form-label">Password</label>' +
          '<input id="zmm-login-pass" type="password" class="form-control" autocomplete="current-password"></div>' +
        '<div class="form-check mb-2">' +
          '<input id="zmm-login-remember" type="checkbox" class="form-check-input" checked>' +
          '<label class="form-check-label" for="zmm-login-remember">Remember me on this browser</label>' +
        '</div>' +
        '<div id="zmm-login-error" class="alert alert-danger" style="display:none;"></div>' +
        '<button class="btn btn-primary w-100" id="zmm-login-btn">Sign In</button>';
    }

    function renderMfaStep() {
        return '' +
        '<div class="alert alert-info">' +
          '<i class="fas fa-shield-alt me-2"></i>Enter the 6-digit code from your authenticator app, ' +
          'or one of your recovery codes.' +
        '</div>' +
        '<div class="mb-3"><label class="form-label">Code</label>' +
          '<input id="zmm-mfa-code" class="form-control form-control-lg text-center font-monospace" ' +
          'autocomplete="one-time-code" inputmode="numeric" autofocus maxlength="20"></div>' +
        '<div id="zmm-mfa-error" class="alert alert-danger" style="display:none;"></div>' +
        '<div class="d-flex gap-2">' +
          '<button class="btn btn-secondary" id="zmm-mfa-back">Back</button>' +
          '<button class="btn btn-primary flex-grow-1" id="zmm-mfa-btn">Verify</button>' +
        '</div>';
    }

    function bindPasswordStep(modal, modalEl) {
        function tryLogin() {
            var u = document.getElementById('zmm-login-user').value.trim();
            var p = document.getElementById('zmm-login-pass').value;
            var r = document.getElementById('zmm-login-remember').checked;
            var err = document.getElementById('zmm-login-error');
            err.style.display = 'none';
            var btn = document.getElementById('zmm-login-btn');
            btn.disabled = true;

            login(u, p, r).then(function (res) {
                if (res && res.mfaRequired) {
                    // Switch the modal body to the MFA step
                    document.getElementById('zmm-login-body').innerHTML = renderMfaStep();
                    bindMfaStep(modal, modalEl);
                } else {
                    modal.hide();
                    modalEl.remove();
                    window.location.reload();
                }
            }).catch(function (e) {
                err.textContent = e.message || String(e);
                err.style.display = '';
                btn.disabled = false;
            });
        }
        document.getElementById('zmm-login-btn').onclick = tryLogin;
        document.getElementById('zmm-login-pass').addEventListener('keydown', function (e) {
            if (e.key === 'Enter') tryLogin();
        });
    }

    function bindMfaStep(modal, modalEl) {
        function trySubmit() {
            var code = document.getElementById('zmm-mfa-code').value.trim();
            var err = document.getElementById('zmm-mfa-error');
            err.style.display = 'none';
            var btn = document.getElementById('zmm-mfa-btn');
            btn.disabled = true;
            submitMfa(code).then(function () {
                modal.hide();
                modalEl.remove();
                window.location.reload();
            }).catch(function (e) {
                err.textContent = e.message || String(e);
                err.style.display = '';
                btn.disabled = false;
            });
        }
        document.getElementById('zmm-mfa-btn').onclick = trySubmit;
        document.getElementById('zmm-mfa-code').addEventListener('keydown', function (e) {
            if (e.key === 'Enter') trySubmit();
        });
        document.getElementById('zmm-mfa-back').onclick = function () {
            state.mfaChallenge = null;
            document.getElementById('zmm-login-body').innerHTML = renderPasswordStep();
            bindPasswordStep(modal, modalEl);
        };
    }

    // ----------------------------------------------------------
    // Fetch interceptor
    // ----------------------------------------------------------
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
        return origFetch(input, init).then(function (resp) {
            if (resp.status === 401) {
                var url = (typeof input === 'string') ? input : (input.url || '');
                if (url.indexOf('/api/') !== -1
                    && url.indexOf('/api/auth/login') === -1
                    && state.ready
                    && state.principal // Only trigger if currently logged in
                ) {
                    state.principal = null;
                    notify();
                    showLoginModal();
                }
            }
            return resp;
        });
    };

    // ----------------------------------------------------------
    // Public API
    // ----------------------------------------------------------
    window.zmmAuth = {
        whoami: function () { return state.principal; },
        login: login,
        submitMfa: submitMfa,
        logout: logout,
        hasScope: hasScope,
        onChange: onAuthChange,
        showLogin: showLoginModal,
        refresh: refresh,
    };

    /**
     * Boot: probe whoami, then decide what UI to surface.
     *
     *   - Authenticated:                  do nothing here (main.js inits dashboard).
     *   - Anonymous + setup needed:       do nothing — setup wizard takes over.
     *   - Anonymous + setup complete:     show login modal.
     *   - /api/setup/status unreachable:  show login modal (fail-safe).
     */
    document.addEventListener('DOMContentLoaded', async function () {
        var p = await whoami();
        state.principal = p;
        state.ready = true;
        notify();

        if (!p) {
            try {
                var s = await fetch('/api/setup/status').then(function (r) { return r.json(); });
                if (!s || !s.needs_setup) {
                    showLoginModal();
                }
            } catch (e) {
                showLoginModal();
            }
        }
    });
})();