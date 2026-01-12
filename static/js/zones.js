/**
 * zones.js
 * Frontend logic for Presence Detection Zones
 */

// ============================================================================
// STATE
// ============================================================================
let zonesData = new Map();
let deviceListCache = [];

// ============================================================================
// INITIALIZATION
// ============================================================================
export function initZones() {
    console.log("Initializing Zones Module...");

    // Initial Fetch
    fetchZones();

    // Bind Tab Click to Refresh
    const zonesTabBtn = document.querySelector('button[data-bs-target="#zones"]');
    if (zonesTabBtn) {
        zonesTabBtn.addEventListener('click', () => {
            fetchZones();
        });
    }

    // Bind Refresh Button
    document.getElementById('btn-refresh-zones')?.addEventListener('click', fetchZones);

    // Bind Create Zone Button
    document.getElementById('btn-create-zone')?.addEventListener('click', openCreateZoneModal);

    // Bind Save Zone Button
    document.getElementById('btn-save-zone')?.addEventListener('click', handleCreateZoneSubmit);

    // Search filter for create modal
    document.getElementById('zone-device-search')?.addEventListener('keyup', (e) => {
        filterDeviceList(e.target.value);
    });

    // --- LIVE UPDATES: Listen for WebSocket events ---
    window.addEventListener('zone-update', (event) => {
        const payload = event.detail;
        if (!payload || !payload.zone) return;

        const zoneName = payload.zone.name;
        // Merge updates into local cache
        const currentZone = zonesData.get(zoneName);
        if (currentZone) {
            // Update fields
            if (payload.zone.state) currentZone.state = payload.zone.state;
            if (payload.zone.occupied !== undefined) currentZone.occupied = payload.zone.occupied;

            // Merge links if provided (deep merge logic simplified here)
            if (payload.zone.links) {
                if (!currentZone.links) currentZone.links = {};
                // If the payload sends partial link updates, merge them.
                // Assuming payload.zone.links contains the updated link stats.
                Object.assign(currentZone.links, payload.zone.links);
            }

            // 1. Update Grid Card (if visible)
            updateZoneCardUI(currentZone);

            // 2. Update Modal (if open for this zone)
            const modalEl = document.getElementById('zoneDetailsModal');
            const modalTitle = document.getElementById('zone-details-title');

            if (modalEl.classList.contains('show') && modalTitle.innerText === zoneName) {
                // Re-render the active tab content
                renderZoneModalContent(currentZone, document.querySelector('#zoneDetailsModal .modal-body'), false);
            }
        }
    });
}

// Helper to update specific card UI element without full re-render
function updateZoneCardUI(zone) {
    // Find the card by iterating or ID (assuming ID format from renderZonesGrid)
    // NOTE: In renderZonesGrid we didn't set IDs, so we might need to rely on re-rendering or selecting by text title
    // Ideally, update renderZonesGrid to add id={`zone-card-${zone.name}`}
    // For now, simpler approach: trigger grid re-render if state changes heavily, or just rely on manual refresh for grid.
    // However, the user asked for modal updates specifically.
}


// ============================================================================
// API CALLS
// ============================================================================
async function fetchZones() {
    try {
        const response = await fetch('/api/zones');
        if (!response.ok) throw new Error("Failed to fetch zones");
        const zones = await response.json();

        zonesData.clear();
        zones.forEach(zone => zonesData.set(zone.name, zone));

        renderZonesGrid();
    } catch (error) {
        console.error("Error fetching zones:", error);
        document.getElementById('zones-container').innerHTML =
            `<div class="col-12 text-center text-danger">Failed to load zones: ${error.message}</div>`;
    }
}

async function fetchDevicesForModal() {
    try {
        const response = await fetch('/api/devices');
        if (!response.ok) throw new Error("Failed to fetch devices");
        deviceListCache = await response.json();
        return deviceListCache;
    } catch (error) {
        console.error("Error fetching devices:", error);
        return [];
    }
}

// ============================================================================
// RENDERING
// ============================================================================
function renderZonesGrid() {
    const container = document.getElementById('zones-container');
    container.innerHTML = '';

    if (zonesData.size === 0) {
        container.innerHTML = `
            <div class="col-12 text-center text-muted py-5">
                <i class="bi bi-inbox fs-1"></i>
                <p class="mt-2">No zones created yet.</p>
            </div>`;
        return;
    }

    zonesData.forEach(zone => {
        const card = createZoneCard(zone);
        container.appendChild(card);
    });
}

