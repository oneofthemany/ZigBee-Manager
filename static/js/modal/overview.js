/**
 * Device Overview Tab
 * Location: static/js/modal/overview.js
 */

import { state } from '../state.js';
import { CONFIG_DEFINITIONS } from './config.js';

export function renderOverviewTab(device) {
    const s = device.state || {};
    const qos = device.settings?.qos || 0;
    const pollingInterval = device.polling_interval !== undefined ? device.polling_interval : 0;

    // Determine power source for UI hints
    const isBattery = device.power_source === 'Battery' || device.type === 'EndDevice';
    // 1. Determine & Sanitize Configuration Schema
    let rawSchema = [];
    
    // Case A: Backend provided schema (likely containing the bad TRV keys)
    if (device.config_schema && Array.isArray(device.config_schema) && device.config_schema.length > 0) {
        rawSchema = device.config_schema;
    } 
    // Case B: Frontend generated schema
    else {
        rawSchema = CONFIG_DEFINITIONS.filter(def => {
            if (s[def.key] !== undefined) return true;
            return false;
        }).map(def => ({
            name: def.key, 
            label: def.label, 
            type: def.type,
            min: typeof def.min === 'function' ? def.min(device) : def.min,
            max: typeof def.max === 'function' ? def.max(device) : def.max,
            step: typeof def.step === 'function' ? def.step(device) : def.step,
            description: def.description, 
            options: def.options
        }));
    }

    // Iterate over whatever schema we have (from backend or frontend) and
    // force check against our strict conditions in CONFIG_DEFINITIONS.
    const schema = rawSchema.filter(item => {
        // Find if we have a strict definition for this key
        const localDef = CONFIG_DEFINITIONS.find(d => d.key === item.name);
        
        // If we have a local definition with a condition, strictly enforce it.
        // e.g. if item.name is 'window_detection', check isAqaraTRV(device)
        if (localDef && localDef.condition) {
            return localDef.condition(device);
        }

        // If no local restriction exists, allow it (e.g. backend specific keys like 'Power On EP1')
        return true;
    });

    // 2. Separate State by Endpoint (Filtering out Config keys & System keys)
    const configKeys = schema.map(c => c.name);
    const ignoredKeys = [
        'last_seen', 'power_source', 'manufacturer', 'model', 'available',
        'pir_o_to_u_delay', 'on', 'state', 'brightness', 'level', 'on_with_timed_off', 'action',
        'linkquality', 'update_available', 'update_state', 'device', 'device_type'
    ];

    // Define sensor-specific keys that should only appear for devices with those capabilities
    const occupancySensorKeys = [
        'motion', 'occupancy', 'presence', 'motion_on_time',
        'motion_timeout', 'radar_sensitivity', 'radar_state',
        'illuminance_lux','presence_sensitivity', 'keep_time',
        'detection_distance_min','detection_distance_max'
    ];
    const contactSensorKeys = ['contact', 'is_open'];
    const iasZoneKeys = ['zone_status', 'tamper', 'battery_low', 'trouble', 'water_leak', 'smoke', 'co_detected', 'vibration', 'alarm'];

    // Helper function to check if device has a specific cluster
    const hasCluster = (clusterId) => {
        if (!device.capabilities || !Array.isArray(device.capabilities)) return false;
        return device.capabilities.some(ep =>
            ep.inputs?.some(c => c.id === clusterId) ||
            ep.outputs?.some(c => c.id === clusterId)
        );
    };

    // Helper function to determine if device has occupancy sensing capability
    const hasOccupancySensing = () => {
        // Check if device has any occupancy-related clusters
        const hasOccupancyCluster = hasCluster(0x0406);  // Occupancy Sensing
        const hasIASZone = hasCluster(0x0500);  // IAS Zone (motion sensors)
        const hasOnOff = hasCluster(0x0006) && device.model?.includes('SML');  // Philips motion sensors
        const hasTuya = hasCluster(0xEF00); // Tuya Cluster (0xEF00) for radar sensors

        // --- Explicitly allow Tuya _TZE manufacturers ---
        const isTuyaSensor = (device.manufacturer || '').startsWith('_TZE') || (device.manufacturer || '').startsWith('_TZ3');

        return hasOccupancyCluster || hasIASZone || hasOnOff || hasTuya || isTuyaSensor;
    };

    const hasContactSensing = () => {
        const hasIASZoneCluster = hasCluster(0x0500);
        const hasContactData = s.contact !== undefined || s.is_open !== undefined;
        return hasIASZoneCluster && hasContactData;
    };

    const hasIASZone = () => {
        return hasCluster(0x0500);
    };


    const isMultiEndpoint = device.capabilities && Array.isArray(device.capabilities) &&
        device.capabilities.filter(ep => ep.id !== 0).length > 1;
    const multiEpPowerKeys = isMultiEndpoint ? ['power', 'voltage', 'current', 'energy'] : [];


    const groups = { 'Global': {} };

    Object.keys(s).forEach(key => {
        if (configKeys.includes(key) || ignoredKeys.includes(key) || key.startsWith('dp_') || key.endsWith('_raw')) return;
        if (key.startsWith('startup_behavior_')) return;
        if (multiEpPowerKeys.includes(key)) return;

        // Filter out occupancy sensor keys if device doesn't have occupancy sensing
        if (occupancySensorKeys.includes(key) && !hasOccupancySensing()) return;

        // Filter out contact sensor keys if device doesn't have contact sensing
        if (contactSensorKeys.includes(key) && !hasContactSensing()) return;

        // Filter out IAS zone keys if device doesn't have IAS Zone cluster
        if (iasZoneKeys.includes(key) && !hasIASZone()) return;

        const match = key.match(/^(.*)_(\d+)$/);
        if (match) {
            const ep = `Endpoint ${match[2]}`;
            if (!groups[ep]) groups[ep] = {};
            groups[ep][match[1]] = s[key];
        } else {
            groups['Global'][key] = s[key];
        }
    });

    // 3. Render Sensor Data
    let sensorHtml = '';
    const sortedGroupNames = Object.keys(groups).sort((a, b) => {
        if (a === 'Global') return -1;
        if (b === 'Global') return 1;
        return a.localeCompare(b);
    });

    sortedGroupNames.forEach(groupName => {
        const groupData = groups[groupName];
        if (Object.keys(groupData).length === 0) return;

        let rows = Object.keys(groupData).sort().map(k => {
            let val = groupData[k];
            if (typeof val === 'boolean') val = val ? 'True' : 'False';

            // Check if value is a long hex/binary string that needs wrapping
            const needsWrap = typeof val === 'string' && val.length > 40;
            const valueClass = needsWrap ? 'text-break' : '';

            // Formatting specifically for Radar State
            if (k === 'radar_state') {
                let badgeClass = 'bg-secondary';
                if (val === 'move' || val === 'presence') badgeClass = 'bg-success';
                else if (val === 'static') badgeClass = 'bg-info';
                val = `<span class="badge ${badgeClass}">${val}</span>`;
            }

            return `
            <tr>
                <td class="fw-bold small text-capitalize" style="width: 50%">${k.replace(/_/g, ' ')}</td>
                <td class="font-monospace small text-end ${valueClass}" style="word-break: break-all;">${val}</td>
            </tr>`;
        }).join('');

        sensorHtml += `
            <div class="card mb-3">
                <div class="card-header py-1 bg-white fw-bold text-success small">${groupName}</div>
                <div class="card-body p-0">
                    <table class="table table-sm table-striped mb-0"><tbody>${rows}</tbody></table>
                </div>
            </div>
        `;
    });

    if (!sensorHtml) sensorHtml = '<div class="alert alert-light small text-center">No sensor data available.</div>';

    // 4. Maintenance Header
    let statusBadges = '';
    const endpoints = new Set();
    Object.keys(s).forEach(k => {
        const match = k.match(/^on_(\d+)$/);
        if (match) endpoints.add(match[1]);
    });

    if (endpoints.size > 0) {
        Array.from(endpoints).sort().forEach(ep => {
            const key = `on_${ep}`;
            const isOn = s[key];
            const isSuccess = isOn === true || isOn === 'ON' || isOn === 1;
            const color = isSuccess ? 'bg-success' : 'bg-secondary';
            const text = isSuccess ? 'ON' : 'OFF';
            statusBadges += `<span class="badge ${color} me-1" title="Endpoint ${ep}">EP${ep}: ${text}</span>`;
        });
    } else if (s.state !== undefined || s.on !== undefined) {
        const isSuccess = s.on === true || s.state === 'ON';
        const color = isSuccess ? 'bg-success' : 'bg-secondary';
        const text = isSuccess ? 'ON' : 'OFF';
        statusBadges = `<span class="badge ${color} me-1">${text}</span>`;
    }

    const canPair = device.type === 'Router' || device.type === 'Coordinator';
    const pairBtn = canPair ?
        `<button type="button" class="btn btn-outline-success" onclick="window.permitJoinVia('${device.ieee}')" title="Permit Join via this device">
            <i class="fas fa-user-plus"></i> Pair
         </button>` : '';

    const maintenanceHtml = `
        <div class="d-flex justify-content-between align-items-center mb-3 p-2 bg-light border rounded">
            <div>
                <span class="fw-bold text-secondary me-2"><i class="fas fa-tools"></i> Maintenance</span>
                ${statusBadges}
            </div>
            <div class="btn-group btn-group-sm">
                ${pairBtn}
                <button type="button" class="btn btn-outline-secondary" onclick="window.doAction('poll', '${device.ieee}')">
                    <i class="fas fa-sync"></i> Poll
                </button>
                <div class="btn-group btn-group-sm" role="group">
                    <button type="button" class="btn btn-outline-info" onclick="window.doAction('reconfigure', '${device.ieee}')" title="Standard Bindings & Reporting">
                        <i class="fas fa-wrench"></i> Reconfigure
                    </button>
                    <button type="button" class="btn btn-outline-info dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-expanded="false">
                        <span class="visually-hidden">Toggle Dropdown</span>
                    </button>
                    <ul class="dropdown-menu dropdown-menu-end">
                        <li><a class="dropdown-item" href="#" onclick="window.doAction('reconfigure_aggressive', '${device.ieee}'); return false;">
                            <i class="fas fa-bolt text-warning"></i> Apply Aggressive LQI
                        </a></li>
                        <li><a class="dropdown-item" href="#" onclick="window.doAction('reconfigure_baseline', '${device.ieee}'); return false;">
                            <i class="fas fa-undo text-secondary"></i> Restore Baseline
                        </a></li>
                    </ul>
                </div>
                <button type="button" class="btn btn-outline-primary" onclick="window.doAction('interview', '${device.ieee}')">
                    <i class="fas fa-fingerprint"></i> Re-Interview
                </button>
                <button type="button" class="btn btn-outline-danger" onclick="window.doAction('remove', '${device.ieee}')">
                    <i class="fas fa-trash"></i> Remove
                </button>
            </div>
        </div>
    `;

    // 5. Generate Configuration Form
    let dynamicFormHtml = '';
    if (schema.length > 0) {
        dynamicFormHtml += `<h6 class="text-primary mt-3 mb-2 border-bottom pb-1">Device Settings</h6><div class="row g-2">`;
        schema.forEach(def => {
            const val = s[def.name] !== undefined ? s[def.name] : '';
            dynamicFormHtml += `<div class="col-md-6"><label class="form-label x-small mb-0 fw-bold">${def.label}</label>`;
            if (def.type === 'select' && def.options) {
                const opts = def.options.map(o => `<option value="${o.value}" ${val == o.value ? 'selected' : ''}>${o.label}</option>`).join('');
                dynamicFormHtml += `<select class="form-select form-select-sm" name="${def.name}">${opts}</select>`;
            } else {
                dynamicFormHtml += `<input type="${def.type || 'text'}"
                       class="form-control form-control-sm"
                       name="${def.name}"
                       value="${val}"
                       min="${def.min}"
                       max="${def.max}"
                       step="${def.step || 1}">`;
            }
            if (def.description) dynamicFormHtml += `<div class="form-text x-small mt-0">${def.description}</div>`;
            dynamicFormHtml += `</div>`;
        });
        dynamicFormHtml += `</div>`;
    } else {
        dynamicFormHtml = `<div class="text-muted small mt-3 fst-italic text-center">No configurable settings available.</div>`;
    }

    return `
        <form id="configForm" onsubmit="saveConfig(event)">
            ${maintenanceHtml}
            <div class="row">
                <div class="col-md-5">
                    ${sensorHtml}
                </div>
                <div class="col-md-7">
                    <div class="card h-100">
                        <div class="card-header py-1 bg-white fw-bold text-primary">
                            <i class="fas fa-sliders-h"></i> Configuration
                        </div>
                        <div class="card-body">
                            <div class="row g-2 mb-3 border-bottom pb-3">
                                <div class="col-md-4">
                                    <label class="form-label x-small mb-0 fw-bold">MQTT QoS</label>
                                    <select class="form-select form-select-sm" name="qos">
                                        <option value="0" ${qos==0?'selected':''}>0 (Normal)</option>
                                        <option value="1" ${qos==1?'selected':''}>1 (At Least Once)</option>
                                        <option value="2" ${qos==2?'selected':''}>2 (Optimise)</option>
                                    </select>
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label x-small mb-0 fw-bold">Polling (Hub Pull)</label>
                                    <input type="number" class="form-control form-control-sm" name="polling_interval"
                                           value="${pollingInterval}" min="0" step="10">
                                    <div class="form-text x-small mt-0 text-truncate">
                                        Seconds. 0 = Disabled.
                                    </div>
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label x-small mb-0 fw-bold">Reporting (Dev Push)</label>
                                    <input type="number" class="form-control form-control-sm" name="reporting_interval"
                                           placeholder="Default" min="0" step="10">
                                    <div class="form-text x-small mt-0 text-truncate">
                                        Max Interval (s).
                                    </div>
                                </div>
                            </div>

                            ${dynamicFormHtml}
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

// === EXPORTED SAVE FUNCTION ===
export async function saveConfig(e) {
    e.preventDefault();
    const btn = document.getElementById('saveConfigBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Applying...';

    const formData = new FormData(e.target);
    const updates = {};
    let qos = 0;
    let pollingInterval = null;

    for (let [key, value] of formData.entries()) {
        if (value === "") continue;

        if (key === 'qos') { qos = parseInt(value); continue; }
        if (key === 'polling_interval') { pollingInterval = parseInt(value); continue; }

        // Pass reporting_interval as a general update to be handled by sensors
        if (key === 'reporting_interval') {
            updates['reporting_max'] = parseInt(value);
            continue;
        }

        if (key.startsWith('tuya_')) {
            updates[key.replace('tuya_', '')] = parseFloat(value);
        } else {
            updates[key] = !isNaN(value) && value.trim() !== '' ? parseFloat(value) : value;
        }
    }

    try {
        const payload = {
            ieee: state.currentDeviceIeee,
            qos: qos,
            updates: updates
        };

        if (pollingInterval !== null && !isNaN(pollingInterval)) {
            payload.polling_interval = pollingInterval;
        }

        const res = await fetch('/api/device/configure', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (data.success) {
            btn.innerHTML = '<i class="fas fa-check"></i> Done';
            btn.classList.replace('btn-primary', 'btn-success');
            if (window.loadDevices) window.loadDevices();
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
window.saveConfig = saveConfig;