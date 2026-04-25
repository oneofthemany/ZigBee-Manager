/**
 * zones.js — Frontend for RSSI-to-coordinator presence detection zones.
 *
 * Model (v2):
 *   - Each zone tracks PER-DEVICE RSSI received at the coordinator.
 *   - User must explicitly start calibration when the room is empty.
 *   - Aggressiveness (σ multiplier) is only editable for mains-fed devices.
 */

// ============================================================================
// STATE
// ============================================================================
let zonesData = new Map();
let deviceListCache = [];
const selectedDevices = new Set();

// ============================================================================
// INIT
// ============================================================================
export function initZones() {
    console.log("Initializing Zones Module (v2)...");

    fetchZones();

    document.querySelector('button[data-bs-target="#zones"]')
        ?.addEventListener('click', fetchZones);
    document.getElementById('btn-refresh-zones')?.addEventListener('click', fetchZones);
    document.getElementById('btn-create-zone')?.addEventListener('click', openCreateZoneModal);
    document.getElementById('btn-save-zone')?.addEventListener('click', handleCreateZoneSubmit);
    document.getElementById('zone-device-search')
        ?.addEventListener('keyup', (e) => filterDeviceList(e.target.value));

    // Live updates
    window.addEventListener('zone-update', (event) => {
        const type = event.detail.type;
        const payload = event.detail.payload;

        if (type === 'zone_calibration') {
            handleCalibrationProgress(payload);
            return;
        }
        if (type === 'zone_update' && payload?.zone) {
            const zoneData = payload.zone;
            zonesData.set(zoneData.name, zoneData);

            // Refresh modal if open for this zone
            const modalEl = document.getElementById('zoneDetailsModal');
            const modalTitle = document.getElementById('zone-details-title');
            if (modalEl?.classList.contains('show')
                && modalTitle?.innerText === zoneData.name) {
                renderZoneModalContent(zoneData, modalEl.querySelector('.modal-body'), false);
            }
            renderZonesGrid();
        }
    });

    console.log("✅ Zone listeners registered");
}

// ============================================================================
// API
// ============================================================================
async function fetchZones() {
    try {
        const r = await fetch('/api/zones');
        if (!r.ok) throw new Error("Failed to fetch zones");
        const zones = await r.json();
        zonesData.clear();
        zones.forEach(z => zonesData.set(z.name, z));
        renderZonesGrid();
    } catch (e) {
        console.error("Error fetching zones:", e);
        const c = document.getElementById('zones-container');
        if (c) c.innerHTML =
            `<div class="col-12 text-center text-danger">Failed to load zones: ${e.message}</div>`;
    }
}

async function fetchDevicesForModal() {
    try {
        const r = await fetch('/api/devices');
        if (!r.ok) throw new Error("Failed to fetch devices");
        deviceListCache = await r.json();
        return deviceListCache;
    } catch (e) {
        console.error("Error fetching devices:", e);
        return [];
    }
}

// ============================================================================
// GRID
// ============================================================================
function renderZonesGrid() {
    const container = document.getElementById('zones-container');
    if (!container) return;
    container.innerHTML = '';
    if (zonesData.size === 0) {
        container.innerHTML = `
            <div class="col-12 text-center text-muted py-5">
                <i class="bi bi-inbox fs-1"></i>
                <p class="mt-2">No zones created yet.</p>
            </div>`;
        return;
    }
    zonesData.forEach(z => container.appendChild(createZoneCard(z)));
}

