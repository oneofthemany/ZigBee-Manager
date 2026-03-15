/**
 * Groups Management Module
 * Handles Zigbee group creation, management, and control
 */

import { state } from './state.js';

// Groups state
const groupsState = {
    allGroups: [],
    selectedBaseDevice: null,
    compatibleDevices: [],
    selectedDevices: new Set(),
    currentGroup: null
};

/**
 * Initialize groups tab
 */
export async function initGroups() {
    console.log("Initialising groups management...");

    // Load existing groups
    await loadGroups();

    // Populate base device dropdown when tab is shown
    const groupsTab = document.querySelector('button[data-bs-target="#groups"]');
    if (groupsTab) {
        groupsTab.addEventListener('shown.bs.tab', () => {
            populateBaseDeviceDropdown();
        });
    }

    // Also populate immediately if devices already loaded
    if (state.devices && state.devices.length > 0) {
        populateBaseDeviceDropdown();
    }

    // Attach Create Button Listener
    const createBtn = document.getElementById('createGroupBtn');
    if (createBtn) {
        createBtn.onclick = createGroup;
    }

    console.log("Groups management initialised");
}

/**
 * Load all groups from API
 */
async function loadGroups() {
    try {
        const response = await fetch('/api/groups');
        const groups = await response.json();

        groupsState.allGroups = groups;
        renderGroupsList(groups);

        // Update count badge
        const countBadge = document.getElementById('groupCount');
        if (countBadge) {
            countBadge.textContent = groups.length;
        }

        console.log(`Loaded ${groups.length} groups`);

    } catch (error) {
        console.error("Failed to load groups:", error);
    }
}

/**
 * Populate base device dropdown
 */
function populateBaseDeviceDropdown() {
    const select = document.getElementById('baseDevice');
    if (!select) return;

    select.innerHTML = '<option value="">-- Choose a device --</option>';

    // Use imported state from state.js
    if (!state.devices || state.devices.length === 0) {
        console.log("No devices available for grouping yet");
        return;
    }

    const devices = state.devices.filter(d => d.type !== 'Coordinator');

    console.log(`Populating dropdown with ${devices.length} devices`);

    devices.forEach(device => {
        const option = document.createElement('option');
        option.value = device.ieee;
        option.textContent = `${device.friendly_name} (${device.manufacturer || '?'} ${device.model || '?'})`;
        select.appendChild(option);
    });
}

/**
 * Handle base device selection
 */
window.onBaseDeviceSelected = async function() {
    const select = document.getElementById('baseDevice');
    const ieee = select.value;

    if (!ieee) {
        hideBaseDeviceInfo();
        return;
    }

    // Find device in imported state
    const device = state.devices.find(d => d.ieee === ieee);
    if (!device) {
        console.error(`Device ${ieee} not found in state`);
        return;
    }

    groupsState.selectedBaseDevice = device;
    groupsState.selectedDevices.clear();
    groupsState.selectedDevices.add(ieee); // Add base device

    // Show device info
    showBaseDeviceInfo(device);

    // Load compatible devices
    await loadCompatibleDevices(ieee);
}

/**
 * Show base device information
 */
function showBaseDeviceInfo(device) {
    const infoDiv = document.getElementById('baseDeviceInfo');
    if (!infoDiv) return;

    infoDiv.classList.remove('d-none');

    const nameSpan = document.getElementById('baseDeviceName');
    if (nameSpan) nameSpan.textContent = device.friendly_name;

    // Determine device type from capabilities
    let deviceType = device.type || 'Unknown';
    const model = (device.model || '').toLowerCase();

    if (deviceType === 'Unknown') {
        if (model.includes('light') || model.includes('bulb') || model.includes('lamp') || model.includes('spot')) deviceType = 'Light';
        else if (model.includes('switch') || model.includes('plug') || model.includes('socket')) deviceType = 'Switch';
        else if (model.includes('cover') || model.includes('blind') || model.includes('curtain')) deviceType = 'Cover';
    }

    const typeSpan = document.getElementById('baseDeviceType');
    if (typeSpan) typeSpan.textContent = deviceType;

    // Show capabilities (mock for now - will come from backend)
    const capabilitiesDiv = document.getElementById('baseDeviceCapabilities');
    if (capabilitiesDiv) {
        // Just show hints based on type for now until we fetch real ones
        let capsHtml = '';
        if (deviceType === 'Light' || deviceType === 'Switch') {
            capsHtml += `<span class="capability-badge bg-success text-white"><i class="fas fa-power-off"></i> On/Off</span>`;
        }
        if (deviceType === 'Light' && (model.includes('dimmable') || model.includes('bulb'))) {
            capsHtml += `<span class="capability-badge bg-info text-white"><i class="fas fa-sun"></i> Brightness</span>`;
        }

        capabilitiesDiv.innerHTML = capsHtml || '<span class="text-muted">Loading...</span>';
    }
}

