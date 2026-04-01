/**
 * otbr.js — Thread Border Router settings sub-tab
 *
 * Provides:
 *   - OTBR daemon status display
 *   - Thread network state (disabled/detached/leader/router/child)
 *   - "Form Network" button to create a new Thread network
 *   - Start/Stop controls
 *   - Active dataset display
 *   - Network info (channel, PAN ID, network name, etc.)
 */

// ============================================================================
// STATE
// ============================================================================

let _otbrPollingInterval = null;

// ============================================================================
// INIT
// ============================================================================

export function initOtbr() {
    const tab = document.querySelector('[data-bs-target="#settingsThread"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadOtbrStatus());
    }
}

// ============================================================================
// STATUS
// ============================================================================

export async function loadOtbrStatus() {
    const container = document.getElementById('threadStatusBody');
    if (!container) return;

    try {
        const res = await fetch('/api/otbr/status');
        const data = await res.json();
        renderOtbrStatus(container, data);
    } catch (e) {
        container.innerHTML = `
            <div class="alert alert-warning small">
                <i class="fas fa-exclamation-triangle me-1"></i>
                Failed to load Thread status: ${e.message}
            </div>`;
    }
}

function renderOtbrStatus(container, data) {
    const stateColors = {
        'disabled': 'secondary',
        'detached': 'warning',
        'child': 'info',
        'router': 'success',
        'leader': 'success',
    };

    const stateColor = stateColors[data.thread_state] || 'secondary';
    const isActive = ['leader', 'router', 'child'].includes(data.thread_state);
    const isDisabled = data.thread_state === 'disabled';

    let networkHtml = '';
    if (data.network) {
        const fields = [
            ['network_name', 'Network Name', 'fa-tag'],
            ['channel', 'Channel', 'fa-broadcast-tower'],
            ['pan_id', 'PAN ID', 'fa-fingerprint'],
            ['extended_pan_id', 'Extended PAN ID', 'fa-barcode'],
            ['mesh_local_prefix', 'Mesh Local Prefix', 'fa-network-wired'],
        ];

        networkHtml = `
            <h6 class="text-uppercase text-muted fw-bold mb-2 mt-3 small">
                <i class="fas fa-network-wired me-1"></i> Thread Network
            </h6>
            <div class="row g-2">
                ${fields.map(([key, label, icon]) => {
                    const val = data.network[key] || '—';
                    return `
                        <div class="col-md-6">
                            <div class="d-flex align-items-center py-1">
                                <i class="fas ${icon} text-muted me-2" style="width:16px"></i>
                                <span class="small text-muted me-2">${label}:</span>
                                <code class="small">${val}</code>
                            </div>
                        </div>`;
                }).join('')}
            </div>`;
    }

    let ipHtml = '';
    if (data.ipaddrs && data.ipaddrs.length > 0) {
        ipHtml = `
            <h6 class="text-uppercase text-muted fw-bold mb-2 mt-3 small">
                <i class="fas fa-globe me-1"></i> IPv6 Addresses
            </h6>
            <div class="small">
                ${data.ipaddrs.map(a => `<code class="d-block mb-1">${a}</code>`).join('')}
            </div>`;
    }

    container.innerHTML = `
        <!-- Status Row -->
        <div class="row g-3 mb-3">
            <div class="col-md-4">
                <div class="d-flex align-items-center">
                    <span class="small text-muted me-2">Daemon:</span>
                    <span class="badge bg-${data.daemon_running ? 'success' : 'secondary'}">
                        ${data.daemon_running ? 'Running' : 'Stopped'}
                    </span>
                </div>
            </div>
            <div class="col-md-4">
                <div class="d-flex align-items-center">
                    <span class="small text-muted me-2">Thread State:</span>
                    <span class="badge bg-${stateColor}">
                        ${data.thread_state}
                    </span>
                </div>
            </div>
            <div class="col-md-4">
                <div class="d-flex align-items-center">
                    <span class="small text-muted me-2">Version:</span>
                    <code class="small">${data.version || '—'}</code>
                </div>
            </div>
        </div>

        <!-- Actions -->
        <div class="d-flex gap-2 mb-3">
            ${!data.available ? `
                <div class="alert alert-info small mb-0 flex-grow-1">
                    <i class="fas fa-info-circle me-1"></i>
                    otbr-agent is not installed. Thread support requires MultiPAN RCP firmware.
                </div>
            ` : isDisabled || data.thread_state === 'detached' ? `
                <button class="btn btn-primary btn-sm" onclick="window._otbrFormNetwork()">
                    <i class="fas fa-play me-1"></i> Form Thread Network
                </button>
                ${data.thread_state === 'detached' ? `
                    <button class="btn btn-outline-primary btn-sm" onclick="window._otbrStartThread()">
                        <i class="fas fa-play me-1"></i> Start (Rejoin)
                    </button>
                ` : ''}
            ` : isActive ? `
                <button class="btn btn-outline-danger btn-sm" onclick="window._otbrStopThread()">
                    <i class="fas fa-stop me-1"></i> Stop Thread
                </button>
                <button class="btn btn-outline-secondary btn-sm" onclick="window._otbrGetDataset()">
                    <i class="fas fa-key me-1"></i> Show Dataset
                </button>
            ` : ''}
            <button class="btn btn-outline-secondary btn-sm ms-auto" onclick="window._otbrRefresh()">
                <i class="fas fa-sync-alt me-1"></i> Refresh
            </button>
        </div>

        <!-- Dataset display (hidden until requested) -->
        <div id="threadDatasetDisplay" style="display:none" class="mb-3"></div>

        ${networkHtml}
        ${ipHtml}

        <!-- Alert area -->
        <div id="threadAlert" class="mt-2" style="display:none"></div>
    `;
}

