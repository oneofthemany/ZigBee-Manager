// static/js/tabs.js
import { renderDeviceTable } from './devices.js';
import { state } from './state.js';

let deviceTabs = {};

export async function loadTabs() {
    const res = await fetch('/api/tabs');
    deviceTabs = await res.json();
    updateTabFilter();
}

function updateTabFilter() {
    const select = document.getElementById('tabFilter');
    if (!select) return;

    select.innerHTML = '<option value="">All Devices</option>';

    for (const tab in deviceTabs) {
        const opt = document.createElement('option');
        opt.value = tab;
        opt.textContent = `${tab} (${deviceTabs[tab].length})`;
        select.appendChild(opt);
    }
}

export function filterByTab() {
    const tab = document.getElementById('tabFilter').value;

    if (!tab) {
        state.deviceFilter = null;
    } else {
        state.deviceFilter = (device) => {
            return deviceTabs[tab].includes(device.ieee);
        };
    }

    renderDeviceTable();
}

export function openTabManager() {
    const modal = `
        <div class="modal fade" id="tabManagerModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5>Manage Device Tabs</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="mb-3">
                            <button class="btn btn-sm btn-success" onclick="createNewTab()">
                                <i class="fas fa-plus"></i> New Tab
                            </button>
                        </div>
                        <div id="tabsList"></div>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modal);
    renderTabsList();
    new bootstrap.Modal(document.getElementById('tabManagerModal')).show();
}

function renderTabsList() {
    const container = document.getElementById('tabsList');
    if (!container) return;

    container.innerHTML = '';

    for (const tab in deviceTabs) {
        const tabCard = `
            <div class="card mb-2">
                <div class="card-body">
                    <div class="d-flex justify-content-between align-items-center">
                        <h6>${tab} <span class="badge bg-secondary">${deviceTabs[tab].length}</span></h6>
                        <div>
                            <button class="btn btn-sm btn-primary" onclick="manageTabDevices('${tab}')">
                                <i class="fas fa-edit"></i> Devices
                            </button>
                            <button class="btn btn-sm btn-danger" onclick="deleteTab('${tab}')">
                                <i class="fas fa-trash"></i>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        container.insertAdjacentHTML('beforeend', tabCard);
    }
}

export async function createNewTab() {
    const name = prompt('Enter tab name:');
    if (!name) return;

    const res = await fetch('/api/tabs', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
    });

    await loadTabs();
    renderTabsList();
}

export async function deleteTab(tab) {
    if (!confirm(`Delete tab "${tab}"?`)) return;

    await fetch(`/api/tabs/${tab}`, {method: 'DELETE'});
    await loadTabs();
    renderTabsList();
}

export function manageTabDevices(tab) {
    const devices = Object.values(window.state.devices);
    const inTab = deviceTabs[tab];

    const modal = `
        <div class="modal fade" id="tabDevicesModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5>Devices in "${tab}"</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="list-group" id="deviceCheckList"></div>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modal);

    const list = document.getElementById('deviceCheckList');
    devices.forEach(dev => {
        const checked = inTab.includes(dev.ieee) ? 'checked' : '';
        list.innerHTML += `
            <label class="list-group-item">
                <input class="form-check-input me-1" type="checkbox" ${checked}
                    onchange="toggleDeviceInTab('${tab}', '${dev.ieee}', this.checked)">
                ${dev.friendly_name || dev.ieee}
            </label>
        `;
    });

    new bootstrap.Modal(document.getElementById('tabDevicesModal')).show();
}

export async function toggleDeviceInTab(tab, ieee, add) {
    if (add) {
        await fetch(`/api/tabs/${tab}/devices`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ieee})
        });
    } else {
        await fetch(`/api/tabs/${tab}/devices/${ieee}`, {method: 'DELETE'});
    }

    await loadTabs();
    renderTabsList();
}