/**
 * Hide base device info
 */
function hideBaseDeviceInfo() {
    const sections = [
        'baseDeviceInfo',
        'compatibleDevicesSection',
        'commonCapabilitiesSection'
    ];

    sections.forEach(id => {
        const element = document.getElementById(id);
        if (element) element.classList.add('d-none');
    });

    const createBtn = document.getElementById('createGroupBtn');
    if (createBtn) createBtn.disabled = true;
}

/**
 * Load compatible devices
 */
async function loadCompatibleDevices(ieee) {
    try {
        const response = await fetch(`/api/devices/${ieee}/compatible`);
        const compatible = await response.json();

        groupsState.compatibleDevices = compatible;
        renderCompatibleDevices(compatible);

        // Show section
        const section = document.getElementById('compatibleDevicesSection');
        if (section) section.classList.remove('d-none');

    } catch (error) {
        console.error("Failed to load compatible devices:", error);
    }
}

/**
 * Render compatible devices list
 */
function renderCompatibleDevices(devices) {
    const container = document.getElementById('compatibleDevicesList');
    if (!container) return;

    if (devices.length === 0) {
        container.innerHTML = '<p class="text-muted text-center p-3">No compatible devices found</p>';
        return;
    }

    // Smart sort: devices with similar names to base device come first
    const baseDevice = groupsState.selectedBaseDevice;
    if (baseDevice && baseDevice.friendly_name) {
        const baseName = baseDevice.friendly_name.toLowerCase();
        const baseWords = baseName.split(/[\s_-]+/).filter(w => w.length > 2);

        devices.sort((a, b) => {
            const aName = (a.name || a.ieee).toLowerCase();
            const bName = (b.name || b.ieee).toLowerCase();

            // Calculate similarity scores
            let aScore = 0;
            let bScore = 0;

            baseWords.forEach(word => {
                if (aName.includes(word)) aScore++;
                if (bName.includes(word)) bScore++;
            });

            // Sort by score descending, then alphabetically
            if (bScore !== aScore) {
                return bScore - aScore;
            }
            return aName.localeCompare(bName);
        });
    }

    container.innerHTML = '';
    console.log(`Rendering ${devices.length} compatible devices`);

    devices.forEach(device => {
        const safeId = device.ieee.replace(/:/g, '_');

        const item = document.createElement('div');
        item.className = 'device-checkbox-item';
        item.innerHTML = `
            <div class="form-check">
                <input class="form-check-input" type="checkbox"
                       id="dev_${safeId}"
                       value="${device.ieee}"
                       onchange="onDeviceCheckChanged('${device.ieee}')">
                <label class="form-check-label" for="dev_${safeId}">
                    <strong>${device.name || device.ieee}</strong>
                    <div class="small text-muted">${device.type || 'Unknown type'}</div>
                    ${renderCapabilityBadges(device.capabilities || [])}
                </label>
            </div>
        `;
        container.appendChild(item);
    });
}

/**
 * Render capability badges
 */
function renderCapabilityBadges(capabilities) {
    const badges = {
        'on_off': '<span class="capability-badge bg-success text-white"><i class="fas fa-power-off"></i> On/Off</span>',
        'brightness': '<span class="capability-badge bg-info text-white"><i class="fas fa-sun"></i> Brightness</span>',
        'color_temp': '<span class="capability-badge bg-warning text-dark"><i class="fas fa-thermometer-half"></i> Color Temp</span>',
        'color_xy': '<span class="capability-badge bg-primary text-white"><i class="fas fa-palette"></i> Color</span>',
        'position': '<span class="capability-badge bg-secondary text-white"><i class="fas fa-arrows-alt-v"></i> Position</span>',
    };

    return capabilities.map(cap => badges[cap] || '').join(' ');
}

/**
 * Handle device checkbox change
 */
