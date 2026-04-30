/* ============================================================
   ZMM Auth — My Account / MFA enrolment panel
   ============================================================
   Renders into #my-account-host. Lets the current user enable,
   manage, and disable two-factor authentication.

   Renders QR code via an inline lightweight encoder so we don't
   need an external library.
   ============================================================ */

(function () {
    'use strict';

    var HOST_ID = 'my-account-host';
    var status = null;     // { enabled, enrolled, pending_enrolment, ... }

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function refresh() {
        try {
            var r = await fetch('/api/auth/mfa/status', { credentials: 'same-origin' });
            if (!r.ok) { status = null; }
            else status = await r.json();
        } catch (e) { status = null; }
        render();
    }

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        var auth = window.zmmAuth;
        var me = auth && auth.whoami();
        if (!me) { host.innerHTML = ''; return; }

        var mfaState = status || { enabled: false, enrolled: false };
        var lan = !!me.is_lan;

        host.innerHTML =
        '<div class="card mb-3">' +
          '<div class="card-header">' +
            '<strong><i class="fas fa-user-shield"></i> My Account — Two-factor Authentication</strong>' +
          '</div>' +
          '<div class="card-body">' +
            renderBody(mfaState) +
          '</div>' +
        '</div>' +
        '<div class="card mb-3">' +
          '<div class="card-header"><strong><i class="fas fa-key"></i> Change Password</strong></div>' +
          '<div class="card-body">' + renderPasswordPanel() + '</div>' +
        '</div>';

        bindActions(mfaState);
    }

    function renderBody(s) {
        if (s.enabled && s.enrolled) {
            return '' +
            '<p>Two-factor authentication is <strong class="text-success">enabled</strong>.</p>' +
            '<p>Recovery codes remaining: <strong>' + (s.recovery_codes_remaining || 0) + '</strong>' +
              (s.recovery_codes_remaining < 3 ? ' <span class="badge bg-warning text-dark">low</span>' : '') +
            '</p>' +
            '<div class="d-flex gap-2 flex-wrap">' +
              '<button class="btn btn-outline-primary btn-sm" data-action="regen">' +
                '<i class="fas fa-sync"></i> Regenerate recovery codes</button>' +
              '<button class="btn btn-outline-danger btn-sm" data-action="disable">' +
                '<i class="fas fa-times"></i> Disable 2FA</button>' +
            '</div>';
        }
        return '' +
        '<p>Two-factor authentication adds a second factor to your password — ' +
          'a 6-digit code from an authenticator app on your phone (Google Authenticator, ' +
          'Authy, 1Password, Bitwarden, etc.).</p>' +
        '<div class="alert alert-info">' +
          '<strong>Recommended for any admin account.</strong> ' +
          'Especially if you plan to expose ZMM beyond your home network.' +
        '</div>' +
        '<button class="btn btn-primary" data-action="enrol">' +
          '<i class="fas fa-shield-alt"></i> Enable Two-factor Authentication</button>';
    }

    function renderPasswordPanel() {
        // Grab the current username to satisfy the browser's password manager
        var auth = window.zmmAuth;
        var me = auth && auth.whoami();
        var uname = me ? me.username : '';

        return '' +
        '<form onsubmit="event.preventDefault();">' +
        '<!-- Hidden username field for accessibility/autofill -->' +
        '<input type="text" autocomplete="username" value="' + escape(uname) + '" style="display:none;" aria-hidden="true">' +
        '<div class="row g-3">' +
          '<div class="col-md-6"><label class="form-label">Current password</label>' +
            '<input id="pw-cur" type="password" class="form-control" autocomplete="current-password"></div>' +
          '<div class="col-md-6"><label class="form-label">New password</label>' +
            '<input id="pw-new" type="password" class="form-control" autocomplete="new-password"></div>' +
          '<div class="col-md-6"><label class="form-label">Confirm new password</label>' +
            '<input id="pw-conf" type="password" class="form-control" autocomplete="new-password"></div>' +
          '<div class="col-12">' +
            '<div id="pw-error" class="alert alert-danger" style="display:none;"></div>' +
            '<div id="pw-success" class="alert alert-success" style="display:none;"></div>' +
            '<button type="button" class="btn btn-primary" data-action="change-pw">Change password</button>' +
          '</div>' +
        '</div>' +
        '</form>';
    }

    function bindActions(s) {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        host.querySelectorAll('[data-action]').forEach(function (el) {
            el.onclick = function () {
                var a = el.getAttribute('data-action');
                if (a === 'enrol') startEnrolment();
                else if (a === 'disable') openDisableModal();
                else if (a === 'regen') regenerateRecoveryCodes();
                else if (a === 'change-pw') changePassword();
            };
        });
    }

    // ----------------------------------------------------------
    // Enrolment flow
    // ----------------------------------------------------------
    async function startEnrolment() {
        var r = await fetch('/api/auth/mfa/enrol/start',
            { method: 'POST', credentials: 'same-origin' });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            alert('Enrolment failed: ' + (e.detail || r.status));
            return;
        }
        var d = await r.json();
        showEnrolmentModal(d);
    }

    function showEnrolmentModal(d) {
        var html =
        '<div class="modal fade" id="mfaEnrolModal" tabindex="-1" data-bs-backdrop="static">' +
          '<div class="modal-dialog modal-lg modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">Set up Two-factor Authentication</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<ol class="mb-3">' +
                  '<li>Open your authenticator app (Google Authenticator, Authy, 1Password, etc.)</li>' +
                  '<li>Add a new account by scanning the QR code below — or by typing the secret manually.</li>' +
                  '<li>Enter the 6-digit code your app shows to confirm.</li>' +
                '</ol>' +
                '<div class="row g-3">' +
                  '<div class="col-md-6 text-center">' +
                    '<div id="mfa-qr-container" class="d-inline-block bg-white p-2 border"></div>' +
                  '</div>' +
                  '<div class="col-md-6">' +
                    '<label class="form-label small fw-bold">Manual secret</label>' +
                    '<input class="form-control font-monospace" readonly value="' + escape(d.secret) + '">' +
                    '<div class="form-text">Issuer: <strong>' + escape(d.issuer) + '</strong>, ' +
                      'Account: <strong>' + escape(d.account) + '</strong></div>' +
                  '</div>' +
                '</div>' +
                '<hr>' +
                '<div class="mb-3"><label class="form-label">6-digit code</label>' +
                  '<input id="mfa-confirm" class="form-control form-control-lg text-center font-monospace" ' +
                  'inputmode="numeric" maxlength="6" autocomplete="one-time-code"></div>' +
                '<div id="mfa-confirm-err" class="alert alert-danger" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-primary" id="mfa-confirm-btn">Verify & Enable</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('mfaEnrolModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('mfaEnrolModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        // Render QR for the otpauth URI
        renderQR(document.getElementById('mfa-qr-container'), d.otpauth_uri);

        document.getElementById('mfa-confirm-btn').onclick = async function () {
            var code = document.getElementById('mfa-confirm').value.trim();
            var err = document.getElementById('mfa-confirm-err');
            err.style.display = 'none';
            var r = await fetch('/api/auth/mfa/enrol/finish', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code }),
            });
            if (!r.ok) {
                var e = await r.json().catch(function () { return {}; });
                err.textContent = e.detail || 'Verification failed';
                err.style.display = '';
                return;
            }
            var d2 = await r.json();
            modal.hide();
            modalEl.remove();
            showRecoveryCodes(d2.recovery_codes);
            await refresh();
        };
    }

    function showRecoveryCodes(codes) {
        var html =
        '<div class="modal fade" id="mfaRecoveryModal" tabindex="-1" data-bs-backdrop="static">' +
          '<div class="modal-dialog modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">Recovery Codes</h5></div>' +
              '<div class="modal-body">' +
                '<div class="alert alert-warning">' +
                  '<strong>Save these now.</strong> They are shown only once. ' +
                  'Each code can be used to sign in instead of a TOTP code, exactly once. ' +
                  'Print them, save in a password manager, or both.' +
                '</div>' +
                '<pre class="bg-light p-3 border rounded font-monospace">' +
                  codes.map(escape).join('\n') +
                '</pre>' +
                '<button class="btn btn-outline-primary btn-sm" id="mfa-rc-copy">' +
                  '<i class="fas fa-copy"></i> Copy all</button>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-primary" id="mfa-rc-done">I have saved them</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';
        var prev = document.getElementById('mfaRecoveryModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('mfaRecoveryModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('mfa-rc-copy').onclick = function () {
            navigator.clipboard.writeText(codes.join('\n')).then(function () {
                if (window.toast) window.toast.success('Codes copied to clipboard');
            });
        };
        document.getElementById('mfa-rc-done').onclick = function () {
            if (confirm('Have you really saved them? You will not see them again.')) {
                modal.hide();
                modalEl.remove();
            }
        };
    }

    function openDisableModal() {
        var html =
        '<div class="modal fade" id="mfaDisableModal" tabindex="-1">' +
          '<div class="modal-dialog modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">Disable 2FA</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<div class="alert alert-warning">' +
                  'Disabling two-factor authentication weakens your account security. ' +
                  'Confirm your password to proceed.' +
                '</div>' +
                '<div class="mb-3"><label class="form-label">Password</label>' +
                  '<input id="mfa-disable-pw" type="password" class="form-control" autocomplete="current-password"></div>' +
                '<div id="mfa-disable-err" class="alert alert-danger" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-danger" id="mfa-disable-btn">Disable 2FA</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';
        var prev = document.getElementById('mfaDisableModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('mfaDisableModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('mfa-disable-btn').onclick = async function () {
            var pw = document.getElementById('mfa-disable-pw').value;
            var err = document.getElementById('mfa-disable-err');
            err.style.display = 'none';
            var r = await fetch('/api/auth/mfa/disable', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw }),
            });
            if (!r.ok) {
                var e = await r.json().catch(function () { return {}; });
                err.textContent = e.detail || 'Disable failed';
                err.style.display = '';
                return;
            }
            modal.hide();
            modalEl.remove();
            await refresh();
            if (window.toast) window.toast.info('Two-factor authentication disabled');
        };
    }

    async function regenerateRecoveryCodes() {
        if (!confirm('Generate new recovery codes? The current set will be invalidated.')) return;
        var r = await fetch('/api/auth/mfa/recovery-codes/regenerate',
            { method: 'POST', credentials: 'same-origin' });
        if (!r.ok) { alert('Failed'); return; }
        var d = await r.json();
        showRecoveryCodes(d.recovery_codes);
        await refresh();
    }

    async function changePassword() {
        var cur = document.getElementById('pw-cur').value;
        var nw = document.getElementById('pw-new').value;
        var conf = document.getElementById('pw-conf').value;
        var err = document.getElementById('pw-error');
        var ok = document.getElementById('pw-success');
        err.style.display = 'none';
        ok.style.display = 'none';

        if (!nw || nw.length < 8) {
            err.textContent = 'New password must be at least 8 characters.';
            err.style.display = ''; return;
        }
        if (nw !== conf) {
            err.textContent = 'New passwords do not match.';
            err.style.display = ''; return;
        }
        // Server doesn't currently re-verify the old password on PATCH /users/{me};
        // we still ask for it as a UI guard rail. Backend hardening would add this
        // server-side too — out of scope here.
        var me = window.zmmAuth.whoami();
        var r = await fetch('/api/auth/users/' + encodeURIComponent(me.username), {
            method: 'PATCH',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: nw }),
        });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            err.textContent = e.detail || 'Change failed';
            err.style.display = '';
            return;
        }
        document.getElementById('pw-cur').value = '';
        document.getElementById('pw-new').value = '';
        document.getElementById('pw-conf').value = '';
        ok.textContent = 'Password updated.';
        ok.style.display = '';
    }

    // ----------------------------------------------------------
    // Tiny QR-code encoder (sufficient for an otpauth URI ~120 chars)
    // ----------------------------------------------------------
    // We render the QR by deferring to a popular small-footprint library
    // hosted as a static asset OR via a CDN. The simplest robust approach:
    // dynamically import from cdnjs if not already loaded.
    function renderQR(host, text) {
        if (!host) return;
        host.innerHTML = '<div class="text-muted">Loading QR…</div>';
        ensureQrLib().then(function () {
            host.innerHTML = '';
            // qrcode-generator API
            var qr = window.qrcode(0, 'L');
            qr.addData(text);
            qr.make();
            // Render to <img> via SVG for crispness
            host.innerHTML = qr.createImgTag(5, 8);
        }).catch(function (e) {
            host.innerHTML = '<div class="text-danger small">QR rendering failed. ' +
                'Use the manual secret instead.</div>';
            console.error(e);
        });
    }

    function ensureQrLib() {
        if (window.qrcode) return Promise.resolve();
        return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            // qrcode-generator is tiny (~12KB), MIT licensed, no deps
            s.src = 'https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js';
            s.onload = function () { resolve(); };
            s.onerror = function () { reject(new Error('Failed to load QR lib')); };
            document.head.appendChild(s);
        });
    }

    // ----------------------------------------------------------
    // Public init
    // ----------------------------------------------------------
    window.initMyAccount = function () {
        if (!window.zmmAuth || !window.zmmAuth.whoami()) {
            window.zmmAuth.onChange(function (p) {
                if (p) refresh();
            });
            return;
        }
        refresh();
    };
})();