function createZoneCard(zone) {
    const col = document.createElement('div');
    col.className = 'col-md-6 col-lg-4 mb-4';

    const stateColors = {
        occupied: 'success', vacant: 'secondary',
        calibrating: 'warning', uncalibrated: 'danger',
    };
    const stateColor = stateColors[zone.state] || 'primary';
    const isOccupied = zone.state === 'occupied';

    const needsCalibration = zone.state === 'uncalibrated';

    col.innerHTML = `
        <div class="card h-100 shadow-sm border-${isOccupied ? 'success' : 'light'}">
            <div class="card-header bg-transparent d-flex justify-content-between align-items-center">
                <h5 class="mb-0 text-truncate" title="${zone.name}">${zone.name}</h5>
                <span class="badge bg-${stateColor} text-uppercase">${zone.state}</span>
            </div>
            <div class="card-body">
                <div class="d-flex justify-content-between mb-2">
                    <span class="text-muted">Devices:</span><strong>${zone.device_count}</strong>
                </div>
                ${zone.occupied_since ? `
                    <div class="alert alert-success py-1 small mb-3">
                        <i class="bi bi-clock-history"></i>
                        Since ${new Date(zone.occupied_since * 1000).toLocaleTimeString()}
                    </div>` : ''}
                ${needsCalibration ? `
                    <div class="alert alert-warning py-1 small mb-3">
                        <i class="bi bi-exclamation-triangle"></i>
                        Not calibrated. Open details and start calibration with room empty.
                    </div>` : ''}
            </div>
            <div class="card-footer bg-transparent border-top-0 d-flex gap-2">
                <button class="btn btn-sm btn-outline-primary flex-grow-1"
                        onclick="window.viewZoneDetails('${zone.name}')">
                    <i class="bi bi-graph-up"></i> Details
                </button>
                <button class="btn btn-sm btn-outline-danger"
                        onclick="window.deleteZone('${zone.name}')" title="Delete">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
        </div>`;
    return col;
}

// ============================================================================
// DETAILS MODAL
// ============================================================================
export async function viewZoneDetails(zoneName) {
    const zone = zonesData.get(zoneName);
    if (!zone) return;
    await fetchDevicesForModal();

    document.getElementById('zone-details-title').innerText = zone.name;
    const body = document.querySelector('#zoneDetailsModal .modal-body');
    renderZoneModalContent(zone, body, true);

    new bootstrap.Modal(document.getElementById('zoneDetailsModal')).show();
}

