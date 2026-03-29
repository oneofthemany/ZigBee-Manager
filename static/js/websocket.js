/**
 * WebSocket Connection Manager
 * Handles WebSocket connection and message routing
 */

import { state } from './state.js';
import { fetchAllDevices, handleDeviceUpdate, removeDeviceRow, renderDeviceTable } from './devices.js';
import { addLogEntry, updateDebugStatus, handleLivePacket, checkDebugStatus } from './logging.js';
import { updatePairingUI, checkPairingStatus } from './actions.js';
import { handleMQTTMessage } from './mqtt-explorer.js';
import { handleOTAProgress } from './modal/ota.js';
import { hideTestRecoveryBanner } from './editor.js'

/**
 * Initialize WebSocket connection
 */
export function initWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    state.socket.onopen = () => {
        document.getElementById('connection-status').innerHTML =
            '<i class="fas fa-circle text-success"></i> Connected';

        if (!state.isRestarting) {
            fetchAllDevices();
            checkDebugStatus();
            checkHAStatus();  // Check HA status on connect
            // Re-check pairing status on reconnect
            if(typeof checkPairingStatus === 'function') checkPairingStatus();
        }
    };

    state.socket.onclose = () => {
        document.getElementById('connection-status').innerHTML =
            '<i class="fas fa-circle text-danger"></i> Disconnected';

        // Update HA status to unknown on disconnect
        updateHAStatus("unknown");

        setTimeout(initWS, 3000);
    };

    state.socket.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);

            if (state.isRestarting && msg.type === "log") {
                window.location.reload();
                return;
            }

            if (msg.type === 'zone_update' || msg.type === 'zone_state' || msg.type === 'zone_calibration') {
                // Dispatch a custom event that zones.js can listen to
                const customEvent = new CustomEvent('zone-update', { detail: msg });
                window.dispatchEvent(customEvent);
                return;
            }

            switch (msg.type) {
                case "log":
                    addLogEntry(msg.payload || msg.data);
                    break;

                case "device_updated":
                    // core.py sends { type: 'device_updated', payload: { ieee: '...', data: {...} } }
                    handleDeviceUpdate(msg.payload);
                    break;

                case 'ota_progress':
                    handleOTAProgress(msg.data);
                    break;

                case "device_list": // New handler for full list updates
                    state.devices = msg.data;
                    renderDeviceTable();
                    // Check if updateMesh is available globally or imported
                    if (window.updateMesh) window.updateMesh();
                    break;

                case "device_joined":
                case "device_initialized":
                    fetchAllDevices();
                    break;

                case "device_left":
                    // Handle both payload structures just in case
                    const leftIeee = msg.ieee || (msg.data ? msg.data.ieee : null) || (msg.payload ? msg.payload.ieee : null);
                    if(leftIeee) removeDeviceRow(leftIeee);
                    break;

                case "pairing_status":
                    // Handle updated payload structure { enabled: bool, remaining: int }
                    if (msg.payload.enabled) {
                        updatePairingUI(msg.payload.remaining);
                    } else {
                        // If disabled, force a check/reset.
                         if(typeof checkPairingStatus === 'function') checkPairingStatus();
                    }
                    break;

                 // handle system alerts
                case 'system_alert':
                    _showSystemToast(msg.payload);
                    break;
                case 'system_alert_clear':
                    _showSystemToast(msg.payload, true);
                    break;

                // handle debug status
                case "debug_status":
                    updateDebugStatus(msg.payload);
                    break;

                //handle debug packet
                case "debug_packet":
                case "packet":
                    handleLivePacket(msg.data || msg.payload);
                    break;

                // handle HA Status
                case "ha_status":
                    const statusData = msg.data || msg.payload;
                    updateHAStatus(statusData ? statusData.status : 'unknown');
                    break;

                case 'test_recovery':
                    if (msg.payload && msg.payload.status === 'auto_rollback') {
                        alert('Test deployment timed out — changes have been rolled back.');
                        hideTestRecoveryBanner();
                        window.location.reload();
                    }
                    break;

                // handle mqtt message
                case "mqtt_message":
                    if (msg.payload) {
                        handleMQTTMessage(msg.payload);
                    }
                    break;

                default:
                    // Pass unhandled message types to external handlers
                    // (setup wizard, plugins, etc.)
                    if (typeof window._onWsMessage === 'function') {
                        window._onWsMessage(msg);
                    }
                    break;
            }
        } catch (e) {
            console.error("WS Error:", e);
        }
    };
}

/**
 * Update Home Assistant Status Badge
 * status: 'online', 'offline', 'unknown'
 */
