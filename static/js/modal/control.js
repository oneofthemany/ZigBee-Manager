/**
 * Device Control Tab
 * Location: static/js/modal/control.js
 */

import { state } from '../state.js';
import { hasCluster } from './config.js';
import { renderScheduleSection, bindScheduleEvents } from './schedule.js';

// Interaction debounce timer
let interactionTimeout = null;
const INTERACTION_DEBOUNCE_MS = 2000;
let activeTouchSlider = null;

// ============================================================================
// HEATING-CONTROLLER INTEGRATION
// ----------------------------------------------------------------------------
// The heating controller may be actively managing some receivers and TRVs.
// When it is, we disable direct setpoint/mode/sensor-type controls for those
// devices so the user isn't fighting the controller. Aqara TRV-local features
// (window detection, child lock, valve detection, calibrate) remain available
// but are routed through /api/heating/controller/trv/* so config.yaml stays
// in sync with what's on the device.
// ============================================================================

export async function refreshHeatingManaged() {
    try {
        const res = await fetch('/api/heating/controller/managed');
        const data = await res.json();
        if (data && data.success) {
            state.heatingManaged = {
                enabled: !!data.enabled,
                ieees: new Set((data.ieees || []).map(String))
            };
            return state.heatingManaged;
        }
    } catch (e) {
        console.debug('Heating managed fetch skipped:', e);
    }
    state.heatingManaged = state.heatingManaged || { enabled: false, ieees: new Set() };
    return state.heatingManaged;
}

function isHeatingManaged(ieee) {
    const hm = state.heatingManaged;
    if (!hm || !hm.enabled) return false;
    return hm.ieees && hm.ieees.has(String(ieee));
}

function isAqaraTRV(device) {
    const manuf = String(device.manufacturer || '').toLowerCase();
    const model = String(device.model || '').toLowerCase();
    const aqaraLike = manuf.includes('lumi') || manuf.includes('aqara');
    const trvMarker =
        model.includes('airrtc') ||
        model.includes('agl001') ||
        (aqaraLike && model.includes('thermostat'));
    return (aqaraLike && trvMarker) || model.includes('agl001');
}


// Which device state keys are considered valid "ambient room temperature"
// readings. Excludes setpoints, internal TRV pipe readings that misreport
// when a radiator is hot, and any key that clearly isn't an air-temp.
const AMBIENT_TEMP_KEYS = [
    'temperature',               // generic sensor (cluster 0x0402)
    'local_temperature',         // thermostat self-report (0x0201 0x0000)
    'current_temperature',       // HA climate alias
    'room_temperature',          // some multi-sensor devices
    'air_temperature',           // some air-quality sensors
    'ambient_temperature',       // rare
];

// Explicitly reject keys that look like temperatures but aren't ambient.
const REJECT_TEMP_KEYS = new Set([
    'setpoint', 'occupied_heating_setpoint', 'occupied_cooling_setpoint',
    'unoccupied_heating_setpoint', 'unoccupied_cooling_setpoint',
    'target_temp', 'temperature_setpoint', 'heating_setpoint',
    'external_temperature',      // this is what *we* push to the TRV; not a source
    'internal_temperature',      // TRV's own pipe probe — unreliable for ambient
    'device_temperature',        // chip temperature, not ambient
    'cpu_temperature',
]);

function getTemperatureSources(excludeIeee) {
    const out = [];
    const cache = state.deviceCache || {};

    for (const [ieee, dev] of Object.entries(cache)) {
        if (!dev || ieee === excludeIeee) continue;
        const s = dev.state || {};

        // Search known ambient keys in priority order
        let temp = null;
        let tempKey = null;
        for (const k of AMBIENT_TEMP_KEYS) {
            if (REJECT_TEMP_KEYS.has(k)) continue;
            const v = s[k];
            const f = (v == null) ? NaN : Number(v);
            // Realistic indoor range: 0–50 °C. Reject exact 0 (uninitialised),
            // anything negative (probably outdoor/device probe), anything wild.
            if (!isNaN(f) && f > 0 && f < 50) {
                temp = f;
                tempKey = k;
                break;
            }
        }

        if (temp == null) continue;

        // Descriptive device-type hint so the dropdown tells the user *why*
        // this device is offered (motion sensor, thermostat, etc.)
        const caps = dev.capability_list || [];
        let kind = 'sensor';
        if (caps.includes('thermostat') || s.system_mode !== undefined) kind = 'thermostat';
        else if (caps.includes('motion_sensor') || caps.includes('occupancy_sensing')) kind = 'motion';
        else if (caps.includes('contact_sensor')) kind = 'contact';
        else if (caps.includes('air_quality')) kind = 'air quality';
        else if ('humidity' in s || 'pressure' in s) kind = 'climate';
        else if ('illuminance' in s || 'lux' in s) kind = 'multi-sensor';

        out.push({
            ieee,
            name: dev.friendly_name || dev.name || ieee,
            model: dev.model || '',
            temperature: temp,
            temp_key: tempKey,
            kind,
        });
    }

    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
}

function renderTempSourceOptions(excludeIeee) {
    const sources = getTemperatureSources(excludeIeee);
    const header = `<option value="">— Manual entry —</option>`;
    if (!sources.length) {
        return header + `<option value="" disabled>No temperature-reporting devices found</option>`;
    }
    return header + sources.map(src =>
        `<option value="${src.ieee}">${src.name} (${src.temperature.toFixed(1)}°C)</option>`
    ).join('');
}

function heatingManagedBanner() {
    return `
        <div class="alert alert-info small mb-0 mt-2" role="alert">
            <i class="fas fa-cogs me-2"></i>
            Managed by the <strong>Heating Controller</strong> — the controller
            drives this receiver to the max setpoint when any room in its circuit
            is calling for heat, so the boiler fires. Set per-room temperatures
            on the individual TRVs.
        </div>`;
}

function managedBadge() {
    return `<span class="badge bg-info text-dark ms-1" title="Managed by Heating Controller">
        <i class="fas fa-cogs"></i> Managed
    </span>`;
}

/**
 * Mark control interaction as active and set debounced clear
 */
function setInteractionActive() {
    state.controlInteractionActive = true;
    if (interactionTimeout) clearTimeout(interactionTimeout);
    interactionTimeout = setTimeout(() => {
        if (!activeTouchSlider) {
            state.controlInteractionActive = false;
        }
    }, INTERACTION_DEBOUNCE_MS);
}

