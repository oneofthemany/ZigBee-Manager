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
    if (action === 'remove' && !confirm("Are you sure?")) return;

    try {
        const res = await fetch(`/api/device/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee: ieee, force: false })
        });
        const data = await res.json();

        if (data.success) {
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: `${action.toUpperCase()} sent.`
            });
        } else {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        console.error(e);
    }
}


/**
 * Remove device with ban option
 */
export async function removeDevice(ieee, ban = false) {
    const msg = ban
        ? "Remove AND ban this device (prevents rejoining)?"
        : "Remove this device?";

    if (!confirm(msg)) return;

    try {
        const res = await fetch('/api/device/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, force: false, ban })
        });
        const data = await res.json();

        if (data.success) {
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: ban ? `Device removed and banned` : `Device removed`
            });
        } else {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        console.error(e);
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
 * Start Touchlink scan for Light Link devices (Ikea, Philips bulbs)
 */
export async function startTouchlinkScan() {
    if (!confirm("Start Touchlink scan for Light Link bulbs?\n\nInstructions:\n1. Bring bulb within 10cm of coordinator\n2. Click OK to start scan\n3. Power cycle bulb (OFF then ON) within 5 seconds")) {
        return;
    }

    try {
        addLogEntry({
            timestamp: getTimestamp(),
            level: 'INFO',
            message: 'Starting Touchlink scan...'
        });

        const res = await fetch("/api/touchlink/scan", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await res.json();

        if (data.success) {
            alert("Touchlink scan started!\n\nPOWER CYCLE YOUR BULB NOW (OFF then ON)\n\nThe bulb should join within 10 seconds.");
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: 'Touchlink scan initiated - power cycle bulb now'
            });
        } else {
            alert("Touchlink scan failed: " + (data.error || "Unknown error") + "\n\nThis may indicate:\n- Coordinator doesn't support Touchlink\n- Zigpy version too old\n- Try standard pairing instead");
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'ERROR',
                message: 'Touchlink scan failed: ' + (data.error || "Unknown")
            });
        }
    } catch (e) {
        alert("Error starting Touchlink scan: " + e.message);
        addLogEntry({
            timestamp: getTimestamp(),
            level: 'ERROR',
            message: 'Touchlink error: ' + e.message
        });
    }
}

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