function renderZoneModalContent(zone, container, fullRender = true) {
    const devices = zone.devices || {};
    const entries = Object.entries(devices);

    // --- Status + controls header ---
    const canCalibrate = zone.state === 'uncalibrated' || zone.state === 'vacant' || zone.state === 'occupied';
    const isCalibrating = zone.state === 'calibrating';

    const headerHtml = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <span>
                <strong>State:</strong>
                <span class="badge bg-${zone.state === 'occupied' ? 'success'
                                        : zone.state === 'calibrating' ? 'warning'
                                        : zone.state === 'uncalibrated' ? 'danger' : 'secondary'}">
                    ${zone.state}
                </span>
                ${zone.occupied_since
                    ? `<span class="badge bg-light text-dark border ms-2">Since ${new Date(zone.occupied_since * 1000).toLocaleTimeString()}</span>`
                    : ''}
            </span>
            <div class="btn-group btn-group-sm">
                ${canCalibrate ? `
                    <button class="btn btn-warning" onclick="window.startZoneCalibration('${zone.name}')">
                        <i class="bi bi-record-circle"></i> Calibrate (room empty)
                    </button>` : ''}
                ${isCalibrating ? `
                    <button class="btn btn-success" onclick="window.stopZoneCalibration('${zone.name}')">
                        <i class="bi bi-check-circle"></i> Finalize now
                    </button>
                    <button class="btn btn-outline-secondary" onclick="window.cancelZoneCalibration('${zone.name}')">
                        <i class="bi bi-x-circle"></i> Cancel
                    </button>` : ''}
            </div>
        </div>`;

    // --- Calibration progress block ---
    let progressHtml = '';
    if (isCalibrating && zone.calibration_start) {
        const total = zone.config?.calibration_time || 120;
        const elapsed = Math.floor((Date.now() / 1000) - zone.calibration_start);
        const pct = Math.min(100, Math.floor((elapsed / total) * 100));
        progressHtml = `
            <div class="alert alert-info">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <strong><i class="bi bi-arrow-repeat"></i> Calibrating — keep the room empty</strong>
                    <span class="badge bg-primary fs-6">${pct}%</span>
                </div>
                <div class="progress" style="height: 20px;">
                    <div class="progress-bar progress-bar-striped progress-bar-animated bg-info"
                         style="width: ${pct}%">${elapsed}s / ${total}s</div>
                </div>
            </div>`;
    }

    // --- Device table (per-device RSSI stats) ---
    const deviceThreshold = zone.config?.deviation_threshold || 2.5;
    let devicesHtml = '';
    if (entries.length === 0) {
        devicesHtml = '<div class="alert alert-secondary">No samples captured yet.</div>';
    } else {
        const rows = entries.map(([ieee, d]) => {
            const friendly = (deviceListCache.find(x => x.ieee.toLowerCase() === ieee.toLowerCase())?.friendly_name) || ieee;
            const rssi = d.last_rssi ?? '-';
            const smoothed = d.smoothed_rssi != null ? d.smoothed_rssi.toFixed(1) : '-';
            const baseline = d.baseline_mean != null ? `${d.baseline_mean.toFixed(1)} ±${(d.baseline_std ?? 0).toFixed(1)}` : '-';
            const dev = d.deviation != null ? d.deviation.toFixed(2) : '-';
            const effThreshold = (deviceThreshold * (d.aggressiveness || 1.0)).toFixed(2);
            const triggered = d.deviation != null && d.deviation > (deviceThreshold * (d.aggressiveness || 1.0));
            const rowClass = triggered ? 'table-danger fw-bold' : '';

            // RSSI colour
            let rssiClass = 'secondary';
            if (d.last_rssi > -70) rssiClass = 'success';
            else if (d.last_rssi > -80) rssiClass = 'warning';
            else if (d.last_rssi != null) rssiClass = 'danger';

            // Aggressiveness control — routers only
            const aggControl = d.is_router
                ? `<div class="input-group input-group-sm">
                        <input type="number" step="0.1" min="0.5" max="2.0"
                            value="${(d.aggressiveness ?? 1.0).toFixed(1)}"
                            class="form-control form-control-sm agg-input"
                            data-ieee="${ieee}" style="max-width:70px;">
                        <button class="btn btn-outline-primary btn-sm"
                            onclick="window.setZoneAggressiveness('${zone.name}', '${ieee}')">Set</button>
                   </div>`
                : `<span class="badge bg-light text-muted border">
                        End-device (fixed ${(d.aggressiveness ?? 1.0).toFixed(1)}σ)
                   </span>`;

            return `
                <tr class="${rowClass}">
                    <td>
                        <div class="small fw-bold text-truncate" style="max-width:180px" title="${ieee}">${friendly}</div>
                        <div class="text-muted small font-monospace">${ieee.slice(-11)}</div>
                        <span class="badge bg-${d.is_router ? 'primary' : 'secondary'}">
                            ${d.is_router ? 'Router' : 'End-device'}
                        </span>
                    </td>
                    <td class="text-center"><span class="badge bg-${rssiClass}">${rssi}</span></td>
                    <td class="text-center"><code>${smoothed}</code></td>
                    <td class="text-center small">${baseline}</td>
                    <td class="text-center">${dev}σ<br><small class="text-muted">thr ${effThreshold}σ</small></td>
                    <td class="text-center small">${d.sample_count ?? 0}</td>
                    <td>${aggControl}</td>
                </tr>`;
        }).join('');

        devicesHtml = `
            <div class="table-responsive">
                <table class="table table-sm table-striped small align-middle">
                    <thead>
                        <tr>
                            <th>Device</th>
                            <th class="text-center">RSSI</th>
                            <th class="text-center">Smoothed</th>
                            <th class="text-center">Baseline μ±σ</th>
                            <th class="text-center">Dev</th>
                            <th class="text-center">N</th>
                            <th>Aggressiveness (routers only)</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    // --- Devices tab (membership) ---
    const deviceList = zone.device_ieees || [];
    const membershipRows = deviceList.map(ieee => {
        const d = deviceListCache.find(x => x.ieee.toLowerCase() === ieee.toLowerCase())
                  || { friendly_name: ieee, model: 'Unknown' };
        const displayName = d.friendly_name === ieee ? ieee : `${d.friendly_name} (${ieee})`;
        return `
            <li class="list-group-item d-flex justify-content-between align-items-center">
                <div>
                    <strong>${displayName}</strong><br>
                    <small class="text-muted">${d.model}</small>
                </div>
                <button class="btn btn-sm btn-outline-danger"
                        onclick="window.removeDeviceFromZone('${zone.name}', '${ieee}')" title="Remove">
                    <i class="bi bi-trash"></i>
                </button>
            </li>`;
    }).join('');

    const availableDevices = deviceListCache.filter(d => !deviceList.includes(d.ieee.toLowerCase()));
    const addOptions = availableDevices.map(d =>
        `<option value="${d.ieee}">${d.friendly_name || d.ieee}</option>`).join('');

    const devicesTab = `
        <div class="card mb-3">
            <div class="card-header bg-light small fw-bold">Add Device</div>
            <div class="card-body p-2">
                <div class="input-group">
                    <select class="form-select form-select-sm" id="zone-add-select">
                        <option value="">Select device...</option>
                        ${addOptions}
                    </select>
                    <button class="btn btn-sm btn-success"
                            onclick="window.addDeviceToZoneFromModal('${zone.name}')">Add</button>
                </div>
            </div>
        </div>
        <h6 class="small text-muted mb-2">Devices in Zone (${deviceList.length})</h6>
        <ul class="list-group list-group-flush border rounded overflow-auto" style="max-height:300px;">
            ${membershipRows}
        </ul>`;

    if (fullRender) {
        container.innerHTML = `
            ${headerHtml}
            ${progressHtml}
            <ul class="nav nav-tabs mb-3" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link active" data-bs-toggle="tab"
                            data-bs-target="#tab-stats" type="button">Live RSSI</button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" data-bs-toggle="tab"
                            data-bs-target="#tab-devices" type="button">Managed Devices</button>
                </li>
            </ul>
            <div class="tab-content">
                <div class="tab-pane fade show active" id="tab-stats">${devicesHtml}</div>
                <div class="tab-pane fade" id="tab-devices">${devicesTab}</div>
            </div>`;
    } else {
        // Partial update: header + progress + stats; preserve tab state
        const hdr = container.querySelector('.d-flex.justify-content-between');
        if (hdr) hdr.outerHTML = headerHtml;

        // Progress block
        let prog = container.querySelector('.alert.alert-info');
        if (isCalibrating && zone.calibration_start) {
            const tpl = document.createElement('div');
            tpl.innerHTML = progressHtml.trim();
            const newProg = tpl.firstChild;
            if (prog) prog.replaceWith(newProg);
            else container.querySelector('.nav-tabs')?.before(newProg);
        } else if (prog) {
            prog.remove();
        }

        const stats = container.querySelector('#tab-stats');
        if (stats) stats.innerHTML = devicesHtml;
        const devs = container.querySelector('#tab-devices');
        if (devs) devs.innerHTML = devicesTab;
    }
}

