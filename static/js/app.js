/**
 * Zigbee Gateway Frontend Application Logic
 * Consolidated Overview & Full Debugging
 */

let socket;
let allLogs = [];
let currentDeviceIeee = null;
let deviceCache = {};
let debugEnabled = false;
let isRestarting = false;

// ============================================================================
// WEBSOCKET & INITIALIZATION
// ============================================================================

function initWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    socket.onopen = () => {
        document.getElementById('connection-status').innerHTML = '<i class="fas fa-circle text-success"></i> Connected';
        if (!isRestarting) {
            fetchAllDevices();
            checkDebugStatus();
        }
    };

    socket.onclose = () => {
        document.getElementById('connection-status').innerHTML = '<i class="fas fa-circle text-danger"></i> Disconnected';
        setTimeout(initWS, 3000);
    };

    socket.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);

            if (isRestarting && msg.type === "log") {
                console.log("System restart detected via log, reloading...");
                window.location.reload();
                return;
            }

            if (msg.type === "log") {
                addLogEntry(msg.payload);
            } else if (msg.type === "device_updated") {
                handleDeviceUpdate(msg.payload);
            } else if (msg.type === "device_joined" || msg.type === "device_initialized") {
                fetchAllDevices();
            } else if (msg.type === "device_left") {
                removeDeviceRow(msg.payload.ieee);
            } else if (msg.type === "pairing_status") {
                updatePairingUI(msg.payload.time);
            } else if (msg.type === "debug_status") {
                updateDebugStatus(msg.payload);
            } else if (msg.type === "debug_packet") {
                handleLivePacket(msg.payload);
            }
        } catch (e) {
            console.error("WS Error:", e);
        }
    };
}

document.addEventListener('DOMContentLoaded', () => {
    initWS();
    setInterval(updateLastSeenTimes, 1000);

    const meshTab = document.querySelector('button[data-bs-target="#topology"]');
    if(meshTab) meshTab.addEventListener('click', async () => {
        const container = document.querySelector('.mesh-topology-container');
        if (container && container.innerHTML.trim() === "") {
             if(typeof window.loadMeshTopology === 'function') {
                 window.loadMeshTopology();
             }
        }
    });

    const settingsTab = document.querySelector('button[data-bs-target="#settings"]');
    if(settingsTab) {
        settingsTab.addEventListener('click', loadConfigYaml);
    }
});

// ============================================================================
// DEVICE LIST MANAGEMENT
// ============================================================================

async function fetchAllDevices() {
    try {
        const res = await fetch('/api/devices');
        if (!res.ok) throw new Error("API Error");
        const devices = await res.json();
        const tbody = document.getElementById('deviceTableBody');
        tbody.innerHTML = '';

        devices.forEach(d => {
            if (deviceCache[d.ieee]) {
                d.state = { ...deviceCache[d.ieee].state, ...d.state };
            }
            deviceCache[d.ieee] = d;
            if (!d.last_seen_ts) d.last_seen_ts = Date.now();

            let row = document.getElementById(`row-${d.ieee.replace(/:/g, '')}`);
            if (row) {
                updateDeviceRowCells(row, d);
            } else {
                tbody.appendChild(createDeviceRow(d));
            }
        });
    } catch (e) { console.error("Fetch devices failed:", e); }
}

function handleDeviceUpdate(payload) {
    const ieee = payload.ieee;
    const data = payload.data;
    if (!deviceCache[ieee]) return;

    if(data && Object.keys(data).length > 0) {
        if (!deviceCache[ieee].state) deviceCache[ieee].state = {};
        Object.assign(deviceCache[ieee].state, data);
        deviceCache[ieee].last_seen_ts = Date.now();

        const rowId = `row-${ieee.replace(/:/g, '')}`;
        const row = document.getElementById(rowId);

        if (row) {
            flashRow(row);
            updateDeviceRowCells(row, deviceCache[ieee]);
        }

        if (currentDeviceIeee === ieee) {
            refreshModalState(deviceCache[ieee]);
        }
    }
}