window.onDeviceCheckChanged = function(ieee) {
    // Replace colons for valid HTML ID lookup
    const safeId = ieee.replace(/:/g, '_');
    const checkbox = document.getElementById(`dev_${safeId}`);

    if (!checkbox) {
        console.error(`Checkbox not found for IEEE: ${ieee} (ID: dev_${safeId})`);
        return;
    }

    if (checkbox.checked) {
        groupsState.selectedDevices.add(ieee);
        console.log(`Added device ${ieee} to selection`);
    } else {
        groupsState.selectedDevices.delete(ieee);
        console.log(`Removed device ${ieee} from selection`);
    }

    // Update button state and common capabilities
    updateGroupCreationState();
}

/**
 * Update group creation state
 */
function updateGroupCreationState() {
    const selectedCount = groupsState.selectedDevices.size;
    const btn = document.getElementById('createGroupBtn');

    if (!btn) return;

    // Need at least 2 devices
    if (selectedCount >= 2) {
        btn.disabled = false;
        showCommonCapabilities();
    } else {
        btn.disabled = true;
        const section = document.getElementById('commonCapabilitiesSection');
        if (section) section.classList.add('d-none');
    }
}

/**
 * Show common capabilities
 */
function showCommonCapabilities() {
    // Get capabilities for all selected devices
    const selectedIEEEs = Array.from(groupsState.selectedDevices);

    // For now, mock the common capabilities
    // In production, this would calculate intersection of all device capabilities
    const section = document.getElementById('commonCapabilitiesSection');
    const info = document.getElementById('commonCapabilitiesInfo');

    if (!section || !info) return;

    info.innerHTML = `
        <div class="mb-2">
            <strong>✅ On/Off Control:</strong> Turn all devices on/off together
        </div>
        <div class="mb-2">
            <strong>✅ Brightness Control:</strong> Adjust brightness for all devices
        </div>
        <div class="text-muted small">
            Selected ${selectedIEEEs.length} devices
        </div>
    `;

    section.classList.remove('d-none');
}

/**
 * Create group
 */
window.createGroup = async function() {
    const name = document.getElementById('groupName').value.trim();

    if (!name) {
        alert('Please enter a group name');
        return;
    }

    if (groupsState.selectedDevices.size < 2) {
        alert('Please select at least 2 devices');
        return;
    }

    const devices = Array.from(groupsState.selectedDevices);

    // Debug logging
    console.log(`Creating group "${name}" with devices:`, devices);

    try {
        const response = await fetch('/api/groups/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, devices })
        });

        const result = await response.json();

        if (result.error) {
            alert(`Error: ${result.error}`);
            return;
        }

        if (result.success) {
            alert(`Group "${name}" created successfully!`);

            // Reset form
            resetGroupCreation();

            // Reload groups
            await loadGroups();
        }

    } catch (error) {
        console.error("Failed to create group:", error);
        alert('Failed to create group. Check console for details.');
    }
}

/**
 * Reset group creation form
 */
window.resetGroupCreation = function() {
    const nameInput = document.getElementById('groupName');
    const baseSelect = document.getElementById('baseDevice');

    if (nameInput) nameInput.value = '';
    if (baseSelect) baseSelect.value = '';

    groupsState.selectedBaseDevice = null;
    groupsState.selectedDevices.clear();
    hideBaseDeviceInfo();
}

/**
 * Render groups list
 */
