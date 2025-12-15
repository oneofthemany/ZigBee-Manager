/**
 * Device List Management
 * Handles device table rendering, updates, and state management
 */

import { state } from './state.js';
import { getTypeIcon, getLqiBadge, timeAgo } from './utils.js';
import { refreshModalState } from './device-modal.js';
import { openDeviceModal } from './device-modal.js';
import { initTableSort, sortDevices, applySortState } from './table-sort.js';


/**
 * Fetch all devices from API and render table
 */
export async function fetchAllDevices() {
    try {
        console.log("Fetching all devices...");
        const res = await fetch('/api/devices');
        if (!res.ok) throw new Error(`API Error: ${res.status}`);
        const devices = await res.json();

        console.log(`Received ${devices.length} devices.`);
        state.devices = devices; // Update state
        renderDeviceTable();
        populateRouterList();

        // Initialise table sorting on first load
        if (!state.tableSortInitialised) {
            initTableSort(handleSort);
            state.tableSortInitialised = true;
        }

    } catch (e) {
        console.error("Failed to fetch devices:", e);
        const tbody = document.getElementById('deviceTableBody');
        if (tbody) tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger">Error loading devices: ${e.message}</td></tr>`;
    }
}

/**
 * Handle sort callback from table-sort module
 */
function handleSort(column, type, direction) {
    console.log(`Sort triggered: ${column} (${type}) ${direction}`);
    renderDeviceTable(); // Re-render with sorted data
}

export function renderDeviceTable() {
    const tbody = document.getElementById('deviceTableBody');
    const coordContainer = document.getElementById('coordinator-info');

    if (!tbody) return;

    tbody.innerHTML = '';

    // Find Coordinator
    const coordinator = state.devices.find(d => d.type === 'Coordinator');
    let otherDevices = state.devices.filter(d => d.type !== 'Coordinator');

    // Apply current sort state to devices
    otherDevices = applySortState(otherDevices);

    // 1. Render Coordinator Card
    if (coordinator && coordContainer) {
        coordContainer.innerHTML = `
            <div class="col-md-1 text-center">
                <i class="fas fa-broadcast-tower fa-2x text-primary"></i>
            </div>
            <div class="col-md-3">
                <h6 class="mb-0">Coordinator</h6>
                <small class="text-muted font-monospace">${coordinator.ieee}</small>
            </div>
            <div class="col-md-3">
                <span class="badge bg-light text-dark border">
                    <i class="fas fa-microchip"></i> ${coordinator.model || 'Unknown'}
                </span>
            </div>
            <div class="col-md-3">
                <span class="badge bg-light text-dark border">
                    <i class="fas fa-industry"></i> ${coordinator.manufacturer || 'Unknown'}
                </span>
            </div>
            <div class="col-md-2 text-end">
                <span class="badge bg-success">Online</span>
            </div>
        `;
    } else if (coordContainer) {
        coordContainer.innerHTML = `<div class="col-12 text-center text-muted small">Coordinator not found</div>`;
    }

    // 2. Render Other Devices
    if (otherDevices.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">No devices paired.</td></tr>';
        return;
    }

    otherDevices.forEach(d => {
        // Merge with existing cache if present to keep transient state
        if (state.deviceCache[d.ieee]) {
            d.state = { ...state.deviceCache[d.ieee].state, ...d.state };
        }
        state.deviceCache[d.ieee] = d;

        if (!d.last_seen_ts) d.last_seen_ts = Date.now();

        const tr = document.createElement('tr');

        // Quirk Badge Logic
        let quirkHtml = '';
        if (d.quirk && d.quirk !== 'None' && d.quirk !== 'NoneType') {
            const quirkName = d.quirk.split('.').pop();
            quirkHtml = `<span class="badge bg-info text-dark" style="font-size:0.65rem" title="${d.quirk}">${quirkName}</span>`;
        }

        // Status Badge Logic
        let statusHtml = d.available !== false
            ? '<span class="badge bg-success me-1">Online</span>'
            : '<span class="badge bg-secondary me-1">Offline</span>';

        tr.innerHTML = `
            <td class="text-center align-middle" style="font-size: 1.2rem;">${getTypeIcon(d.type)}</td>
            <td class="align-middle">
                <div class="fw-bold text-primary" style="cursor:pointer" onclick="window.renamePrompt('${d.ieee}', '${d.friendly_name}')">
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
                    <button class="btn btn-outline-primary manage-btn" title="Details & Control">
                        <i class="fas fa-sliders-h"></i> Manage
                    </button>
                </div>
            </td>
        `;

        // Attach event listener for Manage button correctly
        const manageBtn = tr.querySelector('.manage-btn');
        if (manageBtn) {
            manageBtn.addEventListener('click', () => openDeviceModal(d)); // <--- Pass the object 'd'
        }
        tbody.appendChild(tr);
    });
}