function flashRow(row) {
    if (!row) return;
    row.style.transition = "background-color 0.1s";
    row.style.backgroundColor = "rgba(255, 193, 7, 0.3)";
    setTimeout(() => {
        row.style.transition = "background-color 1.0s ease-out";
        row.style.backgroundColor = "";
    }, 200);
}

function removeDeviceRow(ieee) {
    const id = `row-${ieee.replace(/:/g, '')}`;
    const row = document.getElementById(id);
    if (row) row.remove();
    delete deviceCache[ieee];
}

function createDeviceRow(d) {
    const tr = document.createElement('tr');
    tr.id = `row-${d.ieee.replace(/:/g, '')}`;
    tr.innerHTML = getRowHtml(d);
    return tr;
}

function updateDeviceRowCells(row, d) {
    const lastSeenCell = row.querySelector('.last-seen');
    if (lastSeenCell) {
        lastSeenCell.setAttribute('data-ts', d.last_seen_ts);
        lastSeenCell.innerText = timeAgo(d.last_seen_ts);
    }

    const lqiCell = row.querySelector('.device-lqi');
    if (lqiCell) lqiCell.innerHTML = getLqiBadge(d.lqi);

    const statusCell = row.querySelector('.device-status-badges');
    if (statusCell) {
        // Status now includes the Online/Offline badge AND state badges
        let statusHtml = d.available !== false
            ? '<span class="badge bg-success me-1">Online</span>'
            : '<span class="badge bg-secondary me-1">Offline</span>';

        // Optionally append state badges to the main table view as requested
        // statusHtml += getDeviceStateHtml(d); // Uncomment if you want badges in the table too

        statusCell.innerHTML = statusHtml;
    }
}

function getRowHtml(d) {
    const quirkHtml = d.quirk && d.quirk !== 'None' && d.quirk !== 'Device'
        ? `<div class="text-muted" style="font-size:0.7rem"><i class="fas fa-magic"></i> ${d.quirk}</div>`
        : '';

    let statusHtml = d.available !== false
        ? '<span class="badge bg-success me-1">Online</span>'
        : '<span class="badge bg-secondary me-1">Offline</span>';

    // statusHtml += getDeviceStateHtml(d); // Keep main table clean, use modal for details

    return `
        <td class="text-center align-middle" style="font-size: 1.2rem;">${getTypeIcon(d.type)}</td>
        <td class="align-middle">
            <div class="fw-bold text-primary" style="cursor:pointer" onclick="renamePrompt('${d.ieee}', '${d.friendly_name}')">
                ${d.friendly_name} <i class="fas fa-pen fa-xs text-muted ms-1"></i>
            </div>
        </td>
        <td class="align-middle">
            <div class="font-monospace small text-muted">${d.ieee}</div>
        </td>
        <td class="align-middle small">
            <div>${d.manufacturer || '?'}</div>
            ${quirkHtml}
        </td>
        <td class="align-middle small">
            <div>${d.model || '?'}</div>
        </td>
        <td class="device-lqi align-middle">${getLqiBadge(d.lqi)}</td>
        <td class="last-seen align-middle" data-ts="${d.last_seen_ts}">${timeAgo(d.last_seen_ts)}</td>
        <td class="align-middle device-status-badges">
            ${statusHtml}
        </td>
        <td class="align-middle text-end">
            <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-primary" title="Details & Control" onclick='openDeviceModal(${JSON.stringify(d).replace(/'/g, "&#39;")})'><i class="fas fa-sliders-h"></i> Manage</button>
            </div>
        </td>
    `;
}

