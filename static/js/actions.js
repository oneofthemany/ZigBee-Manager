/**
 * Device Actions & Commands
 * Handles device control, pairing, and maintenance actions
 */

import { addLogEntry } from './logging.js';
import { getTimestamp } from './utils.js';
import { state } from './state.js';

// We need to expose these functions if they are called from HTML onclick handlers
// But generally main.js handles the window assignment.

/**
 * Check pairing status on load
 */
export async function checkPairingStatus() {
    try {
        const res = await fetch("/api/permit_join");
        const data = await res.json();
        if (data.enabled && data.remaining > 0) {
            updatePairingUI(data.remaining);
        } else {
            resetPairingUI();
        }
    } catch (e) {
        console.error("Failed to check pairing status", e);
    }
}

/**
 * Send command to device (now with endpoint support)
 */
export async function sendCommand(ieee, command, value = null, endpoint = null) {
    try {
        const body = { ieee: ieee, command: command, value: value };
        if (endpoint) {
            body.endpoint = endpoint;
        }

        const res = await fetch('/api/device/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.success) {
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: `Command sent${endpoint ? ' (EP'+endpoint+')' : ''}`
            });
        } else {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        alert("Command failed");
    }
}

/**
 * Adjust thermostat setpoint by delta
 */
export function adjustSetpoint(ieee, delta) {
    const input = document.getElementById('setpoint-input');
    if (input) {
        const newVal = parseFloat(input.value) + delta;
        input.value = newVal.toFixed(1);
        sendCommand(ieee, 'temperature', newVal);
    }
}


/**
 * Perform device maintenance action
 */
export async function doAction(action, ieee) {
    let shouldBan = false;

    // Special handling for 'remove' to ask about banning
    if (action === 'remove') {
        if (!confirm("Are you sure you want to remove this device?")) return;
        shouldBan = confirm("Do you also want to BAN this device to prevent it from rejoining?\n\nClick OK to Remove & Ban.\nClick Cancel to just Remove.");
    }

    try {
        let url = `/api/device/${action}`;
        let body = { ieee: ieee, force: false, ban: shouldBan };

        // Handle aggressive/baseline reconfigure variants
        if (action === 'reconfigure_aggressive') {
            url = '/api/device/reconfigure';
            body = { ieee: ieee, aggressive: true };
        } else if (action === 'reconfigure_baseline') {
            url = '/api/device/reconfigure';
            body = { ieee: ieee, aggressive: false };
        }

        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();

        if (data.success) {
            let logMsg = `${action.toUpperCase()} sent.`;
            if (action === 'remove' && shouldBan) {
                logMsg = "Device removed and BANNED.";
            } else if (action === 'remove') {
                logMsg = "Device removed.";
            } else if (action === 'reconfigure_aggressive') {
                logMsg = "Aggressive LQI reporting applied.";
            } else if (action === 'reconfigure_baseline') {
                logMsg = "Baseline reporting restored.";
            }

            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: logMsg
            });

            if (action === 'remove') {
                alert(logMsg);
            }
        } else {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        console.error(e);
        alert("Action failed: " + e.message);
    }
}


/**
 * Ban a device by IEEE
 */
export async function banDevice(ieee, reason = null) {
    try {
        const res = await fetch('/api/ban', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, reason })
        });
        return await res.json();
    } catch (e) {
        console.error(e);
        return { success: false, error: e.message };
    }
}

/**
 * Unban a device
 */
export async function unbanDevice(ieee) {
    try {
        const res = await fetch('/api/unban', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee })
        });
        return await res.json();
    } catch (e) {
        console.error(e);
        return { success: false, error: e.message };
    }
}

/**
 * Get banned devices list
 */
export async function getBannedDevices() {
    try {
        const res = await fetch('/api/banned');
        return await res.json();
    } catch (e) {
        console.error(e);
        return { banned: [], count: 0 };
    }
}

/**
 * Prompt to rename device
 */