function createZoneCard(zone) {
    const col = document.createElement('div');
    col.className = 'col-md-6 col-lg-4 mb-4';

    const stateColors = {
        'occupied': 'success',
        'vacant': 'secondary',
        'calibrating': 'warning'
    };
    const stateColor = stateColors[zone.state] || 'primary';
    const isOccupied = zone.state === 'occupied';

    col.innerHTML = `
        <div class="card h-100 shadow-sm border-${isOccupied ? 'success' : 'light'}">
            <div class="card-header bg-transparent d-flex justify-content-between align-items-center">
                <h5 class="mb-0 text-truncate" title="${zone.name}">${zone.name}</h5>
                <span class="badge bg-${stateColor} text-uppercase">${zone.state}</span>
            </div>
            <div class="card-body">
                <div class="d-flex justify-content-between mb-2">
                    <span class="text-muted">Devices:</span>
                    <strong>${zone.device_count}</strong>
                </div>
                <div class="d-flex justify-content-between mb-3">
                    <span class="text-muted">Links Tracked:</span>
                    <strong>${zone.link_count}</strong>
                </div>

                ${zone.occupied_since ?
                    `<div class="alert alert-success py-1 small mb-3">
                        <i class="bi bi-clock-history"></i> Since ${new Date(zone.occupied_since * 1000).toLocaleTimeString()}
                    </div>` : ''
                }
            </div>
            <div class="card-footer bg-transparent border-top-0 d-flex gap-2">
                <button class="btn btn-sm btn-outline-primary flex-grow-1" onclick="window.viewZoneDetails('${zone.name}')">
                    <i class="bi bi-graph-up"></i> Details & Devices
                </button>
                <button class="btn btn-sm btn-outline-warning" onclick="window.recalibrateZone('${zone.name}')" title="Recalibrate">
                    <i class="bi bi-arrow-clockwise"></i>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="window.deleteZone('${zone.name}')" title="Delete">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
        </div>
    `;
    return col;
}

// ============================================================================
// VIEW ZONE DETAILS (Updated with Tabs and Live Logic)
// ============================================================================
export async function viewZoneDetails(zoneName) {
    const zone = zonesData.get(zoneName);
    if (!zone) return;

    // Refresh device list to resolve friendly names
    await fetchDevicesForModal();

    const modalTitle = document.getElementById('zone-details-title');
    const modalBody = document.querySelector('#zoneDetailsModal .modal-body');

    modalTitle.innerText = zone.name;

    // Initial Render
    renderZoneModalContent(zone, modalBody, true); // true = full render including tabs structure

    const modal = new bootstrap.Modal(document.getElementById('zoneDetailsModal'));
    modal.show();
}

/**
 * Renders the content inside the modal.
 * @param {Object} zone - The zone data
 * @param {HTMLElement} container - The container to render into
 * @param {boolean} fullRender - If true, re-creates the tabs structure. If false, only updates inner content.
 */