function getDeviceStateHtml(d) {
    if (!d.state) return '';

    const keys = Object.keys(d.state).filter(k =>
        k !== 'last_seen' &&
        k !== 'power_source' &&
        !k.startsWith('dp_') // Hide raw DPs from main view
    );

    if (keys.length === 0) return '';

    return keys.map(k => {
        let val = d.state[k];
        let style = "bg-light text-dark";
        let icon = "";

        // Standard States
        if (k === 'occupancy' || k === 'presence') {
            val = val ? 'MOTION' : 'CLEAR';
            style = d.state[k] ? "bg-danger text-white fw-bold" : "bg-success text-white";
            icon = d.state[k] ? '<i class="fas fa-running"></i> ' : '<i class="fas fa-user-slash"></i> ';
        }
        else if (k === 'illuminance_lux' || k === 'illuminance') { val = val + ' lx'; icon = '<i class="fas fa-sun"></i> '; }
        else if (k === 'temperature') { val = val + '°C'; icon = '<i class="fas fa-thermometer-half"></i> '; }
        else if (k === 'humidity') { val = val + '%'; icon = '<i class="fas fa-tint"></i> '; }

        // Tuya Radar Settings (Show in main table as requested)
        else if (k === 'radar_sensitivity') { val = 'Radar: ' + val; icon = '<i class="fas fa-satellite-dish"></i> '; }
        else if (k === 'presence_sensitivity') { val = 'Pres: ' + val; icon = '<i class="fas fa-user-clock"></i> '; }
        else if (k === 'keep_time') { val = 'Keep: ' + val + 's'; icon = '<i class="fas fa-hourglass-half"></i> '; }
        else if (k === 'distance') { val = val + 'm'; icon = '<i class="fas fa-ruler-horizontal"></i> '; }

        // Generic fallback for other keys like detection_distance_min/max if desired
        else {
            return ''; // Skip less important keys for main table to avoid clutter
        }

        return `<span class="badge ${style} border me-1 mb-1" style="font-size: 0.75em;">${icon}${val}</span>`;
    }).join(" ");
}

// ============================================================================
// DEVICE MODAL & TABS
// ============================================================================

