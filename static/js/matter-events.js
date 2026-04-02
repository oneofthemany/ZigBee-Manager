/**
 * matter-events.js — Live event stream for all Matter devices
 */

export function renderMatterEventsTab(device) {
    if (device.protocol !== 'matter') return '';
    const s = device.state || {};

    const lastAction = s.last_action || 'none';
    const lastTime = s.last_action_time
        ? new Date(s.last_action_time * 1000).toLocaleTimeString()
        : '—';
    const lastEp = s.last_action_endpoint || '—';

    // ── Action display (universal for all device types) ────────
    let actionHtml = `
        <div class="d-flex align-items-center gap-3 mb-3 p-2 bg-light rounded">
            <div>
                <span class="small text-muted">Last Action:</span>
                <span class="badge bg-primary ms-1" id="matterLastAction">${lastAction}</span>
            </div>
            <div>
                <span class="small text-muted">Endpoint:</span>
                <span class="badge bg-secondary ms-1" id="matterLastEp">${lastEp}</span>
            </div>
            <div>
                <span class="small text-muted">Time:</span>
                <span class="small ms-1" id="matterLastActionTime">${lastTime}</span>
            </div>
        </div>
    `;

    // ── Device-specific visuals ────────────────────────────────
    let deviceSpecificHtml = '';

    // Buttons / Switches / Remotes / Dials
    if (s.switch_endpoints && Array.isArray(s.switch_endpoints)) {
        deviceSpecificHtml += `
            <h6 class="small fw-bold text-muted mb-2">
                <i class="fas fa-hand-pointer me-1"></i>
                Buttons (${s.button_count || s.switch_endpoints.length})
            </h6>
            <div class="row g-2 mb-3">
                ${s.switch_endpoints.map(ep => {
                    const isRotary = ep.positions > 2 || ep.multi_press_max > 4;
                    const icon = isRotary ? 'fa-sync-alt' : 'fa-circle';
                    const label = isRotary ? 'Dial' : `Button ${ep.endpoint}`;
                    return `
                        <div class="col-md-3 col-sm-4 col-6">
                            <div class="card h-100 text-center">
                                <div class="card-body py-2 px-1">
                                    <i class="fas ${icon} fa-lg mb-1
                                       text-${ep.current_position > 0 ? 'primary' : 'secondary'}"></i>
                                    <div class="small fw-bold">${label}</div>
                                    <div class="text-muted" style="font-size:10px">
                                        EP${ep.endpoint} · ${ep.positions} pos
                                        ${ep.multi_press_max > 0 ? ` · ${ep.multi_press_max}x` : ''}
                                    </div>
                                </div>
                            </div>
                        </div>`;
                }).join('')}
            </div>`;
    }

    // Lock state
    if (s.locked !== undefined) {
        deviceSpecificHtml += `
            <div class="d-flex align-items-center gap-2 mb-3 p-2 bg-light rounded">
                <i class="fas fa-${s.locked ? 'lock' : 'lock-open'} fa-lg
                   text-${s.locked ? 'success' : 'danger'}"></i>
                <span class="fw-bold">${s.locked ? 'Locked' : 'Unlocked'}</span>
            </div>`;
    }

    // Contact sensor
    if (s.contact !== undefined) {
        deviceSpecificHtml += `
            <div class="d-flex align-items-center gap-2 mb-3 p-2 bg-light rounded">
                <i class="fas fa-${s.contact ? 'door-closed' : 'door-open'} fa-lg
                   text-${s.contact ? 'success' : 'warning'}"></i>
                <span class="fw-bold">${s.contact ? 'Closed' : 'Open'}</span>
            </div>`;
    }

    // Occupancy
    if (s.occupancy !== undefined) {
        deviceSpecificHtml += `
            <div class="d-flex align-items-center gap-2 mb-3 p-2 bg-light rounded">
                <i class="fas fa-${s.occupancy ? 'walking' : 'couch'} fa-lg
                   text-${s.occupancy ? 'success' : 'secondary'}"></i>
                <span class="fw-bold">${s.occupancy ? 'Occupied' : 'Clear'}</span>
            </div>`;
    }

    // Thermostat
    if (s.heating_setpoint !== undefined || s.cooling_setpoint !== undefined) {
        deviceSpecificHtml += `
            <div class="row g-2 mb-3">
                ${s.temperature !== undefined ? `
                <div class="col-md-4">
                    <div class="card text-center">
                        <div class="card-body py-2">
                            <div class="text-muted small">Current</div>
                            <div class="fw-bold fs-5">${s.temperature}°C</div>
                        </div>
                    </div>
                </div>` : ''}
                ${s.heating_setpoint !== undefined ? `
                <div class="col-md-4">
                    <div class="card text-center border-danger">
                        <div class="card-body py-2">
                            <div class="text-muted small">Heat</div>
                            <div class="fw-bold fs-5 text-danger">${s.heating_setpoint}°C</div>
                        </div>
                    </div>
                </div>` : ''}
                ${s.cooling_setpoint !== undefined ? `
                <div class="col-md-4">
                    <div class="card text-center border-primary">
                        <div class="card-body py-2">
                            <div class="text-muted small">Cool</div>
                            <div class="fw-bold fs-5 text-primary">${s.cooling_setpoint}°C</div>
                        </div>
                    </div>
                </div>` : ''}
            </div>`;
    }

    // Cover/Blind position
    if (s.position !== undefined) {
        deviceSpecificHtml += `
            <div class="mb-3">
                <div class="d-flex justify-content-between small mb-1">
                    <span>Position</span><span>${s.position}%</span>
                </div>
                <div class="progress" style="height:8px">
                    <div class="progress-bar" style="width:${s.position}%"></div>
                </div>
            </div>`;
    }

    // No specific visuals — show a generic info
    if (!deviceSpecificHtml) {
        deviceSpecificHtml = `
            <div class="text-muted small text-center py-2">
                <i class="fas fa-info-circle me-1"></i>
                Events from this device will appear here and in the live action badge above.
            </div>`;
    }

    // ── Event log (universal) ──────────────────────────────────
    return `
        <div class="mb-3">
            <h6 class="text-uppercase text-muted fw-bold small">
                <i class="fas fa-bolt me-1"></i> Device Events
            </h6>
            ${actionHtml}
            ${deviceSpecificHtml}
            <h6 class="small fw-bold text-muted mt-3 mb-2">
                <i class="fas fa-stream me-1"></i> Event Log
            </h6>
            <div id="matterEventLog" class="small"
                 style="max-height:200px; overflow-y:auto; font-family:monospace;
                        background:#f8f9fa; padding:8px; border-radius:4px;">
                <span class="text-muted">Waiting for events...</span>
            </div>
        </div>
    `;
}