// ============================================================================
// CALIBRATION PROGRESS (WS)
// ============================================================================
function handleCalibrationProgress(data) {
    const modal = document.getElementById('zoneDetailsModal');
    if (!modal?.classList.contains('show')) return;
    const title = modal.querySelector('#zone-details-title');
    if (!title?.textContent.includes(data.zone_name)) return;

    // Patch the zone cache and re-render
    const z = zonesData.get(data.zone_name);
    if (z) {
        z.state = data.state;
        if (data.devices) {
            // Merge device stats from WS payload
            z.devices = data.devices;
        }
        renderZoneModalContent(z, modal.querySelector('.modal-body'), false);
    }
}

// ============================================================================
// ACTIONS
// ============================================================================
export async function startZoneCalibration(zoneName) {
    if (!confirm(`Start calibration for "${zoneName}"?\nLeave the room EMPTY until calibration completes.`)) return;
    try {
        const r = await fetch(`/api/zones/${zoneName}/calibrate/start`, { method: 'POST' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Failed to start');
        const data = await r.json();
        if (data.zone) zonesData.set(zoneName, data.zone);
        fetchZones();
    } catch (e) { alert("Error: " + e.message); }
}

export async function stopZoneCalibration(zoneName) {
    try {
        const r = await fetch(`/api/zones/${zoneName}/calibrate/stop`, { method: 'POST' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Failed to stop');
        const data = await r.json();
        alert(`Calibration complete: ${data.ready_devices} device baselines computed.`);
        fetchZones();
    } catch (e) { alert("Error: " + e.message); }
}

export async function cancelZoneCalibration(zoneName) {
    try {
        await fetch(`/api/zones/${zoneName}/calibrate/cancel`, { method: 'POST' });
        fetchZones();
    } catch (e) { alert("Error: " + e.message); }
}

export async function setZoneAggressiveness(zoneName, ieee) {
    const input = document.querySelector(`.agg-input[data-ieee="${ieee}"]`);
    if (!input) return;
    const value = parseFloat(input.value);
    if (isNaN(value) || value < 0.5 || value > 2.0) {
        return alert("Aggressiveness must be between 0.5 and 2.0");
    }
    try {
        const r = await fetch(`/api/zones/${zoneName}/devices/${ieee}/aggressiveness`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Failed');
        fetchZones();
    } catch (e) { alert("Error: " + e.message); }
}

export async function addDeviceToZoneFromModal(zoneName) {
    const select = document.getElementById('zone-add-select');
    const ieee = select?.value;
    if (!ieee) return;
    try {
        const r = await fetch(`/api/zones/${zoneName}/devices`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ add: [ieee], remove: [] }),
        });
        if (!r.ok) throw new Error("Failed to add device");
        await fetchZones();
        viewZoneDetails(zoneName);
    } catch (e) { alert("Error adding device: " + e.message); }
}

export async function removeDeviceFromZone(zoneName, ieee) {
    if (!confirm(`Remove device ${ieee} from zone? Zone will need recalibration.`)) return;
    try {
        const r = await fetch(`/api/zones/${zoneName}/devices`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ add: [], remove: [ieee] }),
        });
        if (!r.ok) throw new Error("Failed to remove device");
        await fetchZones();
        viewZoneDetails(zoneName);
    } catch (e) { alert("Error removing device: " + e.message); }
}