// Touch-aware interaction lock
document.addEventListener('touchstart', function(e) {
    if (e.target.matches('#tab-control input[type="range"]')) {
        activeTouchSlider = e.target;
        setInteractionActive();
    }
}, { passive: true });

document.addEventListener('touchend', function() {
    if (activeTouchSlider) {
        activeTouchSlider = null;
        setInteractionActive();
    }
}, { passive: true });

document.addEventListener('touchcancel', function() {
    if (activeTouchSlider) {
        activeTouchSlider = null;
        setInteractionActive();
    }
}, { passive: true });

/**
 * Send brightness command with optimistic UI update
 */
window.sendBrightnessCommand = function(ieee, value, epId, labelId) {
    setInteractionActive();

    // Optimistic UI update - update label immediately
    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Brightness: ${value}%`;
    }

    // Send command
    window.sendCommand(ieee, 'brightness', value, epId);
};

/**
 * Handle slider input (during drag) - optimistic label update only
 */
window.onBrightnessInput = function(value, labelId) {
    setInteractionActive();
    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Brightness: ${value}%`;
    }
};

/**
 * Send color temp command with optimistic UI update
 */
window.sendColorTempCommand = function(ieee, kelvin, epId, labelId) {
    setInteractionActive();

    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Color Temp: ${kelvin}K`;
    }

    window.sendCommand(ieee, 'color_temp', kelvin, epId);
};

/**
 * Handle color temp slider input
 */
window.onColorTempInput = function(kelvin, labelId) {
    setInteractionActive();
    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Color Temp: ${kelvin}K`;
    }
};

/**
 * Send position command for covers with optimistic update
 */