function openDeviceModal(d) {
    const cachedDev = deviceCache[d.ieee] || d;
    currentDeviceIeee = cachedDev.ieee;

    const modalBody = document.getElementById('capModalBody');
    if (!modalBody) return;

    let html = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5>${cachedDev.friendly_name}</h5>
                <div class="text-muted small font-monospace">${cachedDev.ieee}</div>
            </div>
            <div>
                <span class="badge bg-secondary">${cachedDev.manufacturer}</span>
                <span class="badge bg-secondary">${cachedDev.model}</span>
            </div>
        </div>

        <ul class="nav nav-tabs mb-3" id="devTabs">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-overview">Overview</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-control">Control</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-caps">Clusters</button></li>
        </ul>

        <div class="tab-content">
            <div class="tab-pane fade show active" id="tab-overview">
                ${renderOverviewTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-control">
                ${renderControlTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-caps">
                ${renderCapsTab(cachedDev)}
            </div>
        </div>
    `;

    modalBody.innerHTML = html;
    const modalEl = document.getElementById('capModal');
    if (modalEl) new bootstrap.Modal(modalEl).show();
}

function refreshModalState(device) {
    const overviewTab = document.getElementById('tab-overview');
    if (overviewTab && overviewTab.closest('.tab-pane').classList.contains('active')) {
        overviewTab.innerHTML = renderOverviewTab(device);
    }
}

// --- OVERVIEW TAB: Consolidated State + Settings + Maintenance ---
function renderOverviewTab(device) {
    const s = device.state || {};
    const qos = device.settings?.qos || 0;

    // 1. Maintenance Header
    const maintenanceHtml = `
        <div class="d-flex justify-content-between align-items-center mb-3 p-2 bg-light border rounded">
            <span class="fw-bold text-secondary"><i class="fas fa-tools"></i> Maintenance</span>
            <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-secondary" onclick="doAction('poll', '${device.ieee}')">
                    <i class="fas fa-sync"></i> Poll
                </button>
                <button class="btn btn-outline-primary" onclick="doAction('interview', '${device.ieee}')">
                    <i class="fas fa-fingerprint"></i> Re-Interview
                </button>
                <button class="btn btn-outline-danger" onclick="doAction('remove', '${device.ieee}')">
                    <i class="fas fa-trash"></i> Remove
                </button>
            </div>
        </div>
    `;

    // 2. State Lists (Categorized)
    const knownSettings = [
        'radar_sensitivity', 'presence_sensitivity', 'keep_time',
        'detection_distance_min', 'detection_distance_max'
    ];
    const ignoredKeys = ['last_seen', 'power_source', 'manufacturer', 'model'];

    let sensorStateHtml = '';
    let unknownStateHtml = '';

    if (device.state) {
        // Live Sensors (States)
        const stateKeys = Object.keys(device.state).filter(k =>
            !knownSettings.includes(k) &&
            !ignoredKeys.includes(k) &&
            !k.startsWith('dp_')
        ).sort();

        if (stateKeys.length > 0) {
            sensorStateHtml = stateKeys.map(k => `
                <tr>
                    <td class="fw-bold small">${k}</td>
                    <td class="font-monospace small text-end">${device.state[k]}</td>
                </tr>
            `).join('');
        } else {
            sensorStateHtml = '<tr><td colspan="2" class="text-center text-muted small">No live sensor data.</td></tr>';
        }

        // Diagnostics (Unknowns/DPs)
        const diagKeys = Object.keys(device.state).filter(k => k.startsWith('dp_')).sort();
        if (diagKeys.length > 0) {
            unknownStateHtml = diagKeys.map(k => `
                <tr>
                    <td class="fw-bold small">${k}</td>
                    <td class="font-monospace small text-end">${device.state[k]}</td>
                </tr>
            `).join('');
        }
    }

    const sensorCardHtml = `
        <div class="card h-100 mb-3">
            <div class="card-header py-1 bg-white fw-bold text-success">
                <i class="fas fa-chart-bar"></i> Sensor States
            </div>
            <div class="card-body p-0">
                <table class="table table-sm table-striped mb-0">
                    <tbody>${sensorStateHtml}</tbody>
                </table>
            </div>
        </div>
    `;

    const diagnosticCardHtml = unknownStateHtml ? `
        <div class="accordion mb-3" id="diagAccordion">
            <div class="accordion-item">
                <h2 class="accordion-header">
                    <button class="accordion-button collapsed py-2" type="button" data-bs-toggle="collapse" data-bs-target="#collapseDiag">
                        <i class="fas fa-bug me-2"></i> Diagnostics (Raw DPs)
                    </button>
                </h2>
                <div id="collapseDiag" class="accordion-collapse collapse" data-bs-parent="#diagAccordion">
                    <div class="accordion-body p-0">
                        <table class="table table-sm table-hover mb-0 text-muted">
                            <tbody>${unknownStateHtml}</tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    ` : '';

    // 3. Configuration Form
    let tuyaFormHtml = '';
    // Check if it's a Tuya device
    const isTuya = (device.manufacturer && (device.manufacturer.includes('_TZE204') || device.manufacturer.includes('_TZE200')))
                   || s.radar_sensitivity !== undefined
                   || s.presence_sensitivity !== undefined;

    if (isTuya) {
        tuyaFormHtml = `
            <h6 class="text-primary mt-3 mb-2 border-bottom pb-1">Radar Configuration</h6>
            <div class="row g-2">
                <div class="col-md-6">
                    <label class="form-label x-small mb-0 fw-bold">Radar Sensitivity (0-10)</label>
                    <input type="number" class="form-control form-control-sm" name="tuya_radar_sensitivity"
                           value="${s.radar_sensitivity ?? ''}" min="0" max="10">
                </div>
                <div class="col-md-6">
                    <label class="form-label x-small mb-0 fw-bold">Presence Sensitivity (0-10)</label>
                    <input type="number" class="form-control form-control-sm" name="tuya_presence_sensitivity"
                           value="${s.presence_sensitivity ?? ''}" min="0" max="10">
                </div>
                <div class="col-md-12">
                    <label class="form-label x-small mb-0 fw-bold">Keep Time (s)</label>
                    <input type="number" class="form-control form-control-sm" name="tuya_keep_time"
                           value="${s.keep_time ?? ''}" min="0" max="3600">
                    <div class="form-text x-small mt-0">Time to hold presence after motion stops.</div>
                </div>
                <div class="col-md-12">
                    <label class="form-label x-small mb-0 fw-bold">Detection Range (m)</label>
                    <div class="input-group input-group-sm">
                        <span class="input-group-text">Min</span>
                        <input type="number" step="0.01" class="form-control" name="tuya_detection_distance_min"
                               value="${s.detection_distance_min ?? ''}">
                        <span class="input-group-text">Max</span>
                        <input type="number" step="0.01" class="form-control" name="tuya_detection_distance_max"
                               value="${s.detection_distance_max ?? ''}">
                    </div>
                </div>
            </div>
        `;
    }

    return `
        <form id="configForm" onsubmit="saveConfig(event)">
            ${maintenanceHtml}
            <div class="row">
                <div class="col-md-5">
                    ${sensorCardHtml}
                    ${diagnosticCardHtml}
                </div>
                <div class="col-md-7">
                    <div class="card h-100">
                        <div class="card-header py-1 bg-white fw-bold text-primary">
                            <i class="fas fa-sliders-h"></i> Configuration
                        </div>
                        <div class="card-body">
                            <div class="mb-2">
                                <label class="form-label x-small mb-0 fw-bold">MQTT QoS</label>
                                <select class="form-select form-select-sm" name="qos">
                                    <option value="0" ${qos==0?'selected':''}>0 (Normal)</option>
                                    <option value="1" ${qos==1?'selected':''}>1 (At Least Once)</option>
                                    <option value="2" ${qos==2?'selected':''}>2 (Critical)</option>
                                </select>
                            </div>
                            ${tuyaFormHtml}
                            <div class="mt-3 pt-2 border-top text-end">
                                <button type="submit" id="saveConfigBtn" class="btn btn-sm btn-primary w-100">
                                    <i class="fas fa-save"></i> Apply Settings
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </form>
    `;
}

async function saveConfig(e) {
    e.preventDefault();
    const btn = document.getElementById('saveConfigBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Applying...';

    const formData = new FormData(e.target);
    const reporting = {};
    const tuyaSettings = {};
    let qos = 0;

    for (let [key, value] of formData.entries()) {
        if (value === "") continue;

        if (key === 'qos') {
            qos = parseInt(value);
            continue;
        }

        if (key.startsWith('tuya_')) {
            // Remove 'tuya_' prefix so backend gets correct keys
            tuyaSettings[key.replace('tuya_', '')] = parseFloat(value);
            continue;
        }

        const [cluster, param] = key.split('_');
        if (cluster && param) {
            if (!reporting[cluster]) reporting[cluster] = {};
            reporting[cluster][param] = parseInt(value);
        }
    }

    try {
        const res = await fetch('/api/device/configure', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                ieee: currentDeviceIeee,
                qos: qos,
                reporting: reporting,
                tuya_settings: tuyaSettings
            })
        });
        const data = await res.json();
        if (data.success) {
            btn.innerHTML = '<i class="fas fa-check"></i> Done';
            btn.classList.replace('btn-primary', 'btn-success');

            // Auto-poll to see updates
            await doAction('poll', currentDeviceIeee);

            setTimeout(() => {
                btn.innerHTML = originalText;
                btn.classList.replace('btn-success', 'btn-primary');
                btn.disabled = false;
            }, 1500);
        } else {
            throw new Error(data.error);
        }
    } catch (err) {
        alert("Error: " + err.message);
        btn.disabled = false;
        btn.innerHTML = originalText;
    }
}

// --- CONTROL TAB (Restored from previous) ---
function renderControlTab(device) {
    const state = device.state || {};
    let html = '<div class="row g-3">';

    // On/Off control
    if (state.state !== undefined || state.on !== undefined) {
        html += `
            <div class="col-12">
                <div class="card">
                    <div class="card-body">
                        <h6 class="card-title"><i class="fas fa-power-off"></i> Power</h6>
                        <div class="btn-group">
                            <button class="btn btn-success" onclick="sendCommand('${device.ieee}', 'on')"><i class="fas fa-toggle-on"></i> On</button>
                            <button class="btn btn-secondary" onclick="sendCommand('${device.ieee}', 'off')"><i class="fas fa-toggle-off"></i> Off</button>
                            <button class="btn btn-outline-primary" onclick="sendCommand('${device.ieee}', 'toggle')"><i class="fas fa-sync"></i> Toggle</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    // Thermostat, Color, Brightness (Simplified check for standard controls)
    if (state.heating_setpoint !== undefined) {
        const setpoint = state.heating_setpoint || 20;
        html += `
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h6 class="card-title"><i class="fas fa-fire"></i> Heating</h6>
                        <div class="input-group">
                            <button class="btn btn-outline-secondary" onclick="adjustSetpoint('${device.ieee}', -0.5)">-</button>
                            <input type="number" class="form-control text-center" value="${setpoint}" id="setpoint-input" onchange="sendCommand('${device.ieee}', 'temperature', this.value)">
                            <button class="btn btn-outline-secondary" onclick="adjustSetpoint('${device.ieee}', 0.5)">+</button>
                        </div>
                    </div>
                </div>
            </div>`;
    }

    // Identify button
    html += `
        <div class="col-12">
            <div class="card">
                <div class="card-body">
                    <h6 class="card-title"><i class="fas fa-search-location"></i> Identify</h6>
                    <button class="btn btn-info" onclick="sendCommand('${device.ieee}', 'identify')">
                        <i class="fas fa-lightbulb"></i> Identify Device (Flash for 5s)
                    </button>
                </div>
            </div>
        </div>
    `;
    html += '</div>';
    return html;
}

// --- CLUSTERS TAB ---
function renderCapsTab(device) {
    if (!device.capabilities || !Array.isArray(device.capabilities) || device.capabilities.length === 0) {
        return '<div class="alert alert-warning">No capability data available.</div>';
    }
    let html = `<div class="accordion" id="epAccordion">`;
    device.capabilities.forEach((ep, idx) => {
        const inputs = Array.isArray(ep.inputs) ? ep.inputs : [];
        const outputs = Array.isArray(ep.outputs) ? ep.outputs : [];
        const inC = inputs.map(c => `<span class="badge bg-light text-dark border m-1">${c.name} (0x${c.id.toString(16)})</span>`).join('');
        const outC = outputs.map(c => `<span class="badge bg-light text-dark border m-1">${c.name} (0x${c.id.toString(16)})</span>`).join('');
        html += `
            <div class="accordion-item">
                <h2 class="accordion-header">
                    <button class="accordion-button ${idx !== 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${ep.id}">
                        Endpoint ${ep.id} <span class="ms-2 badge bg-primary">${ep.profile || '?'}</span>
                    </button>
                </h2>
                <div id="collapse${ep.id}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#epAccordion">
                    <div class="accordion-body">
                        <small class="text-muted d-block mb-2">Input Clusters (Server):</small>
                        <div class="d-flex flex-wrap mb-3">${inC || 'None'}</div>
                        <small class="text-muted d-block mb-2">Output Clusters (Client):</small>
                        <div class="d-flex flex-wrap">${outC || 'None'}</div>
                    </div>
                </div>
            </div>`;
    });
    html += `</div>`;
    return html;
}

// Helper Functions
async function doAction(action, ieee) {
    if (action === 'remove' && !confirm("Are you sure?")) return;
    try {
        const res = await fetch(`/api/device/${action}`, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ieee: ieee, force: false})
        });
        const data = await res.json();
        if(data.success) addLogEntry({timestamp: new Date().toLocaleTimeString(), level: 'INFO', message: `${action.toUpperCase()} sent.`});
        else alert(`Error: ${data.error}`);
    } catch (e) { console.error(e); }
}