/**
 * Handle incoming WebSocket device update events
 */
export function handleDeviceUpdate(payload) {
    // UPDATED LOGGING: Log the payload as JSON
    console.log("1. WebSocket Update Received:", payload.ieee, "\nPayload:", JSON.stringify(payload, null, 2));

    // 1. Find the device in the array
    const devIndex = state.devices.findIndex(d => d.ieee === payload.ieee);

    if (devIndex !== -1) {
        // 2. Update the device state in memory
        // We merge the new data into the existing state object to preserve existing keys
        state.devices[devIndex].state = { ...state.devices[devIndex].state, ...payload.data };
        console.log("2. Current Open Device:", state.currentDeviceIeee);

        // Update metadata if present
        if (payload.data.last_seen) state.devices[devIndex].last_seen_ts = payload.data.last_seen;
        if (payload.data.available !== undefined) state.devices[devIndex].available = payload.data.available;
        if (payload.data.lqi !== undefined) state.devices[devIndex].lqi = payload.data.lqi;

        // Update the cache as well
        state.deviceCache[payload.ieee] = state.devices[devIndex];

        // 3. Update the background table row
        renderDeviceTable();

        // Update router list if device type changed or availability changed
        populateRouterList();

        console.log("3. MATCH! Attempting to refresh modal...");

        // 4. Refresh the modal if it is open for THIS device
        if (state.currentDeviceIeee === payload.ieee) {
            refreshModalState(state.devices[devIndex]); // Pass the updated object
        }
    } else {
        // Device not found in list (maybe new join?), trigger full fetch
        console.log("Device not found in local list, fetching all...");
        fetchDevices();
    }
}

/**
 * Populate the "Pair via specific device" dropdown list
 * Targets <div id="routerList">
 */
function populateRouterList() {
    const listContainer = document.getElementById('routerList');
    if (!listContainer) return;

    // Clear current list
    listContainer.innerHTML = '';

    if (!state.devices || state.devices.length === 0) {
        listContainer.innerHTML = '<span class="dropdown-item disabled">No devices available</span>';
        return;
    }

    // Filter for Routers and Coordinator
    // Case-insensitive check for role/type
    const routers = state.devices.filter(d => {
        const type = (d.type || '').toLowerCase();
        // Also check if it's the coordinator based on IEEE if type is missing
        return type.includes('router') || type.includes('coordinator');
    });

    if (routers.length === 0) {
        listContainer.innerHTML = '<span class="dropdown-item disabled">No routers found</span>';
        return;
    }

    // Sort routers by name
    routers.sort((a, b) => {
        const nameA = a.friendly_name || a.ieee;
        const nameB = b.friendly_name || b.ieee;
        return nameA.localeCompare(nameB);
    });

    // Create dropdown items
    routers.forEach(router => {
        const name = router.friendly_name || router.ieee;
        const model = router.model ? ` <small class="text-muted">(${router.model})</small>` : '';

        const item = document.createElement('a');
        item.className = 'dropdown-item d-flex justify-content-between align-items-center cursor-pointer';
        item.href = '#'; // Prevent default anchor behavior
        item.innerHTML = `<span>${name}${model}</span>`;

        // Add click handler
        item.onclick = (e) => {
            e.preventDefault();
            enablePermitJoinDevice(router.ieee, name);
        };

        listContainer.appendChild(item);
    });
}

/**
 * Enable permit join on specific device
 */
window.enablePermitJoinDevice = async function(ieee, name) {
    if (!confirm(`Enable pairing on ${name} for 120 seconds?`)) return;

    try {
        const response = await fetch('/api/permit_join', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                duration: 120,
                ieee: ieee
            })
        });

        const result = await response.json();

        if (result.success) {
            alert(`Pairing enabled on ${name}. LED on device may flash.`);
        } else {
            alert(`Failed: ${result.error}`);
        }

    } catch (error) {
        console.error("Permit join error:", error);
        alert("Failed to send request");
    }
};

export function removeDeviceRow(ieee) {
    state.devices = state.devices.filter(d => d.ieee !== ieee);
    renderDeviceTable();
}