export async function renamePrompt(ieee, oldName) {
    const name = prompt("Rename Device:", oldName);
    if (name && name !== oldName) {
        await fetch('/api/device/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee: ieee, name: name })
        });
        // Trigger refresh if possible, or wait for WS
        if (window.fetchAllDevices) window.fetchAllDevices();
    }
}

/**
 * Toggle pairing mode (Global)
 */
export function togglePairing() {
    // If currently pairing (state check), we want to stop
    const isPairing = state.pairingInterval !== null;

    const duration = isPairing ? 0 : 240; // 0 = disable

    fetch("/api/permit_join", {
        method: "POST",
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: duration })
    })
    .then(r => r.json())
    .then(d => {
        if (d.status === 'success') {
            // Logic to update UI based on response
            if (d.enabled === false) {
                resetPairingUI();
            } else {
                updatePairingUI(d.duration || 240);
            }
        }
    })
    .catch(e => {
        console.error("Pairing toggle failed:", e);
        alert("Failed to toggle pairing");
    });
}

/**
 * Enable pairing on a specific device (Router)
 */
export async function permitJoinVia(ieee) {
    if (!confirm("Enable pairing via this device?")) return;

    try {
        const res = await fetch("/api/permit_join", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration: 240, target_ieee: ieee })
        });
        const data = await res.json();

        if (data.success) {
             addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: `Pairing enabled via device`
            });
            updatePairingUI(240);
        } else {
            alert("Failed: " + data.error);
        }
    } catch(e) {
        alert("Error starting pairing: " + e);
    }
}

/**
 * Reset Pairing UI to default state
 */
function resetPairingUI() {
    const btn = document.getElementById('pairBtn');
    if (!btn) return;

    if (state.pairingInterval) {
        clearInterval(state.pairingInterval);
        state.pairingInterval = null;
    }

    // Restore button state
    btn.classList.remove('btn-danger');
    btn.classList.add('btn-success');
    btn.innerHTML = `<i class="fas fa-plus-circle"></i> Enable Pairing (All)`;
}

/**
 * Update pairing button UI with countdown
 */
export function updatePairingUI(time) {
    const btn = document.getElementById('pairBtn');
    if (!btn) return;

    // Clear any existing interval first
    if (state.pairingInterval) clearInterval(state.pairingInterval);

    // Change button to "Stop" style
    btn.classList.remove('btn-success');
    btn.classList.add('btn-danger');

    let timeLeft = time;

    // Initial render
    btn.innerHTML = `<i class="fas fa-stop-circle"></i> Stop Pairing (${timeLeft}s)`;

    state.pairingInterval = setInterval(() => {
        timeLeft--;
        if (timeLeft <= 0) {
            resetPairingUI();
        } else {
            btn.innerHTML = `<i class="fas fa-stop-circle"></i> Stop Pairing (${timeLeft}s)`;
        }
    }, 1000);
}

/**
 * Touchlink state
 */
let touchlinkDevices = [];

/**
 * Start Touchlink scan for Light Link devices (Ikea, Philips bulbs)
 * Enhanced with device discovery and actions
 */
export async function startTouchlinkScan() {
    // Open modal instead of confirm dialog
    openTouchlinkModal();
}

/**
 * Open Touchlink modal
 */
function openTouchlinkModal() {
    let modal = document.getElementById('touchlinkModal');
    if (!modal) {
        modal = createTouchlinkModal();
        document.body.appendChild(modal);
    }

    const bsModal = new bootstrap.Modal(modal);
    bsModal.show();

    // Reset state
    touchlinkDevices = [];
    document.getElementById('touchlinkResults').innerHTML = `
        <div class="text-center text-muted py-4">
            <i class="fas fa-info-circle"></i> Click "Scan" to search for Touchlink devices
        </div>
    `;
    document.getElementById('touchlinkStatus').innerHTML = '';
    document.getElementById('identifyAllBtn').disabled = true;
    document.getElementById('resetAllBtn').disabled = true;
}

/**
 * Create Touchlink modal
 */