async function sendCommand(ieee, command, value = null) {
    try {
        const res = await fetch('/api/device/command', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ieee: ieee, command: command, value: value })
        });
        const data = await res.json();
        if (data.success) addLogEntry({timestamp: new Date().toLocaleTimeString(), level: 'INFO', message: `Command sent`});
        else alert(`Error: ${data.error}`);
    } catch (e) { alert("Command failed"); }
}

function adjustSetpoint(ieee, delta) {
    const input = document.getElementById('setpoint-input');
    if (input) {
        const newVal = parseFloat(input.value) + delta;
        input.value = newVal.toFixed(1);
        sendCommand(ieee, 'temperature', newVal);
    }
}

async function renamePrompt(ieee, oldName) {
    const name = prompt("Rename:", oldName);
    if (name && name !== oldName) {
        await fetch('/api/device/rename', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ieee:ieee, name:name})
        });
        fetchAllDevices();
    }
}

function togglePairing() {
    fetch("/api/permit_join", {method: "POST"}).then(r=>r.json()).then(d => {
        if(d.status === 'scanning') updatePairingUI(240);
    });
}

function updatePairingUI(time) {
    const btn = document.getElementById('pairBtn');
    if(!btn) return;
    btn.disabled = true;
    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Pairing (${time}s)`;
    let timeLeft = time;
    const interval = setInterval(() => {
        timeLeft--;
        if (timeLeft <= 0) {
            clearInterval(interval);
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-plus-circle"></i> Enable Pairing`;
        } else {
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Pairing (${timeLeft}s)`;
        }
    }, 1000);
}

function getTypeIcon(type) {
    if (type === 'Coordinator') return '<i class="fas fa-network-wired text-primary"></i>';
    if (type === 'Router') return '<i class="fas fa-wifi text-success"></i>';
    return '<i class="fas fa-battery-three-quarters text-warning"></i>';
}

function getLqiBadge(lqi) {
    let color = 'bg-secondary';
    if (lqi > 150) color = 'bg-success';
    else if (lqi > 80) color = 'bg-warning text-dark';
    else if (lqi > 0) color = 'bg-danger';
    return `<span class="badge ${color}">${lqi}</span>`;
}

function timeAgo(ts) {
    if (!ts) return "Never";
    const seconds = Math.floor((Date.now() - ts) / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds/60)}m ago`;
    return `${Math.floor(seconds/3600)}h ago`;
}

