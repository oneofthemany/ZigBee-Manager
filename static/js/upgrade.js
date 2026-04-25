/**
 * upgrade.js
 * Settings tab — Upgrade Manager card
 *
 * Self-contained module. Add to main.js:
 *   import { initUpgrade } from './upgrade.js';
 *   initUpgrade();
 *
 * Requires a <div id="upgradeCardMount"></div> in the Settings tab
 * (see index.html snippet).
 */

let _pollTimer = null;
let _logPollTimer = null;
let _lastState = null;

// ============================================================================
// INIT
// ============================================================================

export function initUpgrade() {
    // Render when the Settings tab is shown
    const tab = document.querySelector('button[data-bs-target="#settings"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => {
            renderUpgradeCard();
            refreshUpgradeStatus();
            startPolling();
        });
        // Stop polling when leaving the tab
        tab.addEventListener('hidden.bs.tab', stopPolling);
    }

    // Hook WebSocket messages if the global bus exists
    if (typeof window !== 'undefined') {
        window.addEventListener('zmm-ws-message', (ev) => {
            const msg = ev.detail;
            if (!msg || !msg.type) return;
            if (msg.type === 'upgrade_available' || msg.type === 'upgrade_status') {
                refreshUpgradeStatus();
            }
        });
    }
}

function startPolling() {
    stopPolling();
    _pollTimer = setInterval(refreshUpgradeStatus, 5000);
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
}

// ============================================================================
// RENDER THE STATIC CARD SHELL
// ============================================================================

function renderUpgradeCard() {
    const mount = document.getElementById('upgradeCardMount');
    if (!mount) return;
    if (mount.dataset.rendered === 'true') return;

    mount.innerHTML = `
      <div class="card shadow-sm mb-3" id="upgradeCard">
        <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
          <span class="fw-bold"><i class="fas fa-cloud-arrow-down me-1"></i> Application Upgrade</span>
          <button class="btn btn-outline-primary btn-sm" id="upgradeCheckBtn">
            <i class="fas fa-sync-alt me-1"></i> Check now
          </button>
        </div>
        <div class="card-body" id="upgradeCardBody">
          <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
        </div>
        <div class="card-footer text-muted small" id="upgradeCardFooter"></div>
      </div>

      <!-- Upgrade settings card -->
      <div class="card shadow-sm mb-3" id="upgradeSettingsCard">
        <div class="card-header bg-light py-2">
          <span class="fw-bold"><i class="fas fa-gear me-1"></i> Upgrade Settings</span>
        </div>
        <div class="card-body" id="upgradeSettingsBody">
          <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
        </div>
      </div>

      <!-- Build log modal -->
      <div class="modal fade" id="upgradeLogModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-scrollable">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title"><i class="fas fa-terminal me-1"></i> Build Log</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body p-0">
              <pre id="upgradeLogPre" class="m-0 p-3 small text-monospace"
                   style="background:#0b1021;color:#c9d1d9;max-height:60vh;overflow:auto;"></pre>
            </div>
            <div class="modal-footer">
              <div class="form-check me-auto">
                <input class="form-check-input" type="checkbox" id="upgradeLogAutoScroll" checked>
                <label class="form-check-label small" for="upgradeLogAutoScroll">Auto-scroll</label>
              </div>
              <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
            </div>
          </div>
        </div>
      </div>
    `;

    // Hook button
    document.getElementById('upgradeCheckBtn').addEventListener('click', () => checkForUpdates(true));

    mount.dataset.rendered = 'true';
}

// ============================================================================
// REFRESH STATUS + RENDER
// ============================================================================