function createTouchlinkModal() {
    const modal = document.createElement('div');
    modal.id = 'touchlinkModal';
    modal.className = 'modal fade';
    modal.tabIndex = -1;
    modal.innerHTML = `
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header bg-primary text-white">
                    <h5 class="modal-title"><i class="fas fa-broadcast-tower"></i> Touchlink Commissioning</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="alert alert-info mb-3">
                        <strong><i class="fas fa-lightbulb"></i> Instructions:</strong>
                        <ol class="mb-0 mt-2">
                            <li>Power ON the bulb you want to reset</li>
                            <li>Bring it within <strong>10-20cm</strong> of your coordinator</li>
                            <li>Click <strong>Scan</strong> to find the device</li>
                            <li>Use <strong>Identify</strong> to confirm (bulb will blink)</li>
                            <li>Click <strong>Factory Reset</strong> to reset the bulb</li>
                        </ol>
                    </div>

                    <div class="row mb-3">
                        <div class="col-md-6">
                            <label class="form-label">Channel (optional)</label>
                            <select id="touchlinkChannel" class="form-select">
                                <option value="">All Channels (11-26)</option>
                                ${[...Array(16)].map((_, i) => `<option value="${11 + i}">Channel ${11 + i}</option>`).join('')}
                            </select>
                        </div>
                        <div class="col-md-6 d-flex align-items-end">
                            <button id="touchlinkScanBtn" class="btn btn-primary w-100" onclick="window.doTouchlinkScan()">
                                <i class="fas fa-search"></i> Scan for Devices
                            </button>
                        </div>
                    </div>

                    <div id="touchlinkStatus" class="mb-3"></div>

                    <div class="card">
                        <div class="card-header bg-light"><i class="fas fa-list"></i> Discovered Devices</div>
                        <div class="card-body p-0">
                            <div id="touchlinkResults" class="p-3"></div>
                        </div>
                    </div>

                    <div class="mt-3 d-flex gap-2">
                        <button class="btn btn-outline-warning" onclick="window.touchlinkIdentifyAll()" id="identifyAllBtn" disabled>
                            <i class="fas fa-eye"></i> Identify All
                        </button>
                        <button class="btn btn-outline-danger" onclick="window.touchlinkResetAll()" id="resetAllBtn" disabled>
                            <i class="fas fa-exclamation-triangle"></i> Reset All Found
                        </button>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
                </div>
            </div>
        </div>
    `;
    return modal;
}

/**
 * Perform the actual scan
 */
