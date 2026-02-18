/**
 * Device Control Tab
 * Location: static/js/modal/control.js
 */

import { state } from '../state.js';
import { hasCluster } from './config.js';

// Interaction debounce timer
let interactionTimeout = null;
const INTERACTION_DEBOUNCE_MS = 500;

/**
 * Mark control interaction as active and set debounced clear
 */
function setInteractionActive() {
    state.controlInteractionActive = true;
    if (interactionTimeout) clearTimeout(interactionTimeout);
    interactionTimeout = setTimeout(() => {
        state.controlInteractionActive = false;
    }, INTERACTION_DEBOUNCE_MS);
}

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

    // Update ON/OFF badges and controls for each endpoint
    if (device.capabilities && Array.isArray(device.capabilities)) {
        device.capabilities.forEach(ep => {
            const epId = ep.id;
            const isOn = s[`on_${epId}`] !== undefined ? s[`on_${epId}`] : (epId === 1 ? s.on : false);

            // Update ON/OFF badge
            const badge = document.querySelector(`[data-ep-badge="${epId}"]`);
            if (badge) {
                badge.className = isOn ? 'badge bg-success' : 'badge bg-secondary';
                badge.textContent = isOn ? 'ON' : 'OFF';
            }

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
        const piDemand = s.pi_heating_demand || 0;
        const battery = s.battery || 0;

        const modeMap = {
            0: 'Off', 1: 'Auto', 3: 'Cool', 4: 'Heat',
            'off': 'Off', 'auto': 'Auto', 'cool': 'Cool', 'heat': 'Heat'
        };
        const currentModeName = modeMap[systemMode] || systemMode;
        const isHeating = (runningState & 0x0001) || (String(runningState).includes("heat"));

        html += `
        <div class="col-12">
            <div class="card">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-thermometer-half"></i> Thermostat</strong>
                    <div>
                        ${isHeating
                            ? '<span class="badge bg-danger"><i class="fas fa-fire"></i> Heating</span>'
                            : '<span class="badge bg-secondary"><i class="fas fa-pause"></i> Idle</span>'}
                        ${battery > 0 && battery < 20
                            ? `<span class="badge bg-warning text-dark ms-1"><i class="fas fa-battery-quarter"></i> ${battery}%</span>`
                            : ''}
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
                                <h2 class="mb-0 text-primary">${targetTemp}°C</h2>
                            </div>
                        </div>
                        <div class="col-12">
                            <label class="form-label fw-bold"><i class="fas fa-cog"></i> Mode</label>
                            <select id="hvac-mode-${device.ieee}" class="form-select"
                                    onchange="window.setHvacMode('${device.ieee}', this.value)">
                                <option value="off" ${String(systemMode).toLowerCase() === 'off' ? 'selected' : ''}>Off</option>
                                <option value="heat" ${String(systemMode).toLowerCase() === 'heat' ? 'selected' : ''}>Heat</option>
                                <option value="auto" ${String(systemMode).toLowerCase() === 'auto' ? 'selected' : ''}>Auto</option>
                            </select>
                        </div>
                        <div class="col-12">
                            <label class="form-label fw-bold"><i class="fas fa-sliders-h"></i> Set Target</label>
                            <div class="input-group">
                                <button class="btn btn-outline-secondary" onclick="window.adjustThermostat('${device.ieee}', -0.5)">−</button>
                                <input type="number" id="thermostat-setpoint-${device.ieee}" class="form-control text-center"
                                       value="${targetTemp}" step="0.5" min="5" max="35">
                                <button class="btn btn-outline-secondary" onclick="window.adjustThermostat('${device.ieee}', 0.5)">+</button>
                                <button class="btn btn-primary" onclick="window.setThermostatTemp('${device.ieee}')">Set</button>
                            </div>
                        </div>
                        ${piDemand > 0 ? `
                        <div class="col-12">
                            <label class="form-label small text-muted">Heat Demand: ${piDemand}%</label>
                            <div class="progress" style="height: 8px;">
                                <div class="progress-bar bg-danger" style="width: ${piDemand}%"></div>
                            </div>
                        </div>` : ''}
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
            if (ep.component_type === "sensor") {
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

window.uploadSimpleSchedule = async function(ieee) {
    if (!confirm("This will overwrite the device's internal schedule for ALL days. Continue?")) return;

    // Define a standard "Work Day" schedule
    // Time is minutes from midnight (e.g., 6:00 = 6*60 = 360)
    const payload = {
        command: "set_schedule",
        value: {
            day_of_week: 255, // 255 = Apply to All Days (check device specific bitmask)
            transitions: [
                { time: 360, heat: 20.0 }, // 06:00 AM
                { time: 540, heat: 18.0 }, // 09:00 AM (Leave for work)
                { time: 1020, heat: 21.0 }, // 17:00 PM (Return home)
                { time: 1320, heat: 16.0 }  // 22:00 PM (Sleep)
            ]
        }
    };

    try {
        await fetch(`/api/device/${ieee}/command`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        alert('Schedule command sent!');
    } catch (error) {
        console.error('Error:', error);
        alert('Failed: ' + error.message);
    }
};

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