function renderZoneModalContent(zone, container, fullRender = true) {
    // Generate inner HTML for stats
    let linksHtml = '';
    if (zone.links && Object.keys(zone.links).length > 0) {
        linksHtml = `<div class="table-responsive"><table class="table table-sm table-striped small align-middle">
            <thead>
                <tr>
                    <th>Link Pair</th>
                    <th class="text-center">Signal</th>
                    <th class="text-center">Baseline</th>
                    <th class="text-center">Dev (σ)</th>
                    <th class="text-center">Samples</th>
                    <th class="text-center">Range</th>
                </tr>
            </thead>
            <tbody>
                ${Object.entries(zone.links).map(([key, link]) => {
                    // Check if link is an object or simplified from WS
                    const rssi = link.last_rssi !== undefined ? link.last_rssi : (link.rssi || '-');
                    const dev = (link.deviation !== undefined && link.deviation !== null) ? Number(link.deviation).toFixed(2) : '-';
                    const baseline = (link.baseline_mean !== undefined && link.baseline_mean !== null) ? Number(link.baseline_mean).toFixed(1) : '-';
                    const samples = link.sample_count || '-';

                    // Format Min/Max range
                    const minR = link.min_rssi !== undefined ? link.min_rssi : '';
                    const maxR = link.max_rssi !== undefined ? link.max_rssi : '';
                    const range = (minR !== '' && maxR !== '') ? `${minR}..${maxR}` : '-';

                    // Trigger highlighting
                    const isTriggered = parseFloat(dev) > (zone.config?.deviation_threshold || 2.5);
                    const rowClass = isTriggered ? 'table-danger fw-bold' : '';
                    const devClass = isTriggered ? 'text-danger' : '';

                    return `
                        <tr class="${rowClass}">
                            <td class="text-truncate" style="max-width: 150px; font-family:monospace; font-size:0.85em;" title="${key}">${key}</td>
                            <td class="text-center">${rssi}</td>
                            <td class="text-center">${baseline}</td>
                            <td class="text-center ${devClass}">${dev}</td>
                            <td class="text-center text-muted">${samples}</td>
                            <td class="text-center text-muted small">${range}</td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table></div>`;
    } else {
        linksHtml = '<div class="alert alert-secondary">No link data available. Wait for calibration.</div>';
    }

    // Generate inner HTML for devices
    const deviceList = zone.device_ieees || [];
    const devicesListHtml = deviceList.map(ieee => {
        const device = deviceListCache.find(d => d.ieee.toLowerCase() === ieee.toLowerCase()) || { friendly_name: ieee, model: 'Unknown' };
        const displayName = device.friendly_name === ieee ? ieee : `${device.friendly_name} (${ieee})`;

        return `
            <li class="list-group-item d-flex justify-content-between align-items-center">
                <div>
                    <strong>${displayName}</strong><br>
                    <small class="text-muted">${device.model}</small>
                </div>
                <button class="btn btn-sm btn-outline-danger" onclick="window.removeDeviceFromZone('${zone.name}', '${ieee}')" title="Remove Device">
                    <i class="fas fa-trash"></i>
                </button>
            </li>
        `;
    }).join('');

    const availableDevices = deviceListCache.filter(d => !deviceList.includes(d.ieee.toLowerCase()));
    const addOptions = availableDevices.map(d =>
        `<option value="${d.ieee}">${d.friendly_name || d.ieee}</option>`
    ).join('');

    const devicesTabContent = `
        <div class="card mb-3">
            <div class="card-header bg-light small fw-bold">Add Device</div>
            <div class="card-body p-2">
                <div class="input-group">
                    <select class="form-select form-select-sm" id="zone-add-select">
                        <option value="">Select device...</option>
                        ${addOptions}
                    </select>
                    <button class="btn btn-sm btn-success" onclick="window.addDeviceToZoneFromModal('${zone.name}')">Add</button>
                </div>
            </div>
        </div>

        <h6 class="small text-muted mb-2">Devices in Zone (${deviceList.length})</h6>
        <ul class="list-group list-group-flush border rounded overflow-auto" style="max-height: 300px;">
            ${devicesListHtml}
        </ul>
    `;

    // HEADER content
    const headerHtml = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
             <span><strong>Status:</strong> <span class="badge bg-${zone.state === 'occupied' ? 'success' : 'secondary'}">${zone.state}</span></span>
             ${zone.occupied_since ? `<span class="badge bg-light text-dark border">Since: ${new Date(zone.occupied_since * 1000).toLocaleTimeString()}</span>` : ''}
        </div>
    `;

    if (fullRender) {
        // FULL RENDER: Create structure + content
        container.innerHTML = `
            ${headerHtml}
            <ul class="nav nav-tabs mb-3" id="zoneDetailsTabs" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link active" id="stats-tab" data-bs-toggle="tab" data-bs-target="#tab-stats" type="button">Link Statistics</button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="devices-tab" data-bs-toggle="tab" data-bs-target="#tab-devices" type="button">Managed Devices</button>
                </li>
            </ul>

            <div class="tab-content">
                <div class="tab-pane fade show active" id="tab-stats">
                    ${linksHtml}
                </div>
                <div class="tab-pane fade" id="tab-devices">
                    ${devicesTabContent}
                </div>
            </div>
        `;
    } else {
        // PARTIAL RENDER: Just update the specific divs to avoid killing tab state
        // 1. Update Header
        const headerDiv = container.querySelector('.d-flex.justify-content-between');
        if (headerDiv) headerDiv.outerHTML = headerHtml;

        // 2. Update Stats Pane
        const statsPane = document.getElementById('tab-stats');
        if (statsPane) statsPane.innerHTML = linksHtml;

        // 3. Update Devices Pane (less frequent but safe to update)
        const devicesPane = document.getElementById('tab-devices');
        if (devicesPane) devicesPane.innerHTML = devicesTabContent;
    }
}

// ============================================================================
// ACTIONS (Exposed to Window)
// ============================================================================

export async function addDeviceToZoneFromModal(zoneName) {
    const select = document.getElementById('zone-add-select');
    const ieee = select.value;
    if (!ieee) return;

    try {
        const response = await fetch(`/api/zones/${zoneName}/devices`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ add: [ieee], remove: [] })
        });

        if (!response.ok) throw new Error("Failed to add device");

        // Refresh zones data and re-render modal
        await fetchZones();
        viewZoneDetails(zoneName); // Re-open modal to refresh list

    } catch (e) {
        alert("Error adding device: " + e.message);
    }
}