window.doTouchlinkScan = async function() {
    const btn = document.getElementById('touchlinkScanBtn');
    const resultsDiv = document.getElementById('touchlinkResults');
    const statusDiv = document.getElementById('touchlinkStatus');
    const channel = document.getElementById('touchlinkChannel').value;

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Scanning...';
    statusDiv.innerHTML = `
        <div class="alert alert-warning mb-0">
            <i class="fas fa-broadcast-tower fa-pulse"></i> Scanning... This may take up to 30 seconds.
        </div>
    `;
    resultsDiv.innerHTML = '<div class="text-center py-4"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

    addLogEntry({
        timestamp: getTimestamp(),
        level: 'INFO',
        message: `Starting Touchlink scan${channel ? ` on channel ${channel}` : ' on all channels'}...`
    });

    try {
        const url = channel ? `/api/touchlink/scan?channel=${channel}` : '/api/touchlink/scan';
        const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
        const data = await res.json();

        if (data.success && data.devices && data.devices.length > 0) {
            touchlinkDevices = data.devices;
            renderTouchlinkResults();
            statusDiv.innerHTML = `<div class="alert alert-success mb-0"><i class="fas fa-check-circle"></i> Found ${data.devices.length} device(s)</div>`;
            document.getElementById('identifyAllBtn').disabled = false;
            document.getElementById('resetAllBtn').disabled = false;

            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: `Touchlink found ${data.devices.length} device(s)`
            });
        } else {
            touchlinkDevices = [];
            resultsDiv.innerHTML = `
                <div class="text-center text-muted py-4">
                    <i class="fas fa-search"></i> No devices found<br>
                    <small>Ensure bulb is powered on and within 20cm of coordinator</small>
                </div>
            `;
            statusDiv.innerHTML = data.error ? `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${data.error}</div>` : '';
            document.getElementById('identifyAllBtn').disabled = true;
            document.getElementById('resetAllBtn').disabled = true;

            addLogEntry({
                timestamp: getTimestamp(),
                level: 'WARN',
                message: 'Touchlink scan: No devices found'
            });
        }
    } catch (e) {
        statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> Scan failed: ${e.message}</div>`;
        addLogEntry({
            timestamp: getTimestamp(),
            level: 'ERROR',
            message: `Touchlink error: ${e.message}`
        });
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-search"></i> Scan for Devices';
    }
};

/**
 * Render results table
 */
function renderTouchlinkResults() {
    const resultsDiv = document.getElementById('touchlinkResults');

    if (!touchlinkDevices.length) {
        resultsDiv.innerHTML = '<div class="text-center text-muted py-4">No devices found</div>';
        return;
    }

    resultsDiv.innerHTML = `
        <table class="table table-hover mb-0">
            <thead><tr><th>IEEE Address</th><th>Channel</th><th>RSSI</th><th>Actions</th></tr></thead>
            <tbody>
                ${touchlinkDevices.map((d, i) => `
                    <tr>
                        <td><code>${d.ieee || 'Unknown'}</code></td>
                        <td><span class="badge bg-secondary">${d.channel || '-'}</span></td>
                        <td><span class="${d.rssi > -50 ? 'text-success' : d.rssi > -70 ? 'text-warning' : 'text-danger'}">${d.rssi ? d.rssi + ' dBm' : '-'}</span></td>
                        <td>
                            <div class="btn-group btn-group-sm">
                                <button class="btn btn-outline-primary" onclick="window.touchlinkIdentify(${i})" title="Identify"><i class="fas fa-eye"></i></button>
                                <button class="btn btn-outline-danger" onclick="window.touchlinkReset(${i})" title="Factory Reset"><i class="fas fa-undo"></i></button>
                            </div>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

/**
 * Identify single device
 */
window.touchlinkIdentify = async function(index) {
    const device = touchlinkDevices[index];
    if (!device) return;

    const statusDiv = document.getElementById('touchlinkStatus');
    statusDiv.innerHTML = `<div class="alert alert-info mb-0"><i class="fas fa-spinner fa-spin"></i> Identifying ${device.ieee}...</div>`;

    addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: `Touchlink identify: ${device.ieee}` });

    try {
        const res = await fetch('/api/touchlink/identify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee: device.ieee, channel: device.channel })
        });
        const data = await res.json();

        statusDiv.innerHTML = data.success
            ? `<div class="alert alert-success mb-0"><i class="fas fa-check-circle"></i> Device should be blinking</div>`
            : `<div class="alert alert-warning mb-0"><i class="fas fa-exclamation-triangle"></i> ${data.error || 'Identify may have failed'}</div>`;
    } catch (e) {
        statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${e.message}</div>`;
    }
};

/**
 * Reset single device
 */
window.touchlinkReset = async function(index) {
    const device = touchlinkDevices[index];
    if (!device) return;

    if (!confirm(`Factory reset ${device.ieee}?\n\nThis will reset to factory defaults.`)) return;

    const statusDiv = document.getElementById('touchlinkStatus');
    statusDiv.innerHTML = `<div class="alert alert-warning mb-0"><i class="fas fa-spinner fa-spin"></i> Resetting ${device.ieee}...</div>`;

    addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: `Touchlink reset: ${device.ieee}` });

    try {
        const res = await fetch('/api/touchlink/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee: device.ieee, channel: device.channel })
        });
        const data = await res.json();

        if (data.success) {
            statusDiv.innerHTML = `<div class="alert alert-success mb-0"><i class="fas fa-check-circle"></i> Reset sent! Device can now be paired.</div>`;
            touchlinkDevices.splice(index, 1);
            renderTouchlinkResults();
            addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: `Touchlink reset successful: ${device.ieee}` });
        } else {
            statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${data.error || 'Reset failed'}</div>`;
        }
    } catch (e) {
        statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${e.message}</div>`;
    }
};