export async function deleteZone(zoneName) {
    if (!confirm(`Delete zone "${zoneName}"?`)) return;
    try {
        await fetch(`/api/zones/${zoneName}`, { method: 'DELETE' });
        fetchZones();
    } catch (e) { alert("Delete failed: " + e.message); }
}

// Legacy alias — some cards still call this
export async function recalibrateZone(zoneName) {
    if (!confirm(`Reset "${zoneName}" to UNCALIBRATED? You'll need to start a new calibration.`)) return;
    try {
        await fetch(`/api/zones/${zoneName}/recalibrate`, { method: 'POST' });
        fetchZones();
    } catch (e) { alert("Reset failed: " + e.message); }
}

// ============================================================================
// CREATE ZONE MODAL
// ============================================================================
function openCreateZoneModal() {
    document.getElementById('zone-name-input').value = '';
    selectedDevices.clear();
    updateSelectedCount();
    fetchDevicesForModal().then(renderDeviceList);
    new bootstrap.Modal(document.getElementById('createZoneModal')).show();
}

function renderDeviceList(devices) {
    const list = document.getElementById('zone-device-list');
    if (!list) return;
    list.innerHTML = '';
    devices.forEach(device => {
        const item = document.createElement('a');
        item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center';
        item.style.cursor = 'pointer';
        item.innerHTML = `
            <div>
                <strong>${device.friendly_name || device.ieee}</strong><br>
                <small class="text-muted">${device.model || ''} (${device.type || ''})</small>
            </div>
            <input class="form-check-input" type="checkbox"
                ${selectedDevices.has(device.ieee) ? 'checked' : ''}>`;
        item.onclick = (e) => {
            if (e.target.tagName !== 'INPUT') {
                e.preventDefault();
                const cb = item.querySelector('input');
                cb.checked = !cb.checked;
            }
            const checked = item.querySelector('input').checked;
            if (checked) { selectedDevices.add(device.ieee); item.classList.add('active'); }
            else { selectedDevices.delete(device.ieee); item.classList.remove('active'); }
            updateSelectedCount();
        };
        list.appendChild(item);
    });
}

function filterDeviceList(query) {
    const q = query.toLowerCase();
    const filtered = deviceListCache.filter(d =>
        (d.friendly_name && d.friendly_name.toLowerCase().includes(q))
        || d.ieee.toLowerCase().includes(q));
    renderDeviceList(filtered);
}

function updateSelectedCount() {
    const el = document.getElementById('zone-selected-count');
    if (el) el.innerText = selectedDevices.size;
}

async function handleCreateZoneSubmit() {
    const name = document.getElementById('zone-name-input').value.trim();
    if (!name) return alert("Please enter a zone name");
    if (selectedDevices.size < 1) return alert("Select at least 1 device");

    const payload = {
        name,
        device_ieees: Array.from(selectedDevices),
        deviation_threshold: parseFloat(document.getElementById('zone-deviation-threshold')?.value || 2.5),
        min_devices_triggered: parseFloat(document.getElementById('zone-min-links')?.value || 1.5),
        calibration_time: parseInt(document.getElementById('zone-calibration-time')?.value || 120),
        clear_delay: parseInt(document.getElementById('zone-clear-delay')?.value || 15),
    };

    try {
        const r = await fetch('/api/zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) throw new Error((await r.json()).detail || "Failed to create zone");
        bootstrap.Modal.getInstance(document.getElementById('createZoneModal'))?.hide();
        fetchZones();
    } catch (e) { alert("Error: " + e.message); }
}