function renderGroupsList(groups) {
    const container = document.getElementById('groupsList');
    if (!container) return;

    if (groups.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted p-4">
                No groups created yet. Create a group above to get started.
            </div>
        `;
        return;
    }

    container.innerHTML = '';

    groups.forEach(group => {
        const safeName = group.name.replace(/'/g, "\\'");

        const card = document.createElement('div');
        card.className = 'group-card card mb-2';
        card.innerHTML = `
            <div class="card-body">
                <div class="row align-items-center">
                    <div class="col-md-6">
                        <h6 class="mb-1">
                            <i class="fas fa-layer-group text-primary"></i> ${group.name}
                        </h6>
                        <small class="text-muted">
                            ${group.type || 'Unknown'} Group • ${group.members.length} devices
                        </small>
                    </div>
                    <div class="col-md-3">
                        ${renderCapabilityBadges(group.capabilities || [])}
                    </div>
                    <div class="col-md-3 text-end">
                        <button class="btn btn-sm btn-primary" onclick="openGroupControl(${group.id})">
                            <i class="fas fa-sliders-h"></i> Control
                        </button>
                        <button class="btn btn-sm btn-outline-danger ms-1" onclick="deleteGroup(${group.id}, '${safeName}')" title="Delete Group">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
                <div class="mt-2">
                    ${renderGroupMembers(group.devices || [])}
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

/**
 * Render group members
 */
function renderGroupMembers(devices) {
    return devices.map(dev => `
        <span class="member-device">
            <i class="fas fa-lightbulb"></i> ${dev.name}
        </span>
    `).join('');
}

/**
 * Quick Delete Group (No Modal)
 */
window.deleteGroup = async function(groupId, groupName) {
    if (!confirm(`Are you sure you want to delete group "${groupName}"?\n\nThis will remove it from Home Assistant and Zigbee devices.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/groups/${groupId}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            await loadGroups();
            // Show toast or alert? Alert for now as per snippet pattern
            // alert(`Group "${groupName}" deleted`);
        } else {
            const data = await response.json();
            alert(data.error || "Failed to delete group");
        }
    } catch (error) {
        console.error("Failed to delete group:", error);
        alert('Error deleting group');
    }
}


/**
 * Open group control modal
 */
window.openGroupControl = function(groupId) {
    const group = groupsState.allGroups.find(g => g.id === groupId);
    if (!group) {
        console.error(`Group ${groupId} not found`);
        return;
    }

    groupsState.currentGroup = group;

    // Set title
    const titleElement = document.getElementById('groupControlTitle');
    if (titleElement) titleElement.textContent = group.name;

    // Show group info
    const infoDiv = document.getElementById('groupInfoDisplay');
    if (infoDiv) {
        infoDiv.innerHTML = `
            <div class="mb-2"><strong>Type:</strong> ${group.type}</div>
            <div class="mb-2"><strong>Devices:</strong> ${group.members.length}</div>
            <div><strong>Capabilities:</strong> ${renderCapabilityBadges(group.capabilities || [])}</div>
        `;
    }

    // Render control panel
    renderGroupControls(group);

    // Render members
    renderGroupMembersModal(group);

    // Show modal
    const modalElement = document.getElementById('groupControlModal');
    if (modalElement) {
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }
}

/**
 * Render group controls (Comprehensive)
 */
function renderGroupControls(group) {
    const panel = document.getElementById('groupControlPanel');
    if (!panel) return;

    let html = '';
    const caps = group.capabilities || [];

    // --- LIGHTS: On/Off ---
    if (caps.includes('on_off')) {
        html += `
            <div class="card mb-3">
                <div class="card-body p-2">
                    <label class="form-label small fw-bold">Power</label>
                    <div class="btn-group w-100">
                        <button class="btn btn-outline-success" onclick="controlGroup(${group.id}, {state: 'ON'})">ON</button>
                        <button class="btn btn-outline-danger" onclick="controlGroup(${group.id}, {state: 'OFF'})">OFF</button>
                    </div>
                </div>
            </div>
        `;
    }

    // --- LIGHTS: Brightness ---
    if (caps.includes('brightness')) {
        html += `
            <div class="card mb-3">
                <div class="card-body p-2">
                    <label class="form-label small fw-bold">Brightness</label>
                    <input type="range" class="form-range" min="0" max="254" value="127"
                           onchange="controlGroup(${group.id}, {brightness: this.value})">
                </div>
            </div>
        `;
    }

    // --- LIGHTS: Color Mode (combined Temp + Color) ---
    if (caps.includes('color_temp') || caps.includes('color_xy')) {
        const hasBothModes = caps.includes('color_temp') && caps.includes('color_xy');

        html += `
            <div class="card mb-3">
                <div class="card-body p-2">`;

        // Mode toggle only if BOTH modes supported
        if (hasBothModes) {
            html += `
                    <label class="form-label small fw-bold">Color Mode</label>
                    <div class="btn-group w-100 mb-2" role="group">
                        <input type="radio" class="btn-check" name="groupColorMode_${group.id}" id="groupColorModeTemp_${group.id}"
                               checked onchange="showGroupColorMode(${group.id}, 'temp')">
                        <label class="btn btn-outline-secondary btn-sm" for="groupColorModeTemp_${group.id}">Temperature</label>
                        <input type="radio" class="btn-check" name="groupColorMode_${group.id}" id="groupColorModeColor_${group.id}"
                               onchange="showGroupColorMode(${group.id}, 'color')">
                        <label class="btn btn-outline-secondary btn-sm" for="groupColorModeColor_${group.id}">Color</label>
                    </div>`;
        }

        // Color Temperature panel
        if (caps.includes('color_temp')) {
            html += `
                    <div id="groupColorTempPanel_${group.id}">
                        <label class="form-label small text-muted">Color Temperature</label>
                        <div class="d-flex justify-content-between small text-muted">
                            <span>Cool</span><span>Warm</span>
                        </div>
                        <input type="range" class="form-range" min="153" max="500" value="250"
                               style="background: linear-gradient(to right, #99ccff, #fff, #ffae00);"
                               onchange="controlGroup(${group.id}, {color_temp: this.value})">
                    </div>`;
        }

        // Color Picker panel (hidden by default when both modes exist)
        if (caps.includes('color_xy')) {
            const hideColor = hasBothModes ? 'style="display:none"' : '';
            html += `
                    <div id="groupColorPickerPanel_${group.id}" ${hideColor}>
                        <label class="form-label small text-muted">Color</label>
                        <div class="d-flex gap-2 align-items-center">
                            <input type="color" class="form-control form-control-color" id="groupColorPicker_${group.id}"
                                   value="#ff6b6b"
                                   onchange="sendGroupColor(${group.id}, this.value)">
                            <div class="flex-grow-1">
                                <label class="form-label small text-muted mb-0">Saturation</label>
                                <input type="range" class="form-range" min="0" max="100" value="100" id="groupSatSlider_${group.id}"
                                       onchange="sendGroupColor(${group.id}, null, this.value)">
                            </div>
                        </div>
                    </div>`;
        }

        html += `
                </div>
            </div>
        `;
    }

    // --- COVERS: Position & Buttons ---
    if (caps.includes('position')) {
        html += `
            <div class="card mb-3">
                <div class="card-body p-2">
                    <label class="form-label small fw-bold">Cover Control</label>
                    <div class="btn-group w-100 mb-2">
                        <button class="btn btn-outline-primary" onclick="controlGroup(${group.id}, {cover_state: 'OPEN'})"><i class="fas fa-arrow-up"></i> Open</button>
                        <button class="btn btn-outline-secondary" onclick="controlGroup(${group.id}, {cover_state: 'STOP'})"><i class="fas fa-stop"></i> Stop</button>
                        <button class="btn btn-outline-primary" onclick="controlGroup(${group.id}, {cover_state: 'CLOSE'})"><i class="fas fa-arrow-down"></i> Close</button>
                    </div>
                    <label class="form-label small">Position</label>
                    <input type="range" class="form-range" min="0" max="100" value="50"
                           onchange="controlGroup(${group.id}, {position: this.value})">
                </div>
            </div>
        `;
    }

    if (!html) html = '<p class="text-muted">No controls available for this device type.</p>';
    panel.innerHTML = html;
}

/**
 * Control group
 */
window.controlGroup = async function(groupId, command) {
    try {
        console.log(`🎮 Sending group ${groupId} command:`, command);

        const response = await fetch(`/api/groups/${groupId}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(command)
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const result = await response.json();

        if (result.error) {
            console.error(`❌ Group control error:`, result.error);
            alert(`Error: ${result.error}`);
            return;
        }

        console.log(`✅ Group ${groupId} controlled:`, result);

        // Show success feedback
        if (result.results) {
            const successCount = result.results.filter(r => r.success).length;
            const totalCount = result.results.length;

            if (successCount === totalCount) {
                console.log(`✅ All ${totalCount} devices controlled successfully`);
            } else {
                console.warn(`⚠️ ${successCount}/${totalCount} devices controlled`);

                // Show which devices failed with detailed errors
                const failed = result.results.filter(r => r.error);
                if (failed.length > 0) {
                    console.error('❌ Failed devices:', failed);
                    failed.forEach(f => {
                        console.error(`  - Device ${f.ieee}: ${f.error}`);
                    });
                }

                // Only show alert if ALL devices failed
                if (successCount === 0) {
                    alert(`All devices failed to respond. Check console for details.`);
                }
            }
        }

        // Refresh device states after a short delay (only if some succeeded)
        if (result.results && result.results.some(r => r.success)) {
            setTimeout(() => {
                if (window.fetchAllDevices) {
                    window.fetchAllDevices();
                }
            }, 500);
        }

    } catch (error) {
        console.error("❌ Failed to control group:", error);
        alert(`Failed to control group: ${error.message}`);
    }
}

/**
 * Render group members in modal
 */
function renderGroupMembersModal(group) {
    const container = document.getElementById('groupMembersList');
    if (!container) return;

    container.innerHTML = group.devices.map(dev => `
        <div class="d-flex justify-content-between align-items-center mb-2 p-2 border rounded">
            <div>
                <strong>${dev.name}</strong>
                <br><small class="text-muted">${dev.model}</small>
            </div>
            <button class="btn btn-sm btn-outline-danger"
                    onclick="removeDeviceFromGroup(${group.id}, '${dev.ieee}')">
                <i class="fas fa-times"></i> Remove
            </button>
        </div>
    `).join('');
}

/**
 * Remove device from group
 */
window.removeDeviceFromGroup = async function(groupId, ieee) {
    if (!confirm('Remove this device from the group?')) return;

    try {
        const response = await fetch(`/api/groups/${groupId}/remove_device`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee })
        });

        const result = await response.json();

        if (result.error) {
            alert(`Error: ${result.error}`);
            return;
        }

        // Reload groups and reopen modal
        await loadGroups();
        openGroupControl(groupId);

    } catch (error) {
        console.error("Failed to remove device:", error);
        alert('Failed to remove device from group');
    }
}