/**
 * Identify all devices
 */
window.touchlinkIdentifyAll = async function() {
    if (!touchlinkDevices.length) return;

    addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: `Touchlink identify all (${touchlinkDevices.length} devices)` });

    const statusDiv = document.getElementById('touchlinkStatus');
    statusDiv.innerHTML = `<div class="alert alert-info mb-0"><i class="fas fa-spinner fa-spin"></i> Identifying all devices...</div>`;

    try {
        const res = await fetch('/api/touchlink/identify', { method: 'POST' });
        statusDiv.innerHTML = `<div class="alert alert-success mb-0"><i class="fas fa-check-circle"></i> All devices should be blinking</div>`;
    } catch (e) {
        statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${e.message}</div>`;
    }
};

/**
 * Reset all devices
 */
window.touchlinkResetAll = async function() {
    if (!touchlinkDevices.length) return;
    if (!confirm(`Factory reset ALL ${touchlinkDevices.length} devices?\n\nThis cannot be undone!`)) return;

    addLogEntry({ timestamp: getTimestamp(), level: 'WARN', message: `Touchlink reset ALL (${touchlinkDevices.length} devices)` });

    const statusDiv = document.getElementById('touchlinkStatus');
    statusDiv.innerHTML = `<div class="alert alert-warning mb-0"><i class="fas fa-spinner fa-spin"></i> Resetting all devices...</div>`;

    try {
        const res = await fetch('/api/touchlink/reset', { method: 'POST' });
        const data = await res.json();

        if (data.success) {
            statusDiv.innerHTML = `<div class="alert alert-success mb-0"><i class="fas fa-check-circle"></i> ${data.message || 'All devices reset!'}</div>`;
            touchlinkDevices = [];
            renderTouchlinkResults();
            document.getElementById('identifyAllBtn').disabled = true;
            document.getElementById('resetAllBtn').disabled = true;
        } else {
            statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${data.error}</div>`;
        }
    } catch (e) {
        statusDiv.innerHTML = `<div class="alert alert-danger mb-0"><i class="fas fa-exclamation-circle"></i> ${e.message}</div>`;
    }
};

/**
 * Bind two devices
 */
export async function bindDevices(sourceIeee, targetIeee, clusterId) {
    if (!targetIeee || !clusterId) {
        alert("Please select a target device and a cluster.");
        return;
    }

    const btn = document.getElementById('bindBtn');
    const originalText = btn ? btn.innerHTML : 'Bind';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Binding...';
    }

    try {
        const res = await fetch("/api/device/bind", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_ieee: sourceIeee,
                target_ieee: targetIeee,
                cluster_id: parseInt(clusterId)
            })
        });

        // Safe JSON parsing to catch 500 errors
        const text = await res.text();
        let data;
        try {
            data = JSON.parse(text);
        } catch (e) {
            throw new Error(`Server returned invalid JSON: ${text.substring(0, 100)}...`);
        }

        if (data.success) {
             addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: `Bound ${sourceIeee} to ${targetIeee} (Cluster ${clusterId})`
            });
            alert("Binding successful! The device should now control the target.");
        } else {
            alert("Binding failed: " + (data.error || "Unknown error"));
        }
    } catch(e) {
        alert("Error binding devices: " + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    }
}


/**
 * Open the Banned Devices Modal and load data
 */
export async function openBannedModal() {
    // 1. Open the modal using Bootstrap API
    const modalEl = document.getElementById('bannedDevicesModal');
    const modal = new bootstrap.Modal(modalEl);
    modal.show();

    // 2. Load the list
    await refreshBannedList();
}

/**
 * Fetch and Render the banned list
 */