export async function removeDeviceFromZone(zoneName, ieee) {
    if (!confirm(`Remove device ${ieee} from zone? This will trigger recalibration.`)) return;

    try {
        const response = await fetch(`/api/zones/${zoneName}/devices`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ add: [], remove: [ieee] })
        });

        if (!response.ok) throw new Error("Failed to remove device");

        // Refresh zones data and re-render modal
        await fetchZones();
        viewZoneDetails(zoneName); // Re-open modal to refresh list

    } catch (e) {
        alert("Error removing device: " + e.message);
    }
}

export async function recalibrateZone(zoneName) {
    if (!confirm(`Force recalibration for ${zoneName}?`)) return;
    try {
        await fetch(`/api/zones/${zoneName}/recalibrate`, { method: 'POST' });
        fetchZones();
    } catch (e) {
        alert("Recalibration failed: " + e.message);
    }
}

export async function deleteZone(zoneName) {
    if (!confirm(`Are you sure you want to delete zone "${zoneName}"?`)) return;
    try {
        await fetch(`/api/zones/${zoneName}`, { method: 'DELETE' });
        fetchZones();
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

// ============================================================================
// CREATE ZONE LOGIC
// ============================================================================

function openCreateZoneModal() {
    document.getElementById('zone-name-input').value = '';
    selectedDevices.clear();
    updateSelectedCount();
    fetchDevicesForModal().then(devices => renderDeviceList(devices)); // Load and render

    const modal = new bootstrap.Modal(document.getElementById('createZoneModal'));
    modal.show();
}

const selectedDevices = new Set();

function renderDeviceList(devices) {
    const list = document.getElementById('zone-device-list');
    list.innerHTML = '';

    devices.forEach(device => {
        const item = document.createElement('a');
        item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center';
        item.style.cursor = 'pointer';
        item.innerHTML = `
            <div>
                <strong>${device.friendly_name || device.ieee}</strong><br>
                <small class="text-muted">${device.model} (${device.type})</small>
            </div>
            <input class="form-check-input" type="checkbox" ${selectedDevices.has(device.ieee) ? 'checked' : ''}>
        `;

        item.onclick = (e) => {
            if (e.target.tagName !== 'INPUT') {
                e.preventDefault();
                const checkbox = item.querySelector('input');
                checkbox.checked = !checkbox.checked;
            }
            const isChecked = item.querySelector('input').checked;

            if (isChecked) {
                selectedDevices.add(device.ieee);
                item.classList.add('active');
            } else {
                selectedDevices.delete(device.ieee);
                item.classList.remove('active');
            }
            updateSelectedCount();
        };
        list.appendChild(item);
    });
}

function filterDeviceList(query) {
    const lowerQuery = query.toLowerCase();
    const filtered = deviceListCache.filter(d =>
        (d.friendly_name && d.friendly_name.toLowerCase().includes(lowerQuery)) ||
        d.ieee.toLowerCase().includes(lowerQuery)
    );
    renderDeviceList(filtered);
}

function updateSelectedCount() {
    document.getElementById('zone-selected-count').innerText = selectedDevices.size;
}

async function handleCreateZoneSubmit() {
    const name = document.getElementById('zone-name-input').value.trim();
    if (!name) return alert("Please enter a zone name");
    if (selectedDevices.size < 2) return alert("Select at least 2 devices");

    const payload = {
        name: name,
        device_ieees: Array.from(selectedDevices),
        deviation_threshold: parseFloat(document.getElementById('zone-deviation-threshold').value),
        variance_threshold: parseFloat(document.getElementById('zone-variance-threshold').value),
        min_links_triggered: parseInt(document.getElementById('zone-min-links').value),
        calibration_time: parseInt(document.getElementById('zone-calibration-time').value),
        clear_delay: parseInt(document.getElementById('zone-clear-delay').value)
    };

    try {
        const response = await fetch('/api/zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) throw new Error((await response.json()).detail || "Failed to create zone");

        // Close modal and refresh
        const modalEl = document.getElementById('createZoneModal');
        const modal = bootstrap.Modal.getInstance(modalEl);
        modal.hide();

        fetchZones();
    } catch (e) {
        alert("Error: " + e.message);
    }
}