/**
 * Delete current group (From Modal)
 */
window.deleteCurrentGroup = async function() {
    if (!groupsState.currentGroup) return;

    const group = groupsState.currentGroup;

    if (!confirm(`Delete group "${group.name}"? This cannot be undone.`)) return;

    try {
        const response = await fetch(`/api/groups/${group.id}`, {
            method: 'DELETE'
        });

        const result = await response.json();

        if (result.error) {
            alert(`Error: ${result.error}`);
            return;
        }

        // Close modal
        const modalElement = document.getElementById('groupControlModal');
        if (modalElement) {
            const modalInstance = bootstrap.Modal.getInstance(modalElement);
            if (modalInstance) modalInstance.hide();
        }

        // Reload groups
        await loadGroups();

        alert(`Group "${group.name}" deleted`);

    } catch (error) {
        console.error("Failed to delete group:", error);
        alert('Failed to delete group');
    }
}

/**
 * Add device to group
 */
window.addDeviceToGroup = async function() {
    if (!groupsState.currentGroup) return;

    const select = document.getElementById('addDeviceSelect');
    if (!select) return;

    const ieee = select.value;

    if (!ieee) {
        alert('Please select a device');
        return;
    }

    try {
        const response = await fetch(`/api/groups/${groupsState.currentGroup.id}/add_device`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee })
        });

        const result = await response.json();

        if (result.error) {
            alert(`Error: ${result.error}`);
            return;
        }

        // Reload and reopen
        await loadGroups();
        openGroupControl(groupsState.currentGroup.id);

    } catch (error) {
        console.error("Failed to add device:", error);
        alert('Failed to add device to group');
    }
}

