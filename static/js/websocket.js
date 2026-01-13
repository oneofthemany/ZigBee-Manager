/**
 * WebSocket Connection Manager
 * Handles WebSocket connection and message routing
 */

import { state } from './state.js';
import { fetchAllDevices, handleDeviceUpdate, removeDeviceRow, renderDeviceTable } from './devices.js';
import { addLogEntry, updateDebugStatus, handleLivePacket, checkDebugStatus } from './logging.js';
import { updatePairingUI, checkPairingStatus } from './actions.js';
import { handleMQTTMessage } from './mqtt-explorer.js';

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

                case "debug_status":
                    updateDebugStatus(msg.payload);
                    break;

                case "debug_packet":
                case "packet":
                    handleLivePacket(msg.data || msg.payload);
                    break;

                // handle HA Status
                case "ha_status":
                    const statusData = msg.data || msg.payload;
                    updateHAStatus(statusData ? statusData.status : 'unknown');
                    break;

                case "mqtt_message":
                    if (msg.payload) {
                        handleMQTTMessage(msg.payload);
                    }
                    break;

                default:
                    console.debug('Unknown WS message type:', msg.type);
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