async function refreshUpgradeStatus() {
    try {
        const res = await fetch('/api/upgrade/status');
        const data = await res.json();
        if (!data || !data.success) return;
        renderBody(data);
        renderSettings(data);

        // Manage log polling based on state
        const state = data.upgrade_state;
        if (state === 'building' && !_logPollTimer) {
            _logPollTimer = setInterval(refreshBuildLog, 2000);
        } else if (state !== 'building' && _logPollTimer) {
            clearInterval(_logPollTimer);
            _logPollTimer = null;
        }

        // Surface state transitions as toasts
        if (_lastState && _lastState !== state) {
            if (state === 'ready_to_swap') {
                toast('success', 'New image is built and ready to swap');
            } else if (state === 'idle' && _lastState === 'swapping') {
                toast('success', 'Upgrade complete');
            } else if (state === 'failed') {
                toast('danger', 'Upgrade failed: ' + (data.error || 'unknown error'));
            }
        }
        _lastState = state;
    } catch (e) {
        // Silent — the tab may not be visible
    }
}

function renderBody(data) {
    const body = document.getElementById('upgradeCardBody');
    const footer = document.getElementById('upgradeCardFooter');
    if (!body) return;

    const {
        current_version, latest_available, update_available,
        previous_version, previous_image_tag,
        notes, url, last_check,
        upgrade_state, progress_percent, current_step, error,
        architecture, watcher_installed
    } = data;

    const stateBadge = renderStateBadge(upgrade_state);

    let watcherBanner = '';
    if (!watcher_installed) {
        watcherBanner = `
          <div class="alert alert-warning small mb-3">
            <i class="fas fa-triangle-exclamation me-1"></i>
            <strong>Upgrade watcher not installed.</strong>
            Run this on the host to enable in-app upgrades:
            <pre class="mb-0 mt-2 small">curl -fsSL https://raw.githubusercontent.com/oneofthemany/ZigBee-Matter-Manager/main/scripts/install_watcher.sh | bash</pre>
          </div>
        `;
    }

    let progressHtml = '';
    if (upgrade_state && upgrade_state !== 'idle') {
        const pct = Number(progress_percent || 0);
        progressHtml = `
          <div class="mb-3">
            <div class="d-flex justify-content-between small mb-1">
              <span><strong>${escapeHtml(current_step || upgrade_state)}</strong></span>
              <span>${pct}%</span>
            </div>
            <div class="progress" style="height:6px;">
              <div class="progress-bar ${upgrade_state === 'failed' ? 'bg-danger' : ''}"
                   role="progressbar" style="width:${pct}%"
                   aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
            </div>
            ${error ? `<div class="text-danger small mt-2"><i class="fas fa-circle-exclamation me-1"></i> ${escapeHtml(error)}</div>` : ''}
            <div class="mt-2">
              <button class="btn btn-outline-secondary btn-sm" onclick="window.showUpgradeLog()">
                <i class="fas fa-terminal me-1"></i> View log
              </button>
              ${upgrade_state === 'building' ? `
                <button class="btn btn-outline-danger btn-sm ms-1" onclick="window.cancelUpgrade()">
                  <i class="fas fa-ban me-1"></i> Cancel
                </button>` : ''}
            </div>
          </div>
        `;
    }

    let actionHtml = '';
    if (upgrade_state === 'idle' || !upgrade_state) {
        if (update_available) {
            actionHtml = `
              <div class="alert alert-info mb-3">
                <div class="d-flex justify-content-between align-items-start">
                  <div>
                    <div class="fw-bold mb-1">
                      <i class="fas fa-arrow-up-from-bracket me-1"></i>
                      Version ${escapeHtml(latest_available)} is available
                    </div>
                    ${url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener" class="small">Release notes on GitHub <i class="fas fa-external-link-alt ms-1"></i></a>` : ''}
                  </div>
                  <button class="btn btn-primary btn-sm" onclick="window.startUpgradeBuild('${escapeAttr(latest_available)}')"
                          ${!watcher_installed ? 'disabled' : ''}>
                    <i class="fas fa-hammer me-1"></i> Build
                  </button>
                </div>
                ${notes ? `<details class="mt-2"><summary class="small">Release notes</summary>
                  <pre class="small mt-2 mb-0" style="white-space:pre-wrap;max-height:200px;overflow:auto;">${escapeHtml(notes)}</pre>
                </details>` : ''}
              </div>
            `;
        } else {
            actionHtml = `
              <div class="text-success small mb-2">
                <i class="fas fa-check-circle me-1"></i> You're running the latest version.
              </div>
            `;
        }
    } else if (upgrade_state === 'ready_to_swap') {
        actionHtml = `
          <div class="alert alert-success mb-3">
            <div class="fw-bold mb-2">
              <i class="fas fa-check-circle me-1"></i> Image for v${escapeHtml(data.host_status?.target_version || latest_available || '')} is ready
            </div>
            <p class="mb-2 small">
              Swapping takes about 15 seconds. You'll be disconnected briefly,
              then the page will reload automatically.
            </p>
            <button class="btn btn-success btn-sm" onclick="window.startUpgradeSwap()">
              <i class="fas fa-arrow-right-arrow-left me-1"></i> Swap now
            </button>
            <button class="btn btn-outline-secondary btn-sm ms-1" onclick="window.cancelUpgrade()">
              <i class="fas fa-xmark me-1"></i> Discard built image
            </button>
          </div>
        `;
    }

    let rollbackHtml = '';
    if (previous_version && previous_image_tag) {
        rollbackHtml = `
          <div class="border-top pt-3 mt-3">
            <div class="small text-muted mb-1">
              Previous version: <code>${escapeHtml(previous_version)}</code>
            </div>
            <button class="btn btn-outline-warning btn-sm" onclick="window.startUpgradeRollback()">
              <i class="fas fa-rotate-left me-1"></i> Rollback to v${escapeHtml(previous_version)}
            </button>
          </div>
        `;
    }

    body.innerHTML = `
      ${watcherBanner}
      <div class="d-flex justify-content-between align-items-center mb-3">
        <div>
          <div class="small text-muted">Current version</div>
          <div class="fs-5 fw-bold"><code>v${escapeHtml(current_version || 'unknown')}</code> <small class="text-muted">(${escapeHtml(architecture || '')})</small></div>
        </div>
        <div class="text-end">
          ${stateBadge}
        </div>
      </div>
      ${progressHtml}
      ${actionHtml}
      ${rollbackHtml}
    `;

    if (footer) {
        const lastCheckStr = last_check ? new Date(last_check).toLocaleString() : 'never';
        footer.innerHTML = `<i class="fas fa-clock me-1"></i> Last check: ${lastCheckStr}`;
    }
}

function renderStateBadge(state) {
    const map = {
        idle:          { cls: 'bg-success',   label: 'Idle',         icon: 'fa-check' },
        checking:      { cls: 'bg-info',      label: 'Checking',     icon: 'fa-satellite-dish' },
        building:      { cls: 'bg-primary',   label: 'Building',     icon: 'fa-hammer' },
        ready_to_swap: { cls: 'bg-success',   label: 'Ready',        icon: 'fa-circle-check' },
        swapping:      { cls: 'bg-warning text-dark', label: 'Swapping', icon: 'fa-arrow-right-arrow-left' },
        rolling_back:  { cls: 'bg-warning text-dark', label: 'Rolling back', icon: 'fa-rotate-left' },
        failed:        { cls: 'bg-danger',    label: 'Failed',       icon: 'fa-circle-exclamation' },
    };
    const m = map[state] || map.idle;
    return `<span class="badge ${m.cls}"><i class="fas ${m.icon} me-1"></i> ${m.label}</span>`;
}

// ============================================================================
// SETTINGS SECTION
// ============================================================================

function renderSettings(data) {
    const body = document.getElementById('upgradeSettingsBody');
    if (!body) return;

    const auto = !!data.auto_update;
    const win = data.auto_update_window || {};
    const channel = data.channel || 'stable';
    const retention = data.retention_count || 2;
    const repo = data.repo || 'oneofthemany/ZigBee-Matter-Manager';

    body.innerHTML = `
      <div class="row g-3">
        <div class="col-md-6">
          <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="upgAutoUpdate" ${auto ? 'checked' : ''}>
            <label class="form-check-label" for="upgAutoUpdate">
              <strong>Auto-update</strong>
              <div class="small text-muted">Automatically install updates during the quiet window</div>
            </label>
          </div>
        </div>
        <div class="col-md-6">
          <label class="form-label small">Release channel</label>
          <select class="form-select form-select-sm" id="upgChannel">
            <option value="stable" ${channel === 'stable' ? 'selected' : ''}>Stable (releases only)</option>
            <option value="prerelease" ${channel === 'prerelease' ? 'selected' : ''}>Pre-release (all tags)</option>
          </select>
        </div>

        <div class="col-md-6">
          <label class="form-label small">Quiet window start</label>
          <input type="time" class="form-control form-control-sm" id="upgWindowStart"
                 value="${escapeAttr(win.start || '03:00')}">
        </div>
        <div class="col-md-6">
          <label class="form-label small">Quiet window end</label>
          <input type="time" class="form-control form-control-sm" id="upgWindowEnd"
                 value="${escapeAttr(win.end || '05:00')}">
        </div>

        <div class="col-md-6">
          <label class="form-label small">Image retention (keep last N)</label>
          <input type="number" min="1" max="20" class="form-control form-control-sm" id="upgRetention"
                 value="${retention}">
          <div class="form-text small">Older images auto-pruned. Minimum 1. Each image is ~1.5-2 GB.</div>
        </div>
        <div class="col-md-6">
          <label class="form-label small">GitHub repository</label>
          <input type="text" class="form-control form-control-sm" id="upgRepo"
                 value="${escapeAttr(repo)}" placeholder="owner/repo">
        </div>
      </div>
      <div class="mt-3 d-flex gap-2">
        <button class="btn btn-primary btn-sm" onclick="window.saveUpgradeSettings()">
          <i class="fas fa-save me-1"></i> Save settings
        </button>
        <button class="btn btn-outline-secondary btn-sm" onclick="window.runUpgradeGC()">
          <i class="fas fa-broom me-1"></i> Clean up old images
        </button>
      </div>
      <div id="upgradeSettingsAlert" class="alert mt-3 small" style="display:none;"></div>
    `;
}

// ============================================================================
// ACTIONS
// ============================================================================

async function checkForUpdates(force = true) {
    const btn = document.getElementById('upgradeCheckBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Checking...'; }
    try {
        const res = await fetch('/api/upgrade/check?force=' + (force ? 'true' : 'false'), { method: 'POST' });
        const data = await res.json();
        if (!data.success) toast('danger', data.error || 'Check failed');
        await refreshUpgradeStatus();
    } catch (e) {
        toast('danger', 'Check failed: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i> Check now'; }
    }
}

async function startBuild(version) {
    if (!version) return;
    if (!confirm(`Build image for v${version}?\n\nThis takes 15-25 minutes. The current app stays running during the build.`)) return;
    const res = await fetch('/api/upgrade/build', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version })
    });
    const data = await res.json();
    if (data.success) {
        toast('success', data.message || 'Build started');
    } else if (res.status === 409 && (data.error || data.message || '').toLowerCase().includes('progress')) {
        // Stuck lock — offer to force-clear
        if (confirm(
            'The system says another upgrade is in progress, but if you believe nothing is actually running ' +
            '(e.g. the watcher service crashed), you can force-clear the stale lock.\n\n' +
            'Clear the lock now?'
        )) {
            await clearLock();
        }
    } else {
        toast('danger', data.error || data.message || 'Build failed to start');
    }
    refreshUpgradeStatus();
}

async function clearLock() {
    try {
        const res = await fetch('/api/upgrade/clear-lock', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            toast('success', data.message || 'Lock cleared — try Build again');
        } else {
            toast('danger', data.error || 'Could not clear lock');
        }
    } catch (e) {
        toast('danger', 'Clear-lock request failed: ' + e.message);
    }
    refreshUpgradeStatus();
}

async function startSwap() {
    if (!confirm('Swap to the new container?\n\nYou will be briefly disconnected (~15s). The page will reload automatically.')) return;
    const res = await fetch('/api/upgrade/swap', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
        toast('info', 'Swap in progress — reloading when ready...');
        // Poll for health after the API call likely drops
        waitForHealth();
    } else {
        toast('danger', data.error || data.message || 'Swap failed');
    }
}

function waitForHealth() {
    let attempts = 0;
    const iv = setInterval(async () => {
        attempts++;
        try {
            const r = await fetch('/api/status', { cache: 'no-store' });
            if (r.ok) {
                clearInterval(iv);
                setTimeout(() => location.reload(), 1500);
                return;
            }
        } catch (_) { /* expected during swap */ }
        if (attempts > 60) {
            clearInterval(iv);
            toast('danger', 'Server did not come back within 2 minutes. Check the host logs.');
        }
    }, 2000);
}

async function startRollback() {
    if (!confirm('Roll back to the previous version?\n\nYou will be briefly disconnected.')) return;
    const res = await fetch('/api/upgrade/rollback', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
        toast('info', 'Rollback in progress — reloading when ready...');
        waitForHealth();
    } else {
        toast('danger', data.error || data.message || 'Rollback failed');
    }
}

async function cancelUpgrade() {
    if (!confirm('Cancel the in-progress operation?')) return;
    const res = await fetch('/api/upgrade/cancel', { method: 'POST' });
    const data = await res.json();
    if (data.success) toast('warning', 'Cancel requested');
    else toast('danger', data.error || data.message || 'Cancel failed');
    refreshUpgradeStatus();
}

async function saveUpgradeSettings() {
    const body = {
        auto_update: document.getElementById('upgAutoUpdate').checked,
        channel: document.getElementById('upgChannel').value,
        retention_count: parseInt(document.getElementById('upgRetention').value, 10) || 2,
        auto_update_window: {
            start: document.getElementById('upgWindowStart').value || '03:00',
            end:   document.getElementById('upgWindowEnd').value || '05:00',
        },
        repo: document.getElementById('upgRepo').value.trim() || undefined,
    };
    const res = await fetch('/api/upgrade/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    const alert = document.getElementById('upgradeSettingsAlert');
    if (data.success) {
        alert.className = 'alert alert-success small';
        alert.textContent = 'Settings saved.';
    } else {
        alert.className = 'alert alert-danger small';
        alert.textContent = data.error || 'Save failed';
    }
    alert.style.display = 'block';
    setTimeout(() => { alert.style.display = 'none'; }, 5000);
}

async function runGC() {
    if (!confirm('Delete old images beyond the retention count?')) return;
    const res = await fetch('/api/upgrade/gc', { method: 'POST' });
    const data = await res.json();
    if (data.success) toast('success', data.message);
    else toast('danger', data.error || 'GC failed');
}

// ============================================================================
// LOG VIEWER
// ============================================================================

async function showLog() {
    const modalEl = document.getElementById('upgradeLogModal');
    if (!modalEl) return;
    const modal = new window.bootstrap.Modal(modalEl);
    await refreshBuildLog();
    modal.show();
}

async function refreshBuildLog() {
    try {
        const res = await fetch('/api/upgrade/log?lines=500');
        const data = await res.json();
        if (!data.success) return;
        const pre = document.getElementById('upgradeLogPre');
        if (!pre) return;
        pre.textContent = (data.lines || []).join('\n') || '(no output yet)';
        const autoScroll = document.getElementById('upgradeLogAutoScroll');
        if (autoScroll?.checked) {
            pre.scrollTop = pre.scrollHeight;
        }
    } catch (e) { /* ignore */ }
}

// ============================================================================
// EXPORT / GLOBALS
// ============================================================================

if (typeof window !== 'undefined') {
    window.startUpgradeBuild = startBuild;
    window.startUpgradeSwap = startSwap;
    window.startUpgradeRollback = startRollback;
    window.cancelUpgrade = cancelUpgrade;
    window.saveUpgradeSettings = saveUpgradeSettings;
    window.runUpgradeGC = runGC;
    window.showUpgradeLog = showLog;
}

// ============================================================================
// UTILITIES
// ============================================================================

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(type, msg) {
    // Uses your toasts.js-overridden alert if present, otherwise vanilla alert.
    try {
        if (window.showToast) {
            window.showToast(type, msg);
            return;
        }
    } catch (_) {}
    if (type === 'danger') console.error(msg);
    else console.log(msg);
    // Fall back — no UI noise for mere info/success
    if (type === 'danger' || type === 'warning') {
        try { alert(msg); } catch (_) {}
    }
}