window.showGroupColorMode = function(groupId, mode) {
    const tempPanel = document.getElementById(`groupColorTempPanel_${groupId}`);
    const colorPanel = document.getElementById(`groupColorPickerPanel_${groupId}`);
    if (mode === 'temp') {
        if (tempPanel) tempPanel.style.display = '';
        if (colorPanel) colorPanel.style.display = 'none';
    } else {
        if (tempPanel) tempPanel.style.display = 'none';
        if (colorPanel) colorPanel.style.display = '';
    }
};


/**
 * Send color command to group
 */
window.sendGroupColor = async function(groupId, hexColor, saturation) {
    const command = {};

    // If hexColor is provided, convert it to HS
    if (hexColor) {
        // Simple Hex to RGB conversion
        const r = parseInt(hexColor.slice(1, 3), 16);
        const g = parseInt(hexColor.slice(3, 5), 16);
        const b = parseInt(hexColor.slice(5, 7), 16);

        // Convert RGB to HS (Simplified for Zigbee)
        const hs = rgbToHs(r, g, b);
        command.hs_color = [hs.h, hs.s];
    }

    // If saturation is provided from the slider, update it
    if (saturation !== null && saturation !== undefined) {
        if (!command.hs_color) {
            const picker = document.getElementById(`groupColorPicker_${groupId}`);
            return window.sendGroupColor(groupId, picker.value, saturation);
        }
        command.hs_color[1] = parseInt(saturation);
    }

    await window.controlGroup(groupId, command);
};

/**
 * Helper: RGB to HS conversion
 */
function rgbToHs(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s, v = max;
    const d = max - min;
    s = max === 0 ? 0 : d / max;

    if (max === min) {
        h = 0;
    } else {
        switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            case b: h = (r - g) / d + 4; break;
        }
        h /= 6;
    }
    return { h: Math.round(h * 360), s: Math.round(s * 100) };
}


// Export functions
export {
    loadGroups
};