export async function refreshBannedList() {
    const container = document.getElementById('bannedListContainer');
    container.innerHTML = '<div class="text-center text-muted p-3"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    const data = await getBannedDevices();

    if (!data.banned || data.banned.length === 0) {
        container.innerHTML = '<div class="text-center text-muted p-3">No banned devices found.</div>';
        return;
    }

    // Render the list items
    container.innerHTML = data.banned.map(ieee => `
        <div class="list-group-item d-flex justify-content-between align-items-center">
            <div>
                <i class="fas fa-ban text-danger me-2"></i>
                <span class="font-monospace">${ieee}</span>
            </div>
            <button class="btn btn-sm btn-outline-secondary" onclick="handleUnbanClick('${ieee}')">
                <i class="fas fa-unlock"></i> Unban
            </button>
        </div>
    `).join('');
}

/**
 * Handle Unban Button Click
 */
export async function handleUnbanClick(ieee) {
    if (!confirm(`Are you sure you want to unban ${ieee}?`)) return;

    const res = await unbanDevice(ieee);

    if (res.success) {
        // Refresh the list to show it's gone
        await refreshBannedList();
    } else {
        alert("Failed to unban: " + (res.error || "Unknown error"));
    }
}

/**
 * Remove orphans
 */
export async function cleanupOrphans() {
    if (!confirm('This will find and remove devices that exist in the database but are not active on the network. Continue?')) {
        return;
    }

    // First, show what will be removed
    const orphaned = await fetch('/api/devices/orphaned').then(r => r.json());

    if (orphaned.count === 0) {
        alert('No orphaned devices found. Database is clean!');
        return;
    }

    const msg = `Found ${orphaned.count} orphaned devices:\n\n${orphaned.orphaned.join('\n')}\n\nRemove these from database?`;

    if (!confirm(msg)) return;

    const result = await fetch('/api/devices/cleanup-orphaned', {
        method: 'POST'
    }).then(r => r.json());

    alert(`Cleanup complete:\n✓ Removed: ${result.count_removed}\n♻ Recovered: ${result.count_recovered}\n✗ Failed: ${result.count_failed}`);

    // Refresh device list
    await fetchAllDevices();
}

// =============================================================================
// MATTER INTEGRATION
// =============================================================================

/**
 * Commission a Matter device via setup code
 */
export async function matterCommission() {
    const code = prompt(
        "Enter Matter setup code:\n\n" +
        "This can be:\n" +
        "• Manual pairing code (e.g. 3476-610-1411)\n" +
        "• QR code string (e.g. MT:Y3.13OTB00KA0648G00)"
    );
    if (!code || !code.trim()) return;

    try {
        const res = await fetch("/api/matter/commission", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code: code.trim() })
        });
        const data = await res.json();

        if (data.success) {
            addLogEntry({
                timestamp: getTimestamp(),
                level: "INFO",
                message: "Matter device commissioning started"
            });
            alert("Matter commissioning started. The device should appear shortly.");
        } else {
            alert("Commission failed: " + data.error);
        }
    } catch (e) {
        console.error("Matter commission error:", e);
        alert("Commission error: " + e.message);
    }
}


/**
 * Check Matter bridge status and update UI indicator
 */
export async function checkMatterStatus() {
    const badge = document.getElementById('matterStatusBadge');
    const btn = document.getElementById('matterPairBtn');

    try {
        const res = await fetch("/api/matter/status");
        const data = await res.json();

        if (badge) {
            if (!data.enabled) {
                badge.className = 'badge bg-secondary ms-1';
                badge.textContent = 'Not configured';
                if (btn) btn.disabled = true;
            } else if (data.connected) {
                badge.className = 'badge bg-success ms-1';
                badge.textContent = `Matter: ${data.device_count} devices`;
                if (btn) btn.disabled = false;
            } else {
                badge.className = 'badge bg-warning ms-1';
                badge.textContent = 'Matter: Disconnected';
                if (btn) btn.disabled = true;
            }
        }
    } catch (e) {
        if (badge) {
            badge.className = 'badge bg-secondary ms-1';
            badge.textContent = 'Matter: N/A';
        }
    }
}