/**
 * OTA Firmware Update Tab - Device Modal
 * =======================================
 * Provides check/update/notify controls per-device.
 * Listens for WebSocket 'ota_progress' events for live progress.
 */

// ============================================================================
// RENDER OTA TAB
// ============================================================================

export function renderOTATab(device) {
    const ieee = device.ieee;
    return `
    <div id="otaTabContent" class="p-2">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h6 class="mb-0"><i class="fas fa-microchip me-1"></i> Firmware Update</h6>
            <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-primary" onclick="window.otaCheckUpdate('${ieee}')">
                    <i class="fas fa-search"></i> Check
                </button>
                <button class="btn btn-outline-info" onclick="window.otaNotifyDevice('${ieee}')">
                    <i class="fas fa-bell"></i> Notify
                </button>
            </div>
        </div>
        <div id="otaStatus" class="mb-3">
            <div class="alert alert-light small text-center">
                <i class="fas fa-info-circle me-1"></i> Click <strong>Check</strong> to scan for available firmware.
            </div>
        </div>
        <div id="otaProgress" class="d-none mb-3">
            <div class="small fw-bold mb-1">
                <span id="otaProgressLabel">Updating...</span>
                <span id="otaProgressPct" class="float-end">0%</span>
            </div>
            <div class="progress" style="height: 20px;">
                <div id="otaProgressBar" class="progress-bar progress-bar-striped progress-bar-animated"
                     role="progressbar" style="width: 0%"></div>
            </div>
            <div class="mt-2 text-end">
                <button class="btn btn-sm btn-outline-danger" onclick="window.otaCancelUpdate('${ieee}')">
                    <i class="fas fa-times"></i> Cancel
                </button>
            </div>
        </div>
    </div>`;
}

// ============================================================================
// API CALLS
// ============================================================================

window.otaCheckUpdate = async function(ieee) {
    const statusEl = document.getElementById('otaStatus');
    if (!statusEl) return;

    statusEl.innerHTML = `<div class="text-center"><i class="fas fa-spinner fa-spin"></i> Checking...</div>`;

    try {
        const resp = await fetch(`/api/ota/check/${ieee}`);
        const data = await resp.json();

        if (data.available) {
            statusEl.innerHTML = `
                <div class="alert alert-success small">
                    <i class="fas fa-arrow-circle-up me-1"></i>
                    <strong>Update Available!</strong><br>
                    Current: <code>${data.current_version}</code><br>
                    New: <code>${data.new_version}</code>
                    ${data.image_size ? `<br>Size: ${(data.image_size / 1024).toFixed(1)} KB` : ''}
                    <div class="mt-2">
                        <button class="btn btn-sm btn-success" onclick="window.otaStartUpdate('${ieee}')">
                            <i class="fas fa-download"></i> Install Update
                        </button>
                        <button class="btn btn-sm btn-outline-warning ms-1" onclick="window.otaStartUpdate('${ieee}', true)">
                            <i class="fas fa-bolt"></i> Force
                        </button>
                    </div>
                </div>`;
        } else {
            statusEl.innerHTML = `
                <div class="alert alert-light small">
                    <i class="fas fa-check-circle text-success me-1"></i>
                    ${data.current_version ? `Current: <code>${data.current_version}</code> — ` : ''}
                    ${data.error || data.message || 'No update available'}
                </div>`;
        }
    } catch (e) {
        statusEl.innerHTML = `<div class="alert alert-danger small">${e.message}</div>`;
    }
};

window.otaStartUpdate = async function(ieee, force = false) {
    if (!confirm(`Start firmware update for ${ieee}?${force ? '\n\nFORCE mode — this may downgrade!' : ''}`)) return;

    try {
        const resp = await fetch(`/api/ota/update/${ieee}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({force})
        });
        const data = await resp.json();
        if (data.success) {
            _showProgress(true, 'Starting...', 0);
        } else {
            alert('Update failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

window.otaNotifyDevice = async function(ieee) {
    try {
        const resp = await fetch(`/api/ota/notify/${ieee}`, {method: 'POST'});
        const data = await resp.json();
        const statusEl = document.getElementById('otaStatus');
        if (statusEl) {
            statusEl.innerHTML = `<div class="alert alert-${data.success ? 'info' : 'warning'} small">
                ${data.success ? '<i class="fas fa-bell me-1"></i> Image Notify sent — device will check in shortly.' : data.error}
            </div>`;
        }
    } catch (e) {
        alert('Notify failed: ' + e.message);
    }
};

window.otaCancelUpdate = async function(ieee) {
    try {
        await fetch(`/api/ota/cancel/${ieee}`, {method: 'POST'});
        _showProgress(false);
    } catch (e) {
        console.error('Cancel failed:', e);
    }
};

// ============================================================================
// PROGRESS HANDLING (called from WebSocket)
// ============================================================================

export function handleOTAProgress(data) {
    if (!data || !data.status) return;

    const statusMap = {
        'starting':    {text: 'Preparing...', animated: true},
        'downloading': {text: 'Downloading image...', animated: true},
        'updating':    {text: `Updating... ${data.progress || 0}%`, animated: true},
        'complete':    {text: 'Update complete!', animated: false},
        'failed':      {text: `Failed: ${data.error || 'unknown'}`, animated: false},
        'cancelled':   {text: 'Cancelled', animated: false},
    };

    const info = statusMap[data.status] || {text: data.status, animated: false};

    if (data.status === 'complete' || data.status === 'failed' || data.status === 'cancelled') {
        setTimeout(() => _showProgress(false), 3000);
    }

    _showProgress(true, info.text, data.progress || 0, info.animated);
}

function _showProgress(show, label = '', pct = 0, animated = true) {
    const wrap = document.getElementById('otaProgress');
    const bar = document.getElementById('otaProgressBar');
    const lbl = document.getElementById('otaProgressLabel');
    const pctEl = document.getElementById('otaProgressPct');

    if (!wrap) return;

    if (show) {
        wrap.classList.remove('d-none');
        if (bar) {
            bar.style.width = `${pct}%`;
            bar.className = `progress-bar ${animated ? 'progress-bar-striped progress-bar-animated' : ''}`;
            if (pct >= 100) bar.classList.add('bg-success');
        }
        if (lbl) lbl.textContent = label;
        if (pctEl) pctEl.textContent = `${pct}%`;
    } else {
        wrap.classList.add('d-none');
    }
}