window.sendPositionCommand = function(ieee, value, labelId) {
    setInteractionActive();

    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Position: ${value}%`;
    }

    window.sendCommand(ieee, 'position', value);
};

/**
 * Handle position slider input
 */
window.onPositionInput = function(value, labelId) {
    setInteractionActive();
    const label = document.getElementById(labelId);
    if (label) {
        label.textContent = `Position: ${value}%`;
    }
};

/**
 * Update only the values/badges in control tab without full re-render
 * Called by refreshModalState when user is interacting
 */
export function updateControlValues(device) {
    const s = device.state || {};
    const ieee = device.ieee;
    const interacting = state.controlInteractionActive;

    // Update ON/OFF badges and controls for each endpoint
    if (device.capabilities && Array.isArray(device.capabilities)) {
        device.capabilities.forEach(ep => {
            const epId = ep.id;
            const isOn = s[`on_${epId}`] !== undefined ? s[`on_${epId}`] : (epId === 1 ? s.on : false);

            // Update ON/OFF badge (always safe)
            const badge = document.querySelector(`[data-ep-badge="${epId}"]`);
            if (badge) {
                badge.className = isOn ? 'badge bg-success' : 'badge bg-secondary';
                badge.textContent = isOn ? 'ON' : 'OFF';
            }

            // Skip all slider/label updates during active interaction
            if (interacting) return;

            // Update brightness slider and label
            const brightness = s[`brightness_${epId}`] !== undefined ? s[`brightness_${epId}`] : (epId === 1 ? s.brightness : null);
            if (brightness !== null) {
                const briLabelId = `bri-label-${ieee}-${epId}`;
                const briLabel = document.getElementById(briLabelId);
                if (briLabel) {
                    briLabel.textContent = `Brightness: ${brightness}%`;
                }
            }

            // Update color picker and saturation slider from device state
            const hue = s.hue || s.color_hue || 0;
            const sat = s.saturation || s.color_saturation || 254;

            // Convert ZCL format (0-254) to CSS format (hue 0-360, sat 0-100)
            const cssHue = Math.round((hue / 254) * 360);
            const cssSat = Math.round((sat / 254) * 100);

            // Update color picker
            const picker = document.getElementById(`colorPicker_${ieee}_${epId}`);
            if (picker && window.hslToHex) {
                picker.value = window.hslToHex(cssHue, cssSat, 50);
            }

            // Update saturation slider
            const satSlider = document.getElementById(`satSlider_${ieee}_${epId}`);
            if (satSlider) {
                satSlider.value = cssSat;
            }

            // Update color temp slider and label
            const colorTemp = s[`color_temp_${epId}`] || (epId === 1 ? s.color_temp : null);
            if (colorTemp) {
                const kelvin = Math.round(1000000 / colorTemp);
                const ctLabelId = `ct-label-${ieee}-${epId}`;
                const ctLabel = document.getElementById(ctLabelId);
                if (ctLabel) {
                    ctLabel.textContent = `Color Temp: ${kelvin}K`;
                }
            }
        });
    }

    // Update thermostat current temp display
    const currentTempEl = document.querySelector('[data-thermostat-current]');
    if (currentTempEl) {
        const tempKeys = ['internal_temperature', 'temperature', 'local_temperature'];
        for (const key of tempKeys) {
            if (s[key] !== undefined && s[key] !== null && Number(s[key]) !== 0) {
                currentTempEl.textContent = `${Number(s[key]).toFixed(1)}°C`;
                break;
            }
        }
    }
     // Update thermostat target setpoint display
    const setpointEl = document.querySelector(`[data-thermostat-setpoint="${ieee}"]`);
    if (setpointEl) {
        const rawTarget = s.occupied_heating_setpoint || s.heating_setpoint || s.temperature_setpoint;
        if (rawTarget !== undefined) {
            setpointEl.textContent = `${Number(rawTarget).toFixed(1)}°C`;
        }
    }

    // Update thermostat badge (heating/standby/off)
    const badgeEl = document.querySelector(`[data-thermostat-badge="${ieee}"]`);
    if (badgeEl) {
        const hvacAction = s.hvac_action || 'idle';
        const sysMode = s.system_mode || 'off';
        let badgeHtml;
        if (hvacAction === 'heating') {
            badgeHtml = '<span class="badge bg-danger"><i class="fas fa-fire"></i> Heating</span>';
        } else if (sysMode === 'off' || hvacAction === 'off') {
            badgeHtml = '<span class="badge bg-dark"><i class="fas fa-power-off"></i> Off</span>';
        } else {
            badgeHtml = '<span class="badge bg-warning text-dark"><i class="fas fa-pause"></i> Standby</span>';
        }
        // Only update the first child (badge), preserve battery badge if present
        const existingBadge = badgeEl.querySelector('.badge:first-child');
        if (existingBadge) {
            existingBadge.outerHTML = badgeHtml;
        }
    }

    // Update Aqara TRV state-only badges (window/valve/calibration)
    const aqHdr = document.querySelector(`[data-aqara-trv-badges="${ieee}"]`);
    if (aqHdr) {
        const windowOpen = !!s.window_open;
        const valveAlarm = !!s.valve_alarm;
        const calStatus = s.calibration_status || s.motor_calibration || 'idle';
        aqHdr.innerHTML =
            (windowOpen ? '<span class="badge bg-warning text-dark me-1"><i class="fas fa-window-maximize"></i> Window open</span>' : '') +
            (valveAlarm ? '<span class="badge bg-danger me-1"><i class="fas fa-exclamation-triangle"></i> Valve alarm</span>' : '') +
            `<span class="badge bg-secondary">${String(calStatus).replace(/_/g, ' ')}</span>`;
    }
}

export function renderControlTab(device) {
    const s = device.state || {};
    let html = '<div class="row g-3">';
    let controlsFound = false;

    // --- Window Covering (0x0102) ---
    const hasCover = hasCluster(device, 0x0102);
    if (hasCover) {
        controlsFound = true;
        const position = s.position !== undefined ? s.position : 50;
        const isClosed = s.is_closed;
        const posLabelId = `pos-label-${device.ieee}`;

        html += `
        <div class="col-12">
            <div class="card">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-blinds"></i> Window Covering</strong>
                    ${isClosed !== undefined ? (isClosed ? '<span class="badge bg-secondary">Closed</span>' : '<span class="badge bg-success">Open</span>') : ''}
                </div>
                <div class="card-body">
                    <div class="row g-3">
                        <div class="col-12">
                            <label class="form-label small text-muted">Actions</label>
                            <div class="btn-group w-100">
                                <button type="button" class="btn btn-outline-success" onclick="window.sendCommand('${device.ieee}', 'open')"><i class="fas fa-arrow-up"></i> Open</button>
                                <button type="button" class="btn btn-outline-danger" onclick="window.sendCommand('${device.ieee}', 'stop')"><i class="fas fa-stop"></i> Stop</button>
                                <button type="button" class="btn btn-outline-secondary" onclick="window.sendCommand('${device.ieee}', 'close')"><i class="fas fa-arrow-down"></i> Close</button>
                            </div>
                        </div>
                        <div class="col-12">
                            <label id="${posLabelId}" class="form-label small text-muted">Position: ${position}%</label>
                            <input type="range" class="form-range" min="0" max="100" value="${position}"
                                   oninput="window.onPositionInput(this.value, '${posLabelId}')"
                                   onchange="window.sendPositionCommand('${device.ieee}', this.value, '${posLabelId}')">
                            <div class="d-flex justify-content-between small text-muted">
                                <span>Closed (0%)</span>
                                <span>Open (100%)</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    }

    // --- Thermostat (0x0201) ---
    const hasThermostat = hasCluster(device, 0x0201);
    if (hasThermostat) {
        controlsFound = true;
        const tempKeys = ['internal_temperature', 'temperature', 'local_temperature'];
        let validTemp = null;

        for (const key of tempKeys) {
            const val = s[key];
            if (val !== undefined && val !== null && Number(val) !== 0) {
                validTemp = val;
                break;
            }
        }
        if (validTemp === null) {
             for (const key of tempKeys) {
                if (s[key] !== undefined && s[key] !== null) {
                    validTemp = s[key];
                    break;
                }
            }
        }

        const currentTemp = (validTemp !== null && Number(validTemp) !== 0) ? Number(validTemp).toFixed(1) : "--";
        const rawTarget = s.occupied_heating_setpoint || s.heating_setpoint || 20;
        const targetTemp = Number(rawTarget).toFixed(1);
        const systemMode = s.system_mode || 'off';
        const runningState = s.running_state || 0;
        const piDemand = s.heating_demand || s.pi_heating_demand || 0;
        const battery = s.battery || 0;

        // Heating detection: running_state (Hive/ZCL standard) then hvac_action (derived)
        const isHeating = (typeof runningState === 'number' && (runningState & 0x0001))
                       || String(runningState).includes('heat')
                       || s.hvac_action === 'heating';

        let thermostatBadge;
        if (isHeating) {
            thermostatBadge = '<span class="badge bg-danger"><i class="fas fa-fire"></i> Heating</span>';
        } else if (String(systemMode).toLowerCase() === 'off') {
            thermostatBadge = '<span class="badge bg-dark"><i class="fas fa-power-off"></i> Off</span>';
        } else if (String(systemMode).toLowerCase() !== 'off' && !isHeating) {
            thermostatBadge = '<span class="badge bg-warning text-dark"><i class="fas fa-pause"></i> Standby</span>';
        } else {
            thermostatBadge = '<span class="badge bg-secondary"><i class="fas fa-pause"></i> Idle</span>';
        }

        // Heating-controller management status for this device.
        // Only the RECEIVER's direct controls are locked — the controller
        // drives it to max to guarantee the boiler fires. TRVs and standard
        // thermostats stay user-editable so setpoints work as the room gate.
        const managed = isHeatingManaged(device.ieee);
        const isHiveReceiverEarly = (device.model || '').toUpperCase().includes('SLR') ||
                                    (device.model || '').toUpperCase().includes('RECEIVER');
        const receiverManaged = managed && isHiveReceiverEarly;
        const dis = receiverManaged ? 'disabled' : '';

        // Detect Hive receiver — simplified controls
        const isHiveReceiver = (device.model || '').toUpperCase().includes('SLR') ||
                               (device.model || '').toUpperCase().includes('RECEIVER');

        if (isHiveReceiver) {
            // --- HIVE RECEIVER: full heating controls (disabled when managed) ---
            html += `
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-light d-flex justify-content-between align-items-center">
                        <strong><i class="fas fa-fire-alt"></i> Heatlink</strong>
                        <div data-thermostat-badge="${device.ieee}">
                            ${thermostatBadge}${managed ? managedBadge() : ''}
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="row g-3">
                            <div class="col-md-6">
                                <div class="text-center p-3 bg-light rounded">
                                    <small class="text-muted d-block mb-1">Current</small>
                                    <h2 class="mb-0" data-thermostat-current>${currentTemp}°C</h2>
                                </div>
                            </div>
                            <div class="col-md-6">
                                <div class="text-center p-3 bg-primary bg-opacity-10 rounded">
                                    <small class="text-muted d-block mb-1">Target</small>
                                    <h2 class="mb-0 text-primary" data-thermostat-setpoint="${device.ieee}">${targetTemp}°C</h2>
                                </div>
                            </div>
                            <div class="col-12">
                                <label class="form-label fw-bold"><i class="fas fa-sliders-h"></i> Set Target</label>
                                <div class="input-group">
                                    <button class="btn btn-outline-secondary" ${dis} onclick="window.adjustThermostat('${device.ieee}', -0.5)">−</button>
                                    <input type="number" id="thermostat-setpoint-${device.ieee}" class="form-control text-center"
                                           value="${targetTemp}" step="0.5" min="5" max="35" ${dis}>
                                    <button class="btn btn-outline-secondary" ${dis} onclick="window.adjustThermostat('${device.ieee}', 0.5)">+</button>
                                    <button class="btn btn-primary" ${dis} onclick="window.setThermostatTemp('${device.ieee}')">Set</button>
                                </div>
                            </div>
                            <div class="col-12">
                                <label class="form-label fw-bold"><i class="fas fa-cog"></i> Mode</label>
                                <select id="hvac-mode-${device.ieee}" class="form-select" ${dis}
                                        onchange="window.setHvacMode('${device.ieee}', this.value)">
                                    <option value="off" ${String(systemMode).toLowerCase() === 'off' ? 'selected' : ''}>Off</option>
                                    <option value="heat" ${String(systemMode).toLowerCase() === 'heat' ? 'selected' : ''}>Heat</option>
                                    <option value="auto" ${String(systemMode).toLowerCase() === 'auto' ? 'selected' : ''}>Auto</option>
                                </select>
                            </div>
                            ${piDemand > 0 ? `
                            <div class="col-12">
                                <label class="form-label small text-muted">Heat Demand: ${piDemand}%</label>
                                <div class="progress" style="height: 8px;">
                                    <div class="progress-bar bg-danger" style="width: ${piDemand}%"></div>
                                </div>
                            </div>` : ''}
                            ${managed ? `<div class="col-12">${heatingManagedBanner()}</div>` : ''}
                        </div>
                    </div>
                </div>
            </div>
            ${renderScheduleSection(device.ieee)}`;
        } else {
            const isHiveThermostat = (device.model || '').toUpperCase().includes('SLT');

            if (isHiveThermostat) {
                // --- HIVE THERMOSTAT: read-only temperature sensor ---
                html += `
                <div class="col-12">
                    <div class="card">
                        <div class="card-header bg-light d-flex justify-content-between align-items-center">
                            <strong><i class="fas fa-thermometer-half"></i> Thermostat</strong>
                            <div>
                                ${battery > 0
                                    ? `<span class="badge bg-${battery < 20 ? 'warning text-dark' : 'success'}"><i class="fas fa-battery-${battery < 20 ? 'quarter' : 'full'}"></i> ${battery}%</span>`
                                    : ''}
                            </div>
                        </div>
                        <div class="card-body text-center">
                            <div class="p-3 bg-light rounded">
                                <small class="text-muted d-block mb-1">Room Temperature</small>
                                <h1 class="mb-0" data-thermostat-current>${currentTemp}°C</h1>
                            </div>
                            <div class="small text-muted mt-2 fst-italic">
                                <i class="fas fa-info-circle"></i>
                                Temperature sensor — heating is controlled via the Heatlink.
                            </div>
                        </div>
                    </div>
                </div>`;
            } else {
                // --- STANDARD THERMOSTAT / TRV: full controls (disabled when managed) ---
                html += `
                <div class="col-12">
                    <div class="card">
                        <div class="card-header bg-light d-flex justify-content-between align-items-center">
                            <strong><i class="fas fa-thermometer-half"></i> Thermostat</strong>
                            <div data-thermostat-badge="${device.ieee}">
                                ${thermostatBadge}
                                ${battery > 0 && battery < 20
                                    ? `<span class="badge bg-warning text-dark ms-1"><i class="fas fa-battery-quarter"></i> ${battery}%</span>`
                                    : ''}
                                ${managed ? managedBadge() : ''}
                            </div>
                        </div>
                        <div class="card-body">
                            <div class="row g-3">
                                <div class="col-md-6">
                                    <div class="text-center p-3 bg-light rounded">
                                        <small class="text-muted d-block mb-1">Current</small>
                                        <h2 class="mb-0" data-thermostat-current>${currentTemp}°C</h2>
                                    </div>
                                </div>
                                <div class="col-md-6">
                                    <div class="text-center p-3 bg-primary bg-opacity-10 rounded">
                                        <small class="text-muted d-block mb-1">Target</small>
                                        <h2 class="mb-0 text-primary" data-thermostat-setpoint="${device.ieee}">${targetTemp}°C</h2>
                                    </div>
                                </div>
                                <div class="col-12">
                                    <label class="form-label fw-bold"><i class="fas fa-cog"></i> Mode</label>
                                    <select id="hvac-mode-${device.ieee}" class="form-select" ${dis}
                                            onchange="window.setHvacMode('${device.ieee}', this.value)">
                                        <option value="off" ${String(systemMode).toLowerCase() === 'off' ? 'selected' : ''}>Off</option>
                                        <option value="heat" ${String(systemMode).toLowerCase() === 'heat' ? 'selected' : ''}>Heat</option>
                                        <option value="auto" ${String(systemMode).toLowerCase() === 'auto' ? 'selected' : ''}>Auto</option>
                                    </select>
                                </div>
                                <div class="col-12">
                                    <label class="form-label fw-bold"><i class="fas fa-sliders-h"></i> Set Target</label>
                                    <div class="input-group">
                                        <button class="btn btn-outline-secondary" ${dis} onclick="window.adjustThermostat('${device.ieee}', -0.5)">−</button>
                                        <input type="number" id="thermostat-setpoint-${device.ieee}" class="form-control text-center"
                                               value="${targetTemp}" step="0.5" min="5" max="35" ${dis}>
                                        <button class="btn btn-outline-secondary" ${dis} onclick="window.adjustThermostat('${device.ieee}', 0.5)">+</button>
                                        <button class="btn btn-primary" ${dis} onclick="window.setThermostatTemp('${device.ieee}')">Set</button>
                                    </div>
                                </div>
                                ${piDemand > 0 ? `
                                <div class="col-12">
                                    <label class="form-label small text-muted">Heat Demand: ${piDemand}%</label>
                                    <div class="progress" style="height: 8px;">
                                        <div class="progress-bar bg-danger" style="width: ${piDemand}%"></div>
                                    </div>
                                </div>` : ''}
                                ${managed ? `<div class="col-12">${heatingManagedBanner()}</div>` : ''}
                            </div>
                        </div>
                    </div>
                </div>
                ${renderScheduleSection(device.ieee)}`;
            }
        }
    }

    // --- Aqara TRV local features (always shown for Aqara TRVs with 0x0201) ---
    if (isAqaraTRV(device) && hasCluster(device, 0x0201)) {
        controlsFound = true;
        const managed = isHeatingManaged(device.ieee);
        const windowDet = !!s.window_detection;
        const childLock = !!s.child_lock;
        const valveDet = !!s.valve_detection;
        const valveAlarm = !!s.valve_alarm;
        const windowOpen = !!s.window_open;
        const calStatus = s.calibration_status || s.motor_calibration || 'idle';
        const sensorType = s.sensor_type === 'external' ? 'external' : 'internal';
        const extTemp = (s.external_temperature != null)
            ? Number(s.external_temperature).toFixed(1) : '';
        // TRV sensor-type and external-temp push are always user-editable.
        // When the heating controller is managing the circuit it still drives
        // the receiver, but TRV-local config (sensor, external temp, window
        // detection, child lock, calibration) stays under user control.
        const sensorExtDis = '';

        const viaCtrl = managed ? ' (via Heating Controller)' : '';

        html += `
        <div class="col-12">
            <div class="card">
                <div class="card-header bg-light d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <strong><i class="fas fa-temperature-low"></i> Aqara TRV Features${viaCtrl}</strong>
                    <div data-aqara-trv-badges="${device.ieee}">
                        ${windowOpen ? '<span class="badge bg-warning text-dark me-1"><i class="fas fa-window-maximize"></i> Window open</span>' : ''}
                        ${valveAlarm ? '<span class="badge bg-danger me-1"><i class="fas fa-exclamation-triangle"></i> Valve alarm</span>' : ''}
                        <span class="badge bg-secondary">${String(calStatus).replace(/_/g, ' ')}</span>
                    </div>
                </div>
                <div class="card-body">
                    <div class="row g-3">
                        <div class="col-md-6">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" id="aq-wd-${device.ieee}" ${windowDet ? 'checked' : ''}
                                    onchange="window.aqaraSetFeature('${device.ieee}', 'window_detection', this.checked)">
                                <label class="form-check-label" for="aq-wd-${device.ieee}">
                                    <i class="fas fa-window-maximize me-1"></i> Window Detection
                                </label>
                            </div>
                            <div class="form-text small">Pause heating when a window is detected open.</div>
                        </div>
                        <div class="col-md-6">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" id="aq-cl-${device.ieee}" ${childLock ? 'checked' : ''}
                                    onchange="window.aqaraSetFeature('${device.ieee}', 'child_lock', this.checked)">
                                <label class="form-check-label" for="aq-cl-${device.ieee}">
                                    <i class="fas fa-lock me-1"></i> Child Lock
                                </label>
                            </div>
                            <div class="form-text small">Lock the physical dial on the TRV.</div>
                        </div>
                        <div class="col-md-6">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" id="aq-vd-${device.ieee}" ${valveDet ? 'checked' : ''}
                                    onchange="window.aqaraSetFeature('${device.ieee}', 'valve_detection', this.checked)">
                                <label class="form-check-label" for="aq-vd-${device.ieee}">
                                    <i class="fas fa-faucet me-1"></i> Valve Detection
                                </label>
                            </div>
                            <div class="form-text small">Detect and report a seized valve.</div>
                        </div>
                        <div class="col-md-6 d-flex align-items-end">
                            <button class="btn btn-outline-secondary w-100" onclick="window.aqaraCalibrate('${device.ieee}')">
                                <i class="fas fa-wrench me-1"></i> Calibrate Valve
                            </button>
                        </div>
                        <div class="col-12"><hr class="my-0"></div>
                        <div class="col-md-4">
                            <label class="form-label small text-muted">Temperature Sensor</label>
                            <select class="form-select" id="aq-st-${device.ieee}" ${sensorExtDis}
                                onchange="window.aqaraSetSensorType('${device.ieee}', this.value)">
                                <option value="internal" ${sensorType === 'internal' ? 'selected' : ''}>Internal</option>
                                <option value="external" ${sensorType === 'external' ? 'selected' : ''}>External</option>
                            </select>
                            <div class="form-text small">Switch to External to use a room sensor below.</div>
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small text-muted">External Source</label>
                            <select class="form-select" id="aq-src-${device.ieee}" ${sensorExtDis}
                                onchange="window.aqaraSourceChanged('${device.ieee}', this.value)">
                                ${renderTempSourceOptions(device.ieee)}
                            </select>
                            <div class="form-text small">Pick a device to copy its room temperature from.</div>
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small text-muted">Temperature (°C)</label>
                            <div class="input-group">
                                <input type="number" class="form-control" id="aq-ext-${device.ieee}"
                                       step="0.1" min="-40" max="80"
                                       value="${extTemp}" placeholder="e.g. 19.5" ${sensorExtDis}>
                                <button class="btn btn-outline-primary" ${sensorExtDis}
                                        onclick="window.aqaraPushExternalTemp('${device.ieee}')"
                                        title="Write the current value to the TRV">
                                    <i class="fas fa-paper-plane"></i>
                                </button>
                            </div>
                            <div class="form-text small">Pre-filled when a source is selected. Editable.</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    }

    // --- On/Off, Level, Color Clusters ---
    if (device.capabilities && Array.isArray(device.capabilities)) {
        device.capabilities.forEach(ep => {
            const epId = ep.id;

            // Skip sensors/buttons
            const capList = device.capability_list || [];
            if (ep.component_type === "sensor" ||
                capList.includes('contact_sensor') ||
                (capList.includes('motion_sensor') && !capList.includes('switch') && !capList.includes('light'))) {
                return;
            }

            const clusters = (ep.inputs || []).concat(ep.outputs || []);
            const hasOnOff = clusters.some(c => c.id === 0x0006);
            const hasLevel = clusters.some(c => c.id === 0x0008);
            const hasColor = clusters.some(c => c.id === 0x0300);
            const hasElectrical = clusters.some(c => c.id === 0x0B04);
            const hasMultiState = clusters.some(c => [0x0012, 0x0013, 0x0014].includes(c.id));

            const componentType = ep.component_type || 'switch';
            const isLight = componentType === 'light';

            if (hasOnOff || hasLevel || hasColor) {
                controlsFound = true;
                let isOn = s[`on_${epId}`] !== undefined ? s[`on_${epId}`] : (epId === 1 ? s.on : false);
                let brightness = s[`brightness_${epId}`] !== undefined ? s[`brightness_${epId}`] : (epId === 1 ? s.brightness : 0);
                let colorTemp = s[`color_temp_${epId}`] || (epId === 1 ? s.color_temp : 370);
                let kelvin = colorTemp ? Math.round(1000000 / colorTemp) : 2700;

                // Unique IDs for this endpoint's controls
                const briLabelId = `bri-label-${device.ieee}-${epId}`;
                const ctLabelId = `ct-label-${device.ieee}-${epId}`;

                // Use componentType to determine header/icon
                const icon = isLight ? '<i class="fas fa-lightbulb text-warning"></i>' : '<i class="fas fa-plug text-info"></i>';
                const label = isLight ? 'Light' : 'Switch';

                html += `
                <div class="col-12 col-md-6 mb-3">
                    <div class="card h-100">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <strong>${icon} ${label} (EP${epId})</strong>
                            <span data-ep-badge="${epId}" class="${isOn ? 'badge bg-success' : 'badge bg-secondary'}">${isOn ? 'ON' : 'OFF'}</span>
                        </div>
                        <div class="card-body">`;

                if (hasOnOff) {
                    html += `
                        <div class="mb-3">
                            <label class="form-label small text-muted">Power</label>
                            <div class="btn-group w-100">
                                <button type="button" class="btn btn-success" onclick="window.sendCommand('${device.ieee}', 'on', null, ${epId})">On</button>
                                <button type="button" class="btn btn-secondary" onclick="window.sendCommand('${device.ieee}', 'off', null, ${epId})">Off</button>
                                <button type="button" class="btn btn-outline-primary" onclick="window.sendCommand('${device.ieee}', 'toggle', null, ${epId})">Toggle</button>
                            </div>
                        </div>`;
                }

                if (hasLevel) {
                    html += `
                        <div class="mb-3">
                            <label id="${briLabelId}" class="form-label small text-muted">Brightness: ${brightness}%</label>
                            <input type="range" class="form-range" min="0" max="100" value="${brightness}"
                                   oninput="window.onBrightnessInput(this.value, '${briLabelId}')"
                                   onchange="window.sendBrightnessCommand('${device.ieee}', this.value, ${epId}, '${briLabelId}')">
                        </div>`;
                }

                if (hasColor) {
                    const hue = s.hue || s.color_hue || 0;
                    const sat = s.saturation || s.color_saturation || 254;
                    const colorMode = s.color_mode || 'color_temp';
                    const cssHue = Math.round((hue / 254) * 360);
                    const cssSat = Math.round((sat / 254) * 100);

                    html += `
                        <div class="mb-3">
                            <label class="form-label small text-muted">Color Mode</label>
                            <div class="btn-group w-100 mb-2" role="group">
                                <input type="radio" class="btn-check" name="colorMode_${epId}" id="colorModeTemp_${epId}"
                                       ${colorMode === 'color_temp' ? 'checked' : ''} onchange="window.showColorMode('${device.ieee}', ${epId}, 'temp')">
                                <label class="btn btn-outline-secondary btn-sm" for="colorModeTemp_${epId}">Temp</label>
                                <input type="radio" class="btn-check" name="colorMode_${epId}" id="colorModeColor_${epId}"
                                       ${colorMode !== 'color_temp' ? 'checked' : ''} onchange="window.showColorMode('${device.ieee}', ${epId}, 'color')">
                                <label class="btn btn-outline-secondary btn-sm" for="colorModeColor_${epId}">Color</label>
                            </div>
                        </div>
                        <div id="colorTempPanel_${epId}" class="mb-3" style="${colorMode !== 'color_temp' ? 'display:none' : ''}">
                            <label id="${ctLabelId}" class="form-label small text-muted">Color Temp: ${kelvin}K</label>
                            <input type="range" class="form-range" min="2000" max="6500" value="${kelvin}"
                                   style="background: linear-gradient(to right, #ffae00, #ffead1, #fff, #d1eaff, #99ccff);"
                                   oninput="window.onColorTempInput(this.value, '${ctLabelId}')"
                                   onchange="window.sendColorTempCommand('${device.ieee}', this.value, ${epId}, '${ctLabelId}')">
                        </div>
                        <div id="colorPickerPanel_${epId}" class="mb-3" style="${colorMode === 'color_temp' ? 'display:none' : ''}">
                            <label class="form-label small text-muted">Color</label>
                            <div class="d-flex gap-2 align-items-center">
                                <input type="color" class="form-control form-control-color" id="colorPicker_${device.ieee}_${epId}"
                                       value="${window.hslToHex ? window.hslToHex(cssHue, cssSat, 50) : '#ffffff'}"
                                       onchange="window.sendColorFromPicker('${device.ieee}', this.value, ${epId})">
                                <div class="flex-grow-1">
                                    <label class="form-label small text-muted mb-0">Saturation</label>
                                    <input type="range" class="form-range" min="0" max="100" value="${cssSat}" id="satSlider_${device.ieee}_${epId}"
                                           onchange="window.sendHSColor('${device.ieee}', null, this.value, ${epId})">
                                </div>
                            </div>
                        </div>`;
                }

                // Show multistate/electrical for switches at end of card body
                if (!isLight && (hasMultiState || hasElectrical)) {
                    html += `<div class="mt-3 pt-3 border-top">`;

                    if (hasElectrical) {
                        const power   = s[`power_${epId}`] ?? 0;
                        const voltage = s[`voltage_${epId}`] ?? 0;
                        const current = s[`current_${epId}`] ?? 0;
                        html += `
                        <div class="small text-muted mb-2"><i class="fas fa-bolt"></i> Power Monitoring</div>
                        <div class="d-flex justify-content-between">
                            <span>Power: <strong>${power} W</strong></span>
                            <span>Voltage: <strong>${voltage} V</strong></span>
                            <span>Current: <strong>${current} A</strong></span>
                        </div>`;
                    }

                    if (hasMultiState) {
                        // Show multistate/action values if present
                        const multiStateKeys = Object.keys(s).filter(k =>
                            (k.startsWith('multistate_') || k.includes('action') || k.includes('operation')) &&
                            (k.includes(`_${epId}`) || (epId === 1 && !k.match(/_\d+$/)))
                        );
                        if (multiStateKeys.length > 0) {
                            html += `<div class="small text-muted mb-2 mt-2"><i class="fas fa-info-circle"></i> Actions/State</div>`;
                            multiStateKeys.forEach(k => {
                                const displayKey = k.replace(`_${epId}`, '').replace(/_/g, ' ');
                                html += `<span class="badge bg-info text-dark me-1 mb-1">${displayKey}: ${s[k]}</span>`;
                            });
                        }
                    }

                    html += `</div>`;
                }

                html += `</div></div></div>`;
            }
        });
    }

    // --- Show Button/Remote Actions ---
    if (device.capabilities && Array.isArray(device.capabilities)) {
        const sensorEndpoints = device.capabilities.filter(ep => ep.component_type === "sensor");

        sensorEndpoints.forEach(ep => {
            const epId = ep.id;

            // Skip if has OnOff in INPUTS (that's a switch, not a button)
            const hasOnOffInput = (ep.inputs || []).some(c => c.id === 0x0006);
            if (hasOnOffInput) {
                return;
            }

            const hasMultiState = (ep.inputs || []).concat(ep.outputs || []).some(c =>
                [0x0012, 0x0013, 0x0014].includes(c.id)
            );

            // Skip passive sensors (IAS, Occupancy without multistate)
            const hasIAS = (ep.inputs || []).some(c => c.id === 0x0500);
            const hasOccupancy = (ep.inputs || []).some(c => c.id === 0x0406);
            if ((hasIAS || hasOccupancy) && !hasMultiState) {
                return;
            }

            // Show button/remote action info
            if (hasMultiState) {
                const actionKeys = Object.keys(s).filter(k =>
                    (k.startsWith('multistate_') || k.includes('action') || k.includes('click') || k.includes('button')) &&
                    (k.includes(`_${epId}`) || (epId === 1 && !k.match(/_\d+$/)))
                );

                if (actionKeys.length > 0) {
                    controlsFound = true;
                    html += `
                    <div class="col-12 col-md-6 mb-3">
                        <div class="card h-100">
                            <div class="card-header bg-light">
                                <strong><i class="fas fa-hand-pointer text-primary"></i> Button/Remote (EP${epId})</strong>
                            </div>
                            <div class="card-body">
                                <div class="small text-muted mb-2"><i class="fas fa-info-circle"></i> Last Actions</div>`;

                    actionKeys.forEach(k => {
                        const displayKey = k.replace(`_${epId}`, '').replace(/_/g, ' ');
                        const val = s[k];
                        html += `<div class="mb-2">
                            <span class="badge bg-primary me-2">${displayKey}</span>
                            <span class="badge bg-light text-dark">${val}</span>
                        </div>`;
                    });

                    html += `
                            </div>
                        </div>
                    </div>`;
                }
            }
        });
    }

    // --- Sensor Display (Contact, Motion, IAS Zone) ---
    const capList = device.capability_list || [];
    const isContactSensor = capList.includes('contact_sensor');
    const isMotionSensor = capList.includes('motion_sensor') || capList.includes('occupancy_sensing');
    const isIASZone = capList.includes('ias_zone');

    if (isContactSensor) {
        controlsFound = true;
        const contact = s.contact;
        const isOpen = s.is_open;
        const isClosed = contact === true || isOpen === false;
        const statusText = isClosed ? 'CLOSED' : 'OPEN';
        const statusClass = isClosed ? 'bg-success' : 'bg-danger';
        const icon = isClosed ? 'fa-door-closed' : 'fa-door-open';

        html += `
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-light d-flex justify-content-between align-items-center">
                        <strong><i class="fas ${icon}"></i> Contact Sensor</strong>
                        <span class="badge ${statusClass}">${statusText}</span>
                    </div>
                    <div class="card-body text-center">
                        <i class="fas ${icon} fa-3x mb-2 ${isClosed ? 'text-success' : 'text-danger'}"></i>
                        <p class="mb-0 fw-bold">${statusText}</p>
                    </div>
                </div>
            </div>`;
    }

    if (isMotionSensor && !isContactSensor) {
        controlsFound = true;
        const occupied = s.occupancy === true || s.motion === true || s.presence === true;
        const statusText = occupied ? 'MOTION' : 'CLEAR';
        const statusClass = occupied ? 'bg-danger' : 'bg-success';
        const icon = occupied ? 'fa-running' : 'fa-shield-alt';

        html += `
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-light d-flex justify-content-between align-items-center">
                        <strong><i class="fas fa-eye"></i> Motion Sensor</strong>
                        <span class="badge ${statusClass}">${statusText}</span>
                    </div>
                    <div class="card-body text-center">
                        <i class="fas ${icon} fa-3x mb-2 ${occupied ? 'text-danger' : 'text-success'}"></i>
                        <p class="mb-0 fw-bold">${statusText}</p>
                    </div>
                </div>
            </div>`;
    }

    if (isIASZone && !isContactSensor && !isMotionSensor) {
        controlsFound = true;
        const zoneStatus = s.zone_status || 0;
        const alarm = s.alarm || s.water_leak || s.smoke || s.vibration || (zoneStatus & 1);
        const statusText = alarm ? 'ALARM' : 'OK';
        const statusClass = alarm ? 'bg-danger' : 'bg-success';
        const icon = alarm ? 'fa-exclamation-triangle' : 'fa-check-circle';

        html += `
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-light d-flex justify-content-between align-items-center">
                        <strong><i class="fas fa-shield-alt"></i> Zone Sensor</strong>
                        <span class="badge ${statusClass}">${statusText}</span>
                    </div>
                    <div class="card-body text-center">
                        <i class="fas ${icon} fa-3x mb-2 ${alarm ? 'text-danger' : 'text-success'}"></i>
                        <p class="mb-0 fw-bold">${statusText}</p>
                    </div>
                </div>
            </div>`;
    }

    if (!controlsFound) {
        if (s.state !== undefined || s.on !== undefined) {
             html += `
                <div class="col-12"><div class="card"><div class="card-body">
                    <h6>Legacy Power Control</h6>
                    <button class="btn btn-success" onclick="window.sendCommand('${device.ieee}', 'on')">On</button>
                    <button class="btn btn-secondary" onclick="window.sendCommand('${device.ieee}', 'off')">Off</button>
                </div></div></div>
             `;
        } else {
            html += `<div class="col-12"><div class="alert alert-info">No interactive controls found for this device.</div></div>`;
        }
    }
    html += '</div>';
    return html;
}

window.adjustThermostat = function(ieee, delta) {
    const input = document.getElementById(`thermostat-setpoint-${ieee}`);
    if (input) {
        const currentVal = parseFloat(input.value) || 20;
        const newVal = currentVal + delta;
        input.value = Math.max(5, Math.min(35, newVal)).toFixed(1);
    }
};

window.setThermostatTemp = async function(ieee) {
    const input = document.getElementById(`thermostat-setpoint-${ieee}`);
    if (!input) {
        console.error('Thermostat input not found');
        return;
    }
    const temp = parseFloat(input.value);
    if (isNaN(temp) || temp < 5 || temp > 35) {
        alert('Invalid temperature. Must be between 5°C and 35°C');
        return;
    }
    try {
        await window.sendCommand(ieee, 'temperature', temp);
        // Optimistic update — don't wait for WS round-trip
        const setpointEl = document.querySelector(`[data-thermostat-setpoint="${ieee}"]`);
        if (setpointEl) setpointEl.textContent = `${temp.toFixed(1)}°C`;
        console.log(`✓ Temperature set to ${temp}°C`);
    } catch (error) {
        console.error('Failed to set temperature:', error);
        alert('Failed to set temperature: ' + error.message);
    }
};

window.setHvacMode = async function(ieee, mode) {
    try {
        await window.sendCommand(ieee, 'system_mode', mode);
        console.log(`✓ HVAC mode set to ${mode}`);
    } catch (error) {
        console.error('Failed to set HVAC mode:', error);
        alert('Failed to set HVAC mode: ' + error.message);
    }
};

window.showColorMode = function(ieee, epId, mode) {
    const tempPanel = document.getElementById(`colorTempPanel_${epId}`);
    const colorPanel = document.getElementById(`colorPickerPanel_${epId}`);
    if (mode === 'temp') {
        if (tempPanel) tempPanel.style.display = '';
        if (colorPanel) colorPanel.style.display = 'none';
    } else {
        if (tempPanel) tempPanel.style.display = 'none';
        if (colorPanel) colorPanel.style.display = '';
    }
};

// ============================================================================
// AQARA TRV COMMAND HANDLERS
// ----------------------------------------------------------------------------
// When the device is managed by the heating controller, persistent settings
// (window/child_lock/valve detection, calibrate) are routed through the
// controller API so config.yaml stays in sync with the device. Otherwise they
// go through the standard /api/device/command path.
// ============================================================================

window.aqaraSetFeature = async function(ieee, feature, enabled) {
    const managed = isHeatingManaged(ieee);
    const ALLOWED_MANAGED = ['window_detection', 'child_lock', 'valve_detection'];
    try {
        if (managed && ALLOWED_MANAGED.includes(feature)) {
            const body = { ieee, [feature]: !!enabled };
            const res = await fetch('/api/heating/controller/trv/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await res.json();
            if (!data.success) {
                alert('Failed to update ' + feature + ': ' + (data.error || 'unknown'));
            }
        } else {
            await window.sendCommand(ieee, feature, enabled ? 1 : 0);
        }
    } catch (e) {
        console.error('aqaraSetFeature failed:', e);
        alert('Update failed: ' + (e.message || e));
    }
};

window.aqaraCalibrate = async function(ieee) {
    if (!confirm('Start motor calibration?\n\nThe valve will sweep through its full range — this takes roughly 2 minutes and the TRV may be noisy during that time.')) return;
    const managed = isHeatingManaged(ieee);
    try {
        if (managed) {
            const res = await fetch('/api/heating/controller/trv/calibrate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ieee })
            });
            const data = await res.json();
            if (!data.success) alert('Calibration failed: ' + (data.error || 'unknown'));
        } else {
            await window.sendCommand(ieee, 'motor_calibration', 1);
        }
    } catch (e) {
        console.error('aqaraCalibrate failed:', e);
        alert('Calibration failed: ' + (e.message || e));
    }
};

window.aqaraSetSensorType = async function(ieee, type) {
    try {
        const val = (type === 'external') ? 1 : 0;
        await window.sendCommand(ieee, 'sensor_type', val);
    } catch (e) {
        console.error('aqaraSetSensorType failed:', e);
        alert('Sensor-type change failed: ' + (e.message || e));
    }
};

window.aqaraSourceChanged = function(ieee, sourceIeee) {
    const input = document.getElementById(`aq-ext-${ieee}`);
    if (!input) return;
    if (!sourceIeee) return;  // Manual entry chosen — leave the field alone
    const cache = state.deviceCache || {};
    const src = cache[sourceIeee];
    if (!src || !src.state) return;
    const s = src.state;
    let temp = null;
    for (const k of ['temperature', 'local_temperature', 'current_temperature', 'internal_temperature']) {
        const v = s[k];
        const f = (v == null) ? NaN : Number(v);
        if (!isNaN(f) && f !== 0 && f > -40 && f < 80) { temp = f; break; }
    }
    if (temp == null) return;
    input.value = temp.toFixed(1);
};

window.aqaraPushExternalTemp = async function(ieee) {
    const input = document.getElementById(`aq-ext-${ieee}`);
    const srcSel = document.getElementById(`aq-src-${ieee}`);
    const sensorSel = document.getElementById(`aq-st-${ieee}`);
    if (!input) return;

    // If a source is picked, use that source's *current* temperature (freshest)
    let val;
    const sourceIeee = srcSel ? srcSel.value : '';
    if (sourceIeee) {
        const src = (state.deviceCache || {})[sourceIeee];
        const s = (src && src.state) || {};
        for (const k of ['temperature', 'local_temperature', 'current_temperature', 'internal_temperature']) {
            const v = s[k];
            const f = (v == null) ? NaN : Number(v);
            if (!isNaN(f) && f !== 0 && f > -40 && f < 80) { val = f; break; }
        }
        if (val == null) {
            alert('Selected source has no current temperature reading. Try again in a moment.');
            return;
        }
        input.value = val.toFixed(1);
    } else {
        val = parseFloat(input.value);
    }
    if (isNaN(val) || val < -40 || val > 80) {
        alert('Enter a valid temperature between -40 and 80 °C');
        return;
    }

    try {
        // Ensure sensor_type=external first — otherwise the TRV ignores the push
        if (sensorSel && sensorSel.value !== 'external') {
            await window.sendCommand(ieee, 'sensor_type', 1);
            sensorSel.value = 'external';
        }
        await window.sendCommand(ieee, 'external_temp', val);
    } catch (e) {
        console.error('aqaraPushExternalTemp failed:', e);
        alert('Push external temp failed: ' + (e.message || e));
    }
};