function updateHAStatus(status) {
    const badge = document.getElementById('ha-status-badge');
    if (!badge) return;

    // Normalize status string
    const s = (status || 'unknown').toLowerCase();

    if (s === 'online') {
        badge.className = 'badge rounded-pill bg-success';
        badge.innerHTML = '<i class="fas fa-home"></i> HA: Online';
    } else if (s === 'offline') {
        badge.className = 'badge rounded-pill bg-warning text-dark';
        badge.innerHTML = '<i class="fas fa-exclamation-triangle"></i> HA: Offline';
    } else {
        badge.className = 'badge rounded-pill bg-secondary';
        badge.innerHTML = '<i class="fas fa-question"></i> HA: Unknown';
    }
}

/**
 * Check current Home Assistant status
 */
async function checkHAStatus() {
    try {
        const response = await fetch('/api/ha/status');
        if (response.ok) {
            const data = await response.json();
            updateHAStatus(data.status);
        }
    } catch (e) {
        console.error('Failed to check HA status:', e);
    }
}

/**
 * system toast
 */
function _showSystemToast(data, isRecovery) {
    if (!data) return;

    const severity = isRecovery ? 'success' : (data.severity || 'warning');
    const bgClass = severity === 'critical' ? 'bg-danger text-white'
                  : severity === 'success' ? 'bg-success text-white'
                  : 'bg-warning text-dark';
    const icon = severity === 'critical' ? 'exclamation-circle'
               : severity === 'success' ? 'check-circle'
               : 'exclamation-triangle';

    const autoHide = severity === 'critical' ? 30000 : isRecovery ? 5000 : 15000;
    const diag = data.diagnostics || {};
    const hasDiag = diag.cause || (diag.top_consumers && diag.top_consumers.length) || (diag.fixes && diag.fixes.length);
    const toastId = `sys-toast-${Date.now()}`;

    // Build diagnostic details HTML
    let detailsHtml = '';
    if (hasDiag && !isRecovery) {
        let inner = '';

        // Cause
        if (diag.cause) {
            inner += `<div class="mb-1"><strong>Cause:</strong> ${_escToast(diag.cause)}</div>`;
        }

        // Top consumers
        if (diag.top_consumers && diag.top_consumers.length) {
            inner += `<div class="mb-1"><strong>Top consumers:</strong></div><ul class="mb-1 ps-3" style="font-size:0.78rem">`;
            diag.top_consumers.forEach(c => {
                inner += `<li>${_escToast(c.name)}: <strong>${_escToast(c.value)}</strong></li>`;
            });
            inner += `</ul>`;
        }

        // Fix suggestions
        if (diag.fixes && diag.fixes.length) {
            inner += `<div class="mb-1"><strong>How to fix:</strong></div><ol class="mb-0 ps-3" style="font-size:0.78rem">`;
            diag.fixes.forEach(f => {
                // Detect commands (anything with a pipe, slash, or dash pattern)
                const formatted = f.replace(/`([^`]+)`/g, '<code>$1</code>')
                                   .replace(/((?:sudo |curl |echo |ps |top |podman |docker |journalctl )\S+[^<]*)/g, '<code>$1</code>');
                inner += `<li>${formatted}</li>`;
            });
            inner += `</ol>`;
        }

        detailsHtml = `
            <div id="${toastId}-details" style="display:none;border-top:1px solid rgba(0,0,0,0.1);padding-top:6px;margin-top:6px;font-size:0.8rem;max-height:300px;overflow-y:auto">
                ${inner}
            </div>
            <div class="mt-1">
                <a href="#" onclick="event.preventDefault();var d=document.getElementById('${toastId}-details');d.style.display=d.style.display==='none'?'block':'none';this.textContent=d.style.display==='none'?'Show details ▸':'Hide details ▾'" style="font-size:0.75rem;text-decoration:underline">Show details ▸</a>
            </div>`;
    }

    const toastHtml = `
        <div id="${toastId}" class="toast align-items-center border-0 shadow-sm" role="alert"
             data-bs-autohide="true" data-bs-delay="${autoHide}" style="min-width:340px;max-width:480px">
            <div class="${bgClass} rounded-top px-3 py-2 d-flex justify-content-between align-items-center">
                <strong style="font-size:0.85rem"><i class="fas fa-${icon} me-1"></i> System ${severity === 'success' ? 'Recovery' : 'Alert'}</strong>
                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="toast" style="font-size:0.6rem"></button>
            </div>
            <div class="toast-body bg-white rounded-bottom px-3 py-2" style="font-size:0.82rem">
                <div>${_escToast(data.message || 'Unknown alert')}</div>
                ${detailsHtml}
            </div>
        </div>`;

    let container = document.getElementById('system-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'system-toast-container';
        container.className = 'toast-container position-fixed top-0 end-0 p-3';
        container.style.zIndex = '1090';
        document.body.appendChild(container);
    }

    container.insertAdjacentHTML('beforeend', toastHtml);
    const toastEl = document.getElementById(toastId);
    const toast = new bootstrap.Toast(toastEl);
    toast.show();
    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
}

function _escToast(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
