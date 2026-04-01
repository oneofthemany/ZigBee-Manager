/**
 * otbr.js — Thread Border Router settings sub-tab
 * Status, network formation with channel/name, topology, dataset display.
 */

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

    // ── Network info ────────────────────────────────────────────
    let networkHtml = '';
    if (data.network) {
        const fields = [
            ['network_name', 'Network Name', 'fa-tag'],
            ['channel', 'Channel', 'fa-broadcast-tower'],
            ['pan_id', 'PAN ID', 'fa-fingerprint'],
            ['ext_pan_id', 'Ext PAN ID', 'fa-barcode'],
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

    // ── IPv6 addresses ──────────────────────────────────────────
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

    // ── Form network controls ───────────────────────────────────
    let formNetworkHtml = '';
    if (data.available && !isActive) {
        formNetworkHtml = `
            <div class="card border-primary mb-3">
                <div class="card-header bg-primary bg-opacity-10 py-2">
                    <span class="small fw-bold"><i class="fas fa-play me-1"></i> Form Thread Network</span>
                </div>
                <div class="card-body py-2">
                    <div class="row g-2 align-items-end">
                        <div class="col-md-3">
                            <label class="form-label small fw-semibold mb-1">Channel</label>
                            <select class="form-select form-select-sm" id="threadChannelSelect">
                                <option value="">Auto</option>
                                ${Array.from({length: 16}, (_, i) => i + 11).map(ch =>
                                    `<option value="${ch}">Channel ${ch}</option>`
                                ).join('')}
                            </select>
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small fw-semibold mb-1">Network Name</label>
                            <input type="text" class="form-control form-control-sm" id="threadNetworkName"
                                   placeholder="OpenThread" maxlength="16">
                        </div>
                        <div class="col-md-3">
                            <button class="btn btn-primary btn-sm w-100" onclick="window._otbrFormNetwork()">
                                <i class="fas fa-play me-1"></i> Form Network
                            </button>
                        </div>
                        ${data.thread_state === 'detached' ? `
                        <div class="col-md-2">
                            <button class="btn btn-outline-primary btn-sm w-100" onclick="window._otbrStartThread()">
                                <i class="fas fa-redo me-1"></i> Rejoin
                            </button>
                        </div>` : ''}
                    </div>
                </div>
            </div>`;
    }

    // ── Main render ─────────────────────────────────────────────
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

        ${!data.available ? `
            <div class="alert alert-info small mb-3">
                <i class="fas fa-info-circle me-1"></i>
                otbr-agent is not installed. Thread support requires MultiPAN RCP firmware.
            </div>
        ` : ''}

        ${formNetworkHtml}

        ${isActive ? `
        <div class="d-flex gap-2 mb-3">
            <button class="btn btn-outline-danger btn-sm" onclick="window._otbrStopThread()">
                <i class="fas fa-stop me-1"></i> Stop Thread
            </button>
            <button class="btn btn-outline-secondary btn-sm" onclick="window._otbrGetDataset()">
                <i class="fas fa-key me-1"></i> Show Dataset
            </button>
            <button class="btn btn-outline-info btn-sm" onclick="window._otbrLoadTopology()">
                <i class="fas fa-project-diagram me-1"></i> Topology
            </button>
            <button class="btn btn-outline-secondary btn-sm ms-auto" onclick="window._otbrRefresh()">
                <i class="fas fa-sync-alt me-1"></i> Refresh
            </button>
        </div>
        ` : `
        <div class="d-flex justify-content-end mb-3">
            <button class="btn btn-outline-secondary btn-sm" onclick="window._otbrRefresh()">
                <i class="fas fa-sync-alt me-1"></i> Refresh
            </button>
        </div>
        `}

        <!-- Dataset display -->
        <div id="threadDatasetDisplay" style="display:none" class="mb-3"></div>

        <!-- Topology display -->
        <div id="threadTopologyDisplay" style="display:none" class="mb-3"></div>

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
    const channelEl = document.getElementById('threadChannelSelect');
    const nameEl = document.getElementById('threadNetworkName');

    const body = {};
    if (channelEl && channelEl.value) body.channel = parseInt(channelEl.value);
    if (nameEl && nameEl.value.trim()) body.network_name = nameEl.value.trim();

    showThreadAlert('info', '<i class="fas fa-spinner fa-spin me-1"></i> Forming Thread network...');

    try {
        const res = await fetch('/api/otbr/form-network', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
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

window._otbrLoadTopology = async function () {
    const display = document.getElementById('threadTopologyDisplay');
    if (!display) return;

    display.style.display = 'block';
    display.innerHTML = '<div class="text-center py-3"><i class="fas fa-spinner fa-spin"></i> Loading topology...</div>';

    try {
        const res = await fetch('/api/otbr/topology');
        const data = await res.json();

        if (!data.success) {
            display.innerHTML = `<div class="alert alert-warning small">${data.error || 'Failed to load topology'}</div>`;
            return;
        }

        const roleIcons = { leader: '👑', router: '🔀', child: '📱' };
        const roleColors = { leader: 'primary', router: 'success', child: 'secondary' };

        let nodesHtml = data.nodes.map(n => {
            const icon = roleIcons[n.role] || '•';
            const color = roleColors[n.role] || 'secondary';
            const selfBadge = n.is_self ? ' <span class="badge bg-info">self</span>' : '';
            const rssiInfo = n.avg_rssi ? ` RSSI: ${n.avg_rssi}/${n.last_rssi} dBm` : '';
            return `
                <tr>
                    <td>${icon} <span class="badge bg-${color}">${n.role}</span>${selfBadge}</td>
                    <td><code>${n.rloc16}</code></td>
                    <td><code class="small">${n.eui64 || n.id || '—'}</code></td>
                    <td class="small text-muted">${rssiInfo}</td>
                </tr>`;
        }).join('');

        let linksHtml = data.links.map(l => {
            const lqBadge = l.link_quality >= 3 ? 'success' : l.link_quality >= 2 ? 'warning' : 'danger';
            return `
                <tr>
                    <td><code>${l.source}</code></td>
                    <td><code>${l.target}</code></td>
                    <td><span class="badge bg-${lqBadge}">LQ ${l.link_quality}</span></td>
                </tr>`;
        }).join('');

        display.innerHTML = `
            <div class="card border-info">
                <div class="card-header bg-info bg-opacity-10 py-1">
                    <span class="small fw-bold"><i class="fas fa-project-diagram me-1"></i> Thread Topology (${data.nodes.length} nodes, ${data.links.length} links)</span>
                </div>
                <div class="card-body py-2">
                    <h6 class="small fw-bold text-muted mb-1">Nodes</h6>
                    <table class="table table-sm table-striped mb-3">
                        <thead><tr><th>Role</th><th>RLOC16</th><th>Address</th><th>Signal</th></tr></thead>
                        <tbody>${nodesHtml || '<tr><td colspan="4" class="text-muted">Only this node (no neighbors yet)</td></tr>'}</tbody>
                    </table>
                    ${data.links.length > 0 ? `
                    <h6 class="small fw-bold text-muted mb-1">Links</h6>
                    <table class="table table-sm table-striped">
                        <thead><tr><th>Source</th><th>Target</th><th>Quality</th></tr></thead>
                        <tbody>${linksHtml}</tbody>
                    </table>` : ''}
                </div>
            </div>`;
    } catch (e) {
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