function updateLastSeenTimes() {
    document.querySelectorAll('.last-seen').forEach(cell => {
        const ts = parseInt(cell.getAttribute('data-ts'));
        if (ts > 0) cell.innerText = timeAgo(ts);
    });
}

// Config & Debug
async function loadConfigYaml() {
    const editor = document.getElementById('configEditor');
    if(!editor) return;
    try {
        const res = await fetch('/api/config');
        const data = await res.json();
        if(data.success) editor.value = data.content;
    } catch(e) {}
}
async function saveConfigYaml() {
    const editor = document.getElementById('configEditor');
    if(!editor || !confirm("Save?")) return;
    await fetch('/api/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ content: editor.value })
    });
    alert("Saved");
}
async function restartSystem() {
    if(!confirm("Restart?")) return;
    isRestarting = true;
    await fetch('/api/system/restart', { method: 'POST' });
    setTimeout(() => location.reload(), 15000);
}

function addLogEntry(log) {
    allLogs.push(log);
    if (allLogs.length > 500) allLogs.shift();
    renderLogs();
}
function renderLogs() {
    const container = document.getElementById('logs');
    if(!container) return;
    const levelFilter = document.getElementById('logLevelFilter').value;
    const visibleLogs = allLogs.filter(l => levelFilter === 'ALL' || l.level === levelFilter).slice(-100);
    container.innerHTML = visibleLogs.map(l => {
        let color = '#ccc';
        if (l.level === 'INFO') color = '#4CAF50';
        else if (l.level === 'WARNING') color = '#FFC107';
        else if (l.level === 'ERROR') color = '#F44336';
        else if (l.level === 'DEBUG') color = '#2196F3';
        return `<div class="border-bottom border-secondary pb-1 mb-1" style="font-family: monospace; font-size: 0.85rem;"><span class="text-muted">[${l.timestamp}]</span> <span style="color:${color}" class="fw-bold">[${l.level}]</span> ${l.message}</div>`;
    }).join('');
    container.scrollTop = container.scrollHeight;
}
function filterLogs() { renderLogs(); }
function clearLogs() { allLogs = []; renderLogs(); }
async function checkDebugStatus() {
    try {
        const res = await fetch('/api/debug/status');
        const data = await res.json();
        updateDebugStatus(data);
    } catch(e) {}
}
function updateDebugStatus(data) {
    debugEnabled = data.enabled || false;
    const badge = document.getElementById('debugStatusBadge');
    const enableBtn = document.getElementById('debugEnableBtn');
    const disableBtn = document.getElementById('debugDisableBtn');
    if(debugEnabled) {
        if(badge) badge.innerHTML = '<span class="badge bg-success">Active</span>';
        if(enableBtn) enableBtn.classList.add('d-none');
        if(disableBtn) disableBtn.classList.remove('d-none');
    } else {
        if(badge) badge.innerHTML = '<span class="badge bg-secondary">Disabled</span>';
        if(enableBtn) enableBtn.classList.remove('d-none');
        if(disableBtn) disableBtn.classList.add('d-none');
    }
}
async function toggleDebug(enable) {
    const endpoint = enable ? '/api/debug/enable' : '/api/debug/disable';
    await fetch(endpoint, { method: 'POST' });
    checkDebugStatus();
}
async function viewDebugPackets() {
    const modal = new bootstrap.Modal(document.getElementById('debugPacketsModal'));
    modal.show();
    refreshDebugPackets();
}
async function refreshDebugPackets() {
    const content = document.getElementById('debugPacketsContent');
    content.innerHTML = '<div class="text-center p-4"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';
    try {
        const res = await fetch('/api/debug/packets?limit=100');
        const data = await res.json();
        if(data.success) {
            let html = '<table class="table table-sm table-hover"><thead><tr><th>Time</th><th>Cluster</th><th>Cmd</th><th>Payload</th></tr></thead><tbody>';
            data.packets.forEach(p => {
                // REINSTATED: Full JSON payload display with no truncation
                html += `<tr>
                    <td class="small">${p.timestamp_str}</td>
                    <td>${p.cluster_name}</td>
                    <td>${p.decoded.command_name || p.decoded.command_id_hex}</td>
                    <td class="small text-muted">
                        <pre class="m-0" style="white-space: pre-wrap; word-break: break-all; font-size: 0.75rem;">${JSON.stringify(p.decoded, null, 2)}</pre>
                    </td>
                </tr>`;
            });
            html += '</tbody></table>';
            content.innerHTML = html;
        }
    } catch(e) { content.innerHTML = 'Error loading packets'; }
}
function handleLivePacket(p) {
    // Live packet logic
    const tbody = document.querySelector('#debugPacketsContent tbody');
    if (tbody) {
        const rowHtml = `
            <tr style="animation: highlight 1s">
                <td class="small">${p.timestamp_str}</td>
                <td>${p.cluster_name}</td>
                <td>${p.decoded.command_name || p.decoded.command_id_hex}</td>
                <td class="small text-muted">
                    <pre class="m-0" style="white-space: pre-wrap; word-break: break-all; font-size: 0.75rem;">${JSON.stringify(p.decoded, null, 2)}</pre>
                </td>
            </tr>
        `;
        tbody.insertAdjacentHTML('afterbegin', rowHtml);
        if (tbody.rows.length > 100) tbody.lastElementChild.remove();
    }
}
async function downloadDebugLog() {
    window.open('/api/debug/log_file?lines=2000', '_blank');
}