// ============================================================================
// ACTIONS
// ============================================================================

window._otbrFormNetwork = async function () {
    const alert = document.getElementById('threadAlert');
    showThreadAlert('info', '<i class="fas fa-spinner fa-spin me-1"></i> Forming Thread network...');

    try {
        const res = await fetch('/api/otbr/form-network', { method: 'POST' });
        const data = await res.json();

        if (data.success) {
            showThreadAlert('success',
                `Thread network formed — state: <strong>${data.state}</strong>. ` +
                `The border router will become leader shortly.`);
            setTimeout(() => loadOtbrStatus(), 3000);
        } else {
            showThreadAlert('danger', `Failed: ${data.error}`);
        }
    } catch (e) {
        showThreadAlert('danger', `Error: ${e.message}`);
    }
};

window._otbrStartThread = async function () {
    showThreadAlert('info', '<i class="fas fa-spinner fa-spin me-1"></i> Starting Thread...');
    try {
        const res = await fetch('/api/otbr/start', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            showThreadAlert('success', `Thread started — state: ${data.state}`);
            setTimeout(() => loadOtbrStatus(), 3000);
        } else {
            showThreadAlert('danger', `Failed: ${data.error || 'Unknown error'}`);
        }
    } catch (e) {
        showThreadAlert('danger', `Error: ${e.message}`);
    }
};

window._otbrStopThread = async function () {
    showThreadAlert('info', '<i class="fas fa-spinner fa-spin me-1"></i> Stopping Thread...');
    try {
        const res = await fetch('/api/otbr/stop', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            showThreadAlert('warning', 'Thread stopped.');
            setTimeout(() => loadOtbrStatus(), 1000);
        }
    } catch (e) {
        showThreadAlert('danger', `Error: ${e.message}`);
    }
};

window._otbrGetDataset = async function () {
    const display = document.getElementById('threadDatasetDisplay');
    if (!display) return;

    try {
        const res = await fetch('/api/otbr/dataset');
        const data = await res.json();

        if (data.success) {
            display.style.display = 'block';
            display.innerHTML = `
                <div class="card border-secondary">
                    <div class="card-header bg-light py-1 d-flex justify-content-between align-items-center">
                        <span class="small fw-bold">Active Dataset (hex)</span>
                        <button class="btn btn-outline-secondary btn-sm py-0 px-2"
                                onclick="navigator.clipboard.writeText('${data.dataset_hex}').then(() => this.textContent = 'Copied!')">
                            <i class="fas fa-copy me-1"></i> Copy
                        </button>
                    </div>
                    <div class="card-body py-2">
                        <code class="small d-block text-break">${data.dataset_hex}</code>
                    </div>
                </div>`;
        } else {
            display.style.display = 'block';
            display.innerHTML = `<div class="alert alert-warning small">${data.error}</div>`;
        }
    } catch (e) {
        display.style.display = 'block';
        display.innerHTML = `<div class="alert alert-danger small">${e.message}</div>`;
    }
};

window._otbrRefresh = function () {
    loadOtbrStatus();
};

// ============================================================================
// HELPERS
// ============================================================================

function showThreadAlert(type, message) {
    const el = document.getElementById('threadAlert');
    if (!el) return;
    el.style.display = 'block';
    el.className = `alert alert-${type} small mt-2`;
    el.innerHTML = message;
    if (type === 'success' || type === 'warning') {
        setTimeout(() => { el.style.display = 'none'; }, 8000);
    }
}