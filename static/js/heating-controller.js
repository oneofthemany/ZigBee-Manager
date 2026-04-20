/**
 * heating-controller.js
 * Frontend for the active Heating Controller (circuits, rooms, TRV coordination).
 *
 * Two surfaces:
 *   1. Live status panel — added to the heating dashboard via renderControllerPanel()
 *   2. Settings modal — opened via openControllerSettings(); includes:
 *        - Enable/dry-run toggles
 *        - Circuit list with per-circuit room list with per-room TRV picker
 *
 * Endpoints:
 *   GET  /api/heating/controller/state
 *   POST /api/heating/controller/tick
 *   POST /api/heating/controller/dry-run
 *   GET  /api/heating/controller/config
 *   POST /api/heating/controller/config
 *   GET  /api/heating/controller/devices
 */

let controllerState = null;
let controllerConfig = null;
let controllerDevices = { receivers: [], thermostats: [] };
let controllerSensors = [];
let workingCircuits = [];
let controllerStatusTimer = null;

const STATUS_REFRESH_MS = 30_000;

// ============================================================================
// PUBLIC: initialize once
// ============================================================================
export function initHeatingController() {
    ensureControllerSettingsModal();
    console.log("Heating Controller frontend initialised");
}

// ============================================================================
// PUBLIC: status panel for the dashboard
// ============================================================================
export async function loadControllerStatus(targetSelector = '#heatingControllerPanel') {
    const container = document.querySelector(targetSelector);
    if (!container) return;

    try {
        const res = await fetch('/api/heating/controller/state');
        const json = await res.json();
        if (!json.success) {
            container.innerHTML = renderControllerDisabled(json.error);
            bindControllerPanel();
            return;
        }
        controllerState = json.state;
        container.innerHTML = renderControllerPanel(controllerState);
        bindControllerPanel();
        controllerState = json.state;
        container.innerHTML = renderControllerPanel(controllerState);
        bindControllerPanel();
        // Phase 5: populate per-room pre-heat hints asynchronously so the
        // panel renders immediately without blocking on these calculations.
        fillRoomPreheatSlots();
    } catch (err) {
        console.warn('Controller status fetch failed:', err);
        container.innerHTML = `<div class="alert alert-warning small">Controller status unavailable</div>`;
    }
}


// ============================================================================
// RENDER: live status panel
// ============================================================================
function renderControllerDisabled(reason) {
    return `
        <div class="card mb-3">
            <div class="card-header d-flex justify-content-between align-items-center">
                <span><i class="fas fa-cogs me-2"></i>Heating Controller</span>
                <button class="btn btn-sm btn-outline-primary" id="btn-controller-settings">
                    <i class="fas fa-cog me-1"></i> Configure
                </button>
            </div>
            <div class="card-body text-center text-muted py-4">
                <i class="fas fa-power-off fa-2x mb-2 opacity-50"></i>
                <div>${escapeHtml(reason || 'Controller not enabled')}</div>
                <small>Configure circuits and rooms, then enable the controller.</small>
            </div>
        </div>`;
}

function renderControllerPanel(state) {
    if (!state.enabled) {
        return renderControllerDisabled('Controller defined but not enabled');
    }

    const circuits = state.circuits || [];
    const dryBadge = state.dry_run
        ? `<span class="badge bg-warning text-dark"><i class="fas fa-flask me-1"></i>DRY RUN</span>`
        : `<span class="badge bg-success"><i class="fas fa-bolt me-1"></i>LIVE</span>`;
    const ageSec = state.last_tick_age_seconds;
    const ageStr = ageSec != null
        ? (ageSec < 90 ? `${Math.round(ageSec)}s ago` : `${Math.round(ageSec / 60)}m ago`)
        : 'never';

    const circuitCards = circuits.map(c => renderCircuitStatusCard(c)).join('');

    return `
        <div class="card mb-3">
            <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
                <span><i class="fas fa-cogs me-2"></i>Heating Controller ${dryBadge}</span>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-secondary" id="btn-controller-tick" title="Run a control tick now">
                        <i class="fas fa-bolt"></i> Tick now
                    </button>
                    <button class="btn btn-outline-primary" id="btn-controller-settings" title="Configure circuits and rooms">
                        <i class="fas fa-cog"></i> Configure
                    </button>
                </div>
            </div>
            <div class="card-body">
                <div class="small text-muted mb-2">Last tick: ${ageStr}</div>
                ${circuits.length === 0
                    ? `<div class="text-center text-muted py-3">No circuits configured. Click <strong>Configure</strong> to start.</div>`
                    : `<div class="row g-3">${circuitCards}</div>`}
            </div>
        </div>`;
}

function renderCircuitStatusCard(c) {
    // Split "intent" (controller wants heat) from "reality" (receiver is
    // actually firing). Hive SLR commonly sits at system_mode=heat with
    // running_state=0 when the internal comparator isn't calling yet.
    const receiverRunning = c.receiver_state?.running === true;
    const callBadge = receiverRunning
        ? `<span class="badge bg-danger"><i class="fas fa-fire me-1"></i>Heating</span>`
        : (c.calling_for_heat
            ? `<span class="badge bg-warning text-dark" title="Controller is calling for heat but the receiver has not yet responded"><i class="fas fa-hourglass-half me-1"></i>Calling (waiting)</span>`
            : `<span class="badge bg-secondary">Idle</span>`);
    const recvAction = c.receiver_action || {};
    const recvState = c.receiver_state || {};
    const runningBadge = recvState.running
        ? `<span class="badge bg-danger ms-1" title="Boiler is firing right now"><i class="fas fa-fire me-1"></i>running</span>`
        : (recvState.running === false
            ? `<span class="badge bg-secondary ms-1" title="Receiver is idle">idle</span>`
            : '');
    const modeBadge = recvState.system_mode
        ? `<span class="badge bg-light text-dark border ms-1">${escapeHtml(String(recvState.system_mode))}</span>`
        : '';
    const spSuffix = (recvState.setpoint != null)
        ? ` @ <strong>${Number(recvState.setpoint).toFixed(1)}°</strong>`
        : '';
    const recvLine = c.receiver_ieee
        ? `Receiver: <code>${escapeHtml(c.receiver_ieee.slice(-8))}</code>${spSuffix}${runningBadge}${modeBadge}${recvAction.command ? ` → <strong>${escapeHtml(recvAction.command)}</strong>` : ''}${recvAction.dry_run ? ' <em>(dry-run)</em>' : ''}`
        : `<span class="text-warning">No receiver assigned</span>`;

    const rooms = (c.rooms || []).map(r => {
        const statusMeta = {
            cold: { c: '#fd7e14', icon: 'snowflake', label: 'Cold' },
            ontarget: { c: '#198754', icon: 'check', label: 'On target' },
            hot: { c: '#dc3545', icon: 'temperature-high', label: 'Hot' },
            unknown: { c: '#6c757d', icon: 'question', label: 'Unknown' },
        }[r.status] || { c: '#6c757d', icon: 'question', label: 'Unknown' };

        // Pre-heat hint: only show for rooms currently below target
        const preheatSlotId = `preheat-${escapeAttr(c.id)}-${escapeAttr(r.room_id)}`;
        const preheatSlot = (r.status === 'cold')
            ? `<div class="small ms-3 mt-1" id="${preheatSlotId}"
                    data-circuit-id="${escapeAttr(c.id)}" data-room-id="${escapeAttr(r.room_id)}">
                   <i class="fas fa-hourglass-half text-muted me-1"></i>
                   <span class="text-muted">calculating pre-heat…</span>
               </div>`
            : '';

        const trvLines = (r.trvs || []).map(t => {
            const offline = !t.online ? ` <small class="text-danger">offline</small>` : '';
            const sp = t.intended_setpoint != null ? ` → <strong>${t.intended_setpoint}°</strong>` : '';
            const actionTag = t.action === 'force_close'
                ? ` <span class="badge bg-warning text-dark" style="font-size:0.65rem;">force-close</span>`
                : '';
            const cur = t.current_temp != null ? `${t.current_temp.toFixed(1)}°` : '—';
            return `<div class="small">
                <i class="fas fa-thermometer-half text-muted me-1"></i>
                ${escapeHtml(t.name || t.ieee.slice(-8))}: ${cur}${sp}${actionTag}${offline}
            </div>`;
        }).join('');

        return `
            <div class="border-start border-3 ps-2 mb-2" style="border-color:${statusMeta.c} !important;">
                <div class="d-flex justify-content-between align-items-baseline">
                    <strong>${escapeHtml(r.name)}</strong>
                    <span style="color:${statusMeta.c}; font-size:0.85rem;">
                        <i class="fas fa-${statusMeta.icon} me-1"></i>${r.current_temp != null ? r.current_temp.toFixed(1) : '—'}° / ${r.target_temp != null ? r.target_temp.toFixed(1) : '—'}°
                    </span>
                </div>
                ${trvLines || `<div class="small text-muted fst-italic">
                    <i class="fas fa-broadcast-tower me-1"></i>Sensor-only room — radiator runs on circuit flow
                </div>`}
                ${preheatSlot}
            </div>`;
    }).join('');

    return `
        <div class="col-md-6">
            <div class="card h-100">
                <div class="card-body">
                    <div class="d-flex justify-content-between align-items-start mb-2">
                        <h6 class="mb-0"><i class="fas fa-stream me-1"></i>${escapeHtml(c.name)}</h6>
                        ${callBadge}
                    </div>
                    <div class="small text-muted mb-2">${recvLine}</div>
                    ${rooms || '<div class="text-muted small">No rooms configured</div>'}
                </div>
            </div>
        </div>`;
}

function bindControllerPanel() {
    document.getElementById('btn-controller-settings')?.addEventListener('click', openControllerSettings);
    document.getElementById('btn-controller-tick')?.addEventListener('click', async () => {
        const btn = document.getElementById('btn-controller-tick');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Ticking…`;
        try {
            const res = await fetch('/api/heating/controller/tick', { method: 'POST' });
            const json = await res.json();
            if (json.success) {
                if (typeof window.showToast === 'function') {
                    window.showToast('Controller tick complete', 'success');
                }
                loadControllerStatus();
            } else {
                alert(`Tick failed: ${json.error}`);
            }
        } catch (e) {
            alert(`Tick failed: ${e.message}`);
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });
}

// ============================================================================
// SETTINGS MODAL
// ============================================================================
function ensureControllerSettingsModal() {
    if (document.getElementById('controllerSettingsModal')) return;
    const html = `
        <div class="modal fade" id="controllerSettingsModal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title"><i class="fas fa-cogs me-2"></i>Heating Controller — Circuits & Rooms</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body" id="controllerSettingsBody">
                        <div class="text-center text-muted py-5">
                            <div class="spinner-border spinner-border-sm me-2"></div>Loading…
                        </div>
                    </div>
                    <div class="modal-footer">
                        <div id="controllerSettingsStatus" class="me-auto small text-muted"></div>
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary" id="btnControllerSave">
                            <i class="fas fa-save me-1"></i> Save
                        </button>
                    </div>
                </div>
            </div>
        </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
    document.getElementById('btnControllerSave').addEventListener('click', saveControllerSettings);
}

export async function openControllerSettings() {
    const modalEl = document.getElementById('controllerSettingsModal');
    const bodyEl = document.getElementById('controllerSettingsBody');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    bodyEl.innerHTML = `<div class="text-center text-muted py-5">
        <div class="spinner-border spinner-border-sm me-2"></div>Loading…</div>`;
    modal.show();

    try {
        const [cfgRes, devRes, sensorRes] = await Promise.all([
            fetch('/api/heating/controller/config').then(r => r.json()),
            fetch('/api/heating/controller/devices').then(r => r.json()).catch(() => ({ success: false })),
            fetch('/api/heating/controller/sensors').then(r => r.json()).catch(() => ({ success: false })),
        ]);
        if (!cfgRes.success) throw new Error(cfgRes.error || 'Config load failed');

        controllerConfig = cfgRes.config;
        controllerDevices = devRes.success
            ? { receivers: devRes.receivers || [], thermostats: devRes.thermostats || [] }
            : { receivers: [], thermostats: [] };
        controllerSensors = (sensorRes.success ? (sensorRes.sensors || []) : []);
        workingCircuits = JSON.parse(JSON.stringify(controllerConfig.circuits || []));
        // Normalise: the backend emits `trvs: [{ieee, ...}]` but this frontend
        // historically read/wrote `trv_ieees: [...]`. Derive/keep both in sync
        // so either source of the saved config lights up the checkboxes.
        for (const c of workingCircuits) {
            for (const r of (c.rooms || [])) {
                const fromTrvs = Array.isArray(r.trvs)
                    ? r.trvs.map(t => t && t.ieee).filter(Boolean)
                    : [];
                const fromLegacy = Array.isArray(r.trv_ieees) ? r.trv_ieees : [];
                // Union (preserve any extras that might only exist in legacy form)
                const merged = Array.from(new Set([...fromTrvs, ...fromLegacy]));
                r.trv_ieees = merged;
                // Keep trvs list shape — used by backend and persists per-TRV
                // settings (window_detection, child_lock, valve_detection)
                if (!Array.isArray(r.trvs)) r.trvs = [];
                const existingIeees = new Set(r.trvs.map(t => t.ieee));
                for (const ieee of merged) {
                    if (!existingIeees.has(ieee)) r.trvs.push({ ieee });
                }
                // Drop any trv entries that no longer appear in merged
                r.trvs = r.trvs.filter(t => merged.includes(t.ieee));
            }
        }

        bodyEl.innerHTML = renderControllerForm(controllerConfig);
        bindControllerForm();
    } catch (err) {
        bodyEl.innerHTML = `<div class="alert alert-danger">Failed to load: ${escapeHtml(err.message)}</div>`;
    }
}

function renderControllerForm(cfg) {
    return `
        <div class="alert alert-info small mb-3">
            <i class="fas fa-info-circle me-1"></i>
            <strong>Circuits</strong> are receivers/zone valves that call for boiler heat.
            Each circuit contains <strong>rooms</strong>, and each room has one or more <strong>TRVs</strong>.
            The controller fires the receiver when any room is below its target, and force-closes
            TRVs of hot rooms to prevent demand stealing.
        </div>

        <div class="d-flex gap-3 mb-3 flex-wrap">
            <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" id="ctrlEnabled" ${cfg.enabled ? 'checked' : ''}>
                <label class="form-check-label" for="ctrlEnabled"><strong>Enable controller</strong></label>
            </div>
            <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" id="ctrlDryRun" ${cfg.dry_run ? 'checked' : ''}>
                <label class="form-check-label" for="ctrlDryRun">
                    Dry-run <small class="text-muted">(log only, no commands sent)</small>
                </label>
            </div>
        </div>

        <div class="d-flex justify-content-between align-items-center mb-3">
            <strong>Circuits</strong>
            <button class="btn btn-sm btn-primary" id="btnAddCircuit">
                <i class="fas fa-plus me-1"></i> Add circuit
            </button>
        </div>
        <div id="circuitsList"></div>`;
}

function bindControllerForm() {
    document.getElementById('btnAddCircuit')?.addEventListener('click', () => {
        const n = workingCircuits.length + 1;
        workingCircuits.push({
            id: `circuit_${n}`,
            name: `Circuit ${n}`,
            receiver_ieee: null,
            receiver_command: 'thermostat',
            rooms: [],
        });
        renderCircuitsList();
    });
    renderCircuitsList();
}

function renderCircuitsList() {
    const container = document.getElementById('circuitsList');
    if (!container) return;
    if (!workingCircuits.length) {
        container.innerHTML = `<div class="text-center text-muted py-4 border rounded">
            No circuits yet. Click <strong>Add circuit</strong> to start.
        </div>`;
        return;
    }
    container.innerHTML = workingCircuits.map((c, i) => renderCircuitCard(c, i)).join('');
    bindCircuitCards();
}

function renderCircuitCard(circuit, ci) {
    const usedRecv = new Set(workingCircuits
        .map((c, i) => i === ci ? null : c.receiver_ieee)
        .filter(Boolean));
    const recvOptions = controllerDevices.receivers
        .filter(r => !usedRecv.has(r.ieee) || r.ieee === circuit.receiver_ieee)
        .map(r => {
            const modeStr = r.system_mode ? ` [${r.system_mode}]` : '';
            return `<option value="${escapeAttr(r.ieee)}" ${r.ieee === circuit.receiver_ieee ? 'selected' : ''}>
                ${escapeHtml(r.name)}${modeStr} (${escapeHtml(r.ieee.slice(-8))})
            </option>`;
        }).join('');

    const roomsHtml = (circuit.rooms || []).length
        ? circuit.rooms.map((r, ri) => renderRoomCard(r, ci, ri)).join('')
        : `<div class="text-muted small text-center py-2">No rooms in this circuit yet.</div>`;

    return `
        <div class="card mb-3 controller-circuit-card" data-ci="${ci}">
            <div class="card-header d-flex justify-content-between align-items-center bg-light">
                <div class="d-flex align-items-center gap-2 flex-grow-1">
                    <i class="fas fa-stream text-primary"></i>
                    <input type="text" class="form-control form-control-sm circuit-name" data-ci="${ci}"
                           value="${escapeAttr(circuit.name)}" placeholder="Circuit name" style="max-width:240px;">
                    <small class="text-muted">id: ${escapeHtml(circuit.id)}</small>
                </div>
                <button class="btn btn-sm btn-outline-danger btn-delete-circuit" data-ci="${ci}">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
            <div class="card-body">
                <div class="row g-2 mb-3">
                    <div class="col-md-7">
                        <label class="form-label small mb-1">Receiver / zone valve</label>
                        <select class="form-select form-select-sm circuit-receiver" data-ci="${ci}">
                            <option value="">— No receiver assigned —</option>
                            ${recvOptions}
                        </select>
                    </div>
                    <div class="col-md-5 d-flex align-items-end">
                        <small class="text-muted"><i class="fas fa-info-circle me-1"></i>Controls via system_mode (heat/off)</small>
                    </div>
                </div>

                <div class="d-flex justify-content-between align-items-center mb-2">
                    <strong class="small">Rooms in this circuit</strong>
                    <button class="btn btn-sm btn-outline-primary btn-add-room" data-ci="${ci}">
                        <i class="fas fa-plus me-1"></i> Add room
                    </button>
                </div>
                ${roomsHtml}
            </div>
        </div>`;
}

function renderRoomCard(room, ci, ri) {
    const usedTrvs = new Set();
    workingCircuits.forEach((c, ci2) => {
        c.rooms.forEach((r, ri2) => {
            if (ci2 === ci && ri2 === ri) return;
            (r.trv_ieees || []).forEach(t => usedTrvs.add(t));
        });
    });

    const availableTrvs = controllerDevices.thermostats.filter(t => !usedTrvs.has(t.ieee));
    const trvCheckboxes = availableTrvs.length
        ? availableTrvs.map(t => {
            const checked = (room.trv_ieees || []).includes(t.ieee) ? 'checked' : '';
            const tempStr = t.temperature != null ? ` <small class="text-muted">(${Number(t.temperature).toFixed(1)}°C)</small>` : '';
            return `
                <label class="list-group-item small d-flex align-items-center py-1">
                    <input class="form-check-input me-2 room-trv-cb" type="checkbox"
                           data-ci="${ci}" data-ri="${ri}" data-ieee="${escapeAttr(t.ieee)}" ${checked}>
                    <div class="flex-grow-1">
                        <div>${escapeHtml(t.name)}${tempStr}</div>
                        <small class="text-muted">${escapeHtml(t.ieee)}</small>
                    </div>
                </label>`;
        }).join('')
        : `<div class="list-group-item small text-muted">No available TRVs.</div>`;

    // Room temperature sensor dropdown — any device reporting a temperature
    // (motion sensors, THP, contact sensors with temp, etc.)
    const sensorIeee = room.temperature_sensor_ieee || '';
    const sensorOptions = [
        `<option value="">— None (use TRV readings) —</option>`,
        ...controllerSensors
            .filter(s => s.ieee !== sensorIeee)   // selected one added below
            .map(s => {
                const kindLabel = s.is_thermostat ? ' · thermostat' : '';
                return `<option value="${escapeAttr(s.ieee)}">
                    ${escapeHtml(s.name)} (${Number(s.temperature).toFixed(1)}°C${kindLabel})
                </option>`;
            }),
    ];
    // Make sure currently-selected sensor is present in the list even if
    // it's temporarily unavailable/offline
    if (sensorIeee && !controllerSensors.some(s => s.ieee === sensorIeee)) {
        sensorOptions.push(`<option value="${escapeAttr(sensorIeee)}" selected>
            ${escapeHtml(sensorIeee)} (offline)
        </option>`);
    } else if (sensorIeee) {
        const sel = controllerSensors.find(s => s.ieee === sensorIeee);
        if (sel) {
            sensorOptions.splice(1, 0, `<option value="${escapeAttr(sel.ieee)}" selected>
                ${escapeHtml(sel.name)} (${Number(sel.temperature).toFixed(1)}°C)
            </option>`);
        }
    }

    const extMode = room.external_temp_mode || (sensorIeee ? 'advisory' : 'off');
    const trvCount = (room.trv_ieees || []).length;
    const sensorOnlyBanner = trvCount === 0 && sensorIeee ? `
        <div class="alert alert-info alert-sm py-1 px-2 small mb-2">
            <i class="fas fa-broadcast-tower me-1"></i>
            <strong>Sensor-only room</strong> — the radiator runs on circuit flow whenever
            any room in this circuit calls for heat. The sensor above drives this room's
            call-for-heat decision.
        </div>` : '';
    const noTrvsNoSensorWarning = trvCount === 0 && !sensorIeee ? `
        <div class="alert alert-warning alert-sm py-1 px-2 small mb-2">
            <i class="fas fa-exclamation-triangle me-1"></i>
            This room has no TRVs <strong>and</strong> no temperature sensor — it cannot
            call for heat. Add a TRV or a sensor, or remove the room.
        </div>` : '';

    return `
        <div class="card mb-2 ms-3" style="border-left: 3px solid var(--bs-info);">
            <div class="card-body py-2">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <div class="d-flex align-items-center gap-2 flex-grow-1">
                        <i class="fas fa-door-open text-info"></i>
                        <input type="text" class="form-control form-control-sm room-name"
                               data-ci="${ci}" data-ri="${ri}"
                               value="${escapeAttr(room.name)}" placeholder="Room name" style="max-width:200px;">
                        <small class="text-muted">id: ${escapeHtml(room.id)}</small>
                    </div>
                    <button class="btn btn-sm btn-outline-danger btn-delete-room"
                            data-ci="${ci}" data-ri="${ri}" title="Delete room">
                        <i class="fas fa-times"></i>
                    </button>
                </div>

                <div class="row g-2 mb-2">
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Target °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm room-target"
                               data-ci="${ci}" data-ri="${ri}" value="${room.target_temp}">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Setback °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm room-setback"
                               data-ci="${ci}" data-ri="${ri}" value="${room.night_setback}">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Min °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm room-min"
                               data-ci="${ci}" data-ri="${ri}" value="${room.min_temp}">
                    </div>
                </div>

                <div class="row g-2 mb-2">
                    <div class="col-md-8">
                        <label class="form-label small mb-1">
                            <i class="fas fa-thermometer-half me-1"></i>Room temperature sensor
                        </label>
                        <select class="form-select form-select-sm room-sensor"
                                data-ci="${ci}" data-ri="${ri}">
                            ${sensorOptions.join('')}
                        </select>
                        <div class="form-text small">
                            Pick any device reporting temperature (motion sensor, THP, thermostat, etc.)
                            to drive call-for-heat for this room.
                        </div>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small mb-1">External temp mode</label>
                        <select class="form-select form-select-sm room-ext-mode"
                                data-ci="${ci}" data-ri="${ri}" ${!sensorIeee ? 'disabled' : ''}>
                            <option value="off"      ${extMode === 'off' ? 'selected' : ''}>Off (ignore sensor)</option>
                            <option value="advisory" ${extMode === 'advisory' ? 'selected' : ''}>Advisory (controller uses sensor)</option>
                            <option value="push"     ${extMode === 'push' ? 'selected' : ''}>Push (also send to TRVs)</option>
                        </select>
                    </div>
                </div>

                ${sensorOnlyBanner}
                ${noTrvsNoSensorWarning}

                <label class="form-label small mb-1">
                    TRVs <span class="badge bg-secondary">${trvCount}</span>
                    ${trvCount === 0 && sensorIeee ? '<small class="text-muted ms-2">optional for sensor-only rooms</small>' : ''}
                </label>
                <div class="list-group mb-2" style="max-height:200px; overflow-y:auto;">${trvCheckboxes}</div>

                ${renderDimensionsPanel(room, ci, ri)}
            </div>
        </div>`;
}

// ============================================================================
// DIMENSIONS PANEL — optional per-room, collapsed by default
// ============================================================================
function renderDimensionsPanel(room, ci, ri) {
    const dim = room.dimensions || {};
    const walls = dim.walls || {};
    const windows = dim.windows || [];
    const doors = dim.doors || [];
    const hasContent = !!(dim.floor_area_m2 || walls.external_m2 || windows.length || doors.length);
    const badgeHtml = hasContent
        ? `<span class="badge bg-success ms-1">set</span>`
        : `<span class="badge bg-secondary ms-1">not set</span>`;

    const collapseId = `dimensions-c${ci}-r${ri}`;

    // Window rows
    const windowRows = windows.map((w, wi) => `
        <div class="row g-1 mb-1 align-items-center window-row" data-ci="${ci}" data-ri="${ri}" data-wi="${wi}">
            <div class="col-md-3">
                <input type="number" step="0.1" min="0" class="form-control form-control-sm window-area"
                       data-ci="${ci}" data-ri="${ri}" data-wi="${wi}"
                       value="${w.area_m2 ?? ''}" placeholder="m²">
            </div>
            <div class="col-md-3">
                <select class="form-select form-select-sm window-glazing"
                        data-ci="${ci}" data-ri="${ri}" data-wi="${wi}">
                    <option value="single"  ${w.glazing === 'single'  ? 'selected' : ''}>Single</option>
                    <option value="double"  ${w.glazing === 'double' || !w.glazing ? 'selected' : ''}>Double</option>
                    <option value="triple"  ${w.glazing === 'triple'  ? 'selected' : ''}>Triple</option>
                </select>
            </div>
            <div class="col-md-3">
                <select class="form-select form-select-sm window-orient"
                        data-ci="${ci}" data-ri="${ri}" data-wi="${wi}">
                    ${['unknown', 'N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'].map(o =>
                        `<option value="${o}" ${(w.orientation || 'unknown') === o ? 'selected' : ''}>${o === 'unknown' ? '— orientation —' : o}</option>`
                    ).join('')}
                </select>
            </div>
            <div class="col-md-3">
                <button class="btn btn-sm btn-outline-danger btn-window-del"
                        data-ci="${ci}" data-ri="${ri}" data-wi="${wi}" title="Remove window">
                    <i class="fas fa-times"></i>
                </button>
            </div>
        </div>`).join('');

    // Door rows
    const doorRows = doors.map((d, di) => `
        <div class="row g-1 mb-1 align-items-center door-row" data-ci="${ci}" data-ri="${ri}" data-di="${di}">
            <div class="col-md-4">
                <input type="number" step="0.1" min="0" class="form-control form-control-sm door-area"
                       data-ci="${ci}" data-ri="${ri}" data-di="${di}"
                       value="${d.area_m2 ?? ''}" placeholder="m²">
            </div>
            <div class="col-md-5">
                <select class="form-select form-select-sm door-type"
                        data-ci="${ci}" data-ri="${ri}" data-di="${di}">
                    <option value="internal" ${d.type === 'internal' || !d.type ? 'selected' : ''}>Internal</option>
                    <option value="external" ${d.type === 'external' ? 'selected' : ''}>External (to outside)</option>
                </select>
            </div>
            <div class="col-md-3">
                <button class="btn btn-sm btn-outline-danger btn-door-del"
                        data-ci="${ci}" data-ri="${ri}" data-di="${di}" title="Remove door">
                    <i class="fas fa-times"></i>
                </button>
            </div>
        </div>`).join('');

    return `
        <div class="border-top pt-2 mt-2">
            <a class="small text-decoration-none" data-bs-toggle="collapse" href="#${collapseId}" role="button"
               aria-expanded="${hasContent ? 'true' : 'false'}">
                <i class="fas fa-ruler-combined me-1"></i>Room dimensions ${badgeHtml}
                <i class="fas fa-caret-down ms-1"></i>
            </a>
            <div class="collapse ${hasContent ? 'show' : ''}" id="${collapseId}">
                <div class="pt-2">
                    <div class="row g-2 mb-2">
                        <div class="col-md-4">
                            <label class="form-label small mb-1">Floor area (m²)</label>
                            <input type="number" step="0.1" min="0" class="form-control form-control-sm dim-floor-area"
                                   data-ci="${ci}" data-ri="${ri}" value="${dim.floor_area_m2 ?? ''}">
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small mb-1">Ceiling height (m)</label>
                            <input type="number" step="0.1" min="1.5" max="5" class="form-control form-control-sm dim-ceiling-height"
                                   data-ci="${ci}" data-ri="${ri}" value="${dim.ceiling_height_m ?? 2.4}">
                        </div>
                    </div>

                    <div class="row g-2 mb-2">
                        <div class="col-md-4">
                            <label class="form-label small mb-1" title="Walls facing outside — biggest heat loss">
                                External wall area (m²) <i class="fas fa-question-circle text-muted"></i>
                            </label>
                            <input type="number" step="0.1" min="0" class="form-control form-control-sm dim-wall-ext"
                                   data-ci="${ci}" data-ri="${ri}" value="${walls.external_m2 ?? ''}">
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small mb-1" title="Walls shared with neighbouring property">
                                Party wall area (m²) <i class="fas fa-question-circle text-muted"></i>
                            </label>
                            <input type="number" step="0.1" min="0" class="form-control form-control-sm dim-wall-party"
                                   data-ci="${ci}" data-ri="${ri}" value="${walls.party_m2 ?? ''}">
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small mb-1" title="Walls adjoining other rooms in this dwelling">
                                Internal wall area (m²)
                            </label>
                            <input type="number" step="0.1" min="0" class="form-control form-control-sm dim-wall-int"
                                   data-ci="${ci}" data-ri="${ri}" value="${walls.internal_m2 ?? ''}">
                        </div>
                    </div>

                    <div class="row g-2 mb-2">
                        <div class="col-md-6">
                            <label class="form-label small mb-1">Floor type</label>
                            <select class="form-select form-select-sm dim-floor-type"
                                    data-ci="${ci}" data-ri="${ri}">
                                ${[
                                    'unknown',
                                    'solid',
                                    'suspended',
                                    'carpet_over_concrete',
                                    'tile_over_concrete',
                                    'wooden',
                                    'carpet_over_wooden',
                                ].map(t =>
                                    `<option value="${t}" ${(dim.floor_type || 'unknown') === t ? 'selected' : ''}>${t.replace(/_/g, ' ')}</option>`
                                ).join('')}
                            </select>
                        </div>
                        <div class="col-md-6">
                            <label class="form-label small mb-1">Ceiling type</label>
                            <select class="form-select form-select-sm dim-ceiling-type"
                                    data-ci="${ci}" data-ri="${ri}">
                                ${['unknown', 'insulated', 'uninsulated', 'flat_roof'].map(t =>
                                    `<option value="${t}" ${(dim.ceiling_type || 'unknown') === t ? 'selected' : ''}>${t.replace(/_/g, ' ')}</option>`
                                ).join('')}
                            </select>
                        </div>
                    </div>

                    <div class="d-flex justify-content-between align-items-center mt-3 mb-1">
                        <strong class="small"><i class="fas fa-square me-1"></i>Windows (${windows.length})</strong>
                        <button class="btn btn-sm btn-outline-secondary btn-window-add"
                                data-ci="${ci}" data-ri="${ri}">
                            <i class="fas fa-plus me-1"></i>Add window
                        </button>
                    </div>
                    <div class="small text-muted mb-1">Area, glazing type and orientation per window</div>
                    ${windowRows || '<div class="small text-muted">No windows added.</div>'}

                    <div class="d-flex justify-content-between align-items-center mt-3 mb-1">
                        <strong class="small"><i class="fas fa-door-closed me-1"></i>Doors (${doors.length})</strong>
                        <button class="btn btn-sm btn-outline-secondary btn-door-add"
                                data-ci="${ci}" data-ri="${ri}">
                            <i class="fas fa-plus me-1"></i>Add door
                        </button>
                    </div>
                    ${doorRows || '<div class="small text-muted">No doors added.</div>'}

                    <div class="mt-3 pt-2 border-top">
                        <div class="row g-2 mb-2 align-items-end">
                            <div class="col-md-5">
                                <label class="form-label small mb-1">
                                    Installed radiator capacity (W at ΔT50)
                                    <i class="fas fa-question-circle text-muted"
                                       title="Rated output stamped on the radiator (e.g. 1200W). Leave blank if unknown."></i>
                                </label>
                                <input type="number" step="10" min="0" class="form-control form-control-sm dim-rad-watts"
                                       data-ci="${ci}" data-ri="${ri}"
                                       value="${(room.radiator && room.radiator.watts_at_dt50) ?? ''}"
                                       placeholder="e.g. 1200">
                            </div>
                            <div class="col-md-7">
                                <label class="form-label small mb-1">Description (optional)</label>
                                <input type="text" class="form-control form-control-sm dim-rad-desc"
                                       data-ci="${ci}" data-ri="${ri}"
                                       value="${escapeAttr((room.radiator && room.radiator.description) || '')}"
                                       placeholder="e.g. Type 22 600×1000 ×2">
                            </div>
                        </div>

                        <div class="d-flex gap-2 mb-2 flex-wrap">
                            <button class="btn btn-sm btn-outline-primary btn-thermal-preview"
                                    data-ci="${ci}" data-ri="${ri}"
                                    data-room-id="${escapeAttr(room.id)}">
                                <i class="fas fa-calculator me-1"></i>Thermal profile
                            </button>
                            <button class="btn btn-sm btn-outline-success btn-sizing-preview"
                                    data-ci="${ci}" data-ri="${ri}"
                                    data-room-id="${escapeAttr(room.id)}">
                                <i class="fas fa-ruler-horizontal me-1"></i>Radiator sizing
                            </button>
                            <button class="btn btn-sm btn-outline-info btn-preheat-preview"
                                    data-ci="${ci}" data-ri="${ri}"
                                    data-room-id="${escapeAttr(room.id)}">
                                <i class="fas fa-hourglass-half me-1"></i>Pre-heat time
                            </button>
                        </div>

                        <div class="mt-2 small thermal-preview-slot"
                             data-ci="${ci}" data-ri="${ri}"></div>
                        <div class="mt-2 small sizing-preview-slot"
                             data-ci="${ci}" data-ri="${ri}"></div>
                        <div class="mt-2 small preheat-preview-slot"
                             data-ci="${ci}" data-ri="${ri}"></div>
                    </div>
                </div>
            </div>
        </div>`;
}

function bindCircuitCards() {
    // Circuit-level
    document.querySelectorAll('.circuit-name').forEach(el => {
        el.addEventListener('input', e => {
            workingCircuits[+e.target.dataset.ci].name = e.target.value;
        });
    });
    document.querySelectorAll('.circuit-receiver').forEach(el => {
        el.addEventListener('change', e => {
            workingCircuits[+e.target.dataset.ci].receiver_ieee = e.target.value || null;
        });
    });
    document.querySelectorAll('.circuit-recvcmd').forEach(el => {
        el.addEventListener('change', e => {
            workingCircuits[+e.target.dataset.ci].receiver_command = e.target.value;
        });
    });
    document.querySelectorAll('.btn-delete-circuit').forEach(btn => {
        btn.addEventListener('click', () => {
            const ci = +btn.dataset.ci;
            if (confirm(`Delete circuit "${workingCircuits[ci]?.name}"? This will also remove its rooms.`)) {
                workingCircuits.splice(ci, 1);
                renderCircuitsList();
            }
        });
    });
    document.querySelectorAll('.btn-add-room').forEach(btn => {
        btn.addEventListener('click', () => {
            const ci = +btn.dataset.ci;
            workingCircuits[ci].rooms = workingCircuits[ci].rooms || [];
            const n = workingCircuits[ci].rooms.length + 1;
            // Prefix with circuit id so newly-added rooms are globally unique.
            // User can rename later; this just avoids the default collision.
            const prefix = (workingCircuits[ci].id || `c${ci + 1}`).toLowerCase()
                .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
            workingCircuits[ci].rooms.push({
                id: `${prefix}_room_${n}`,
                name: `Room ${n}`,
                target_temp: 21, night_setback: 17, min_temp: 16,
                trv_ieees: [], trvs: [], schedule: [],
                temperature_sensor_ieee: null,
                external_temp_mode: 'off',
            });
            renderCircuitsList();
        });
    });

    // Room-level
    document.querySelectorAll('.room-name').forEach(el => {
        el.addEventListener('input', e => {
            workingCircuits[+e.target.dataset.ci].rooms[+e.target.dataset.ri].name = e.target.value;
        });
    });
    const roomNumeric = [
        ['.room-target', 'target_temp'],
        ['.room-setback', 'night_setback'],
        ['.room-min', 'min_temp'],
    ];
    for (const [sel, key] of roomNumeric) {
        document.querySelectorAll(sel).forEach(el => {
            el.addEventListener('change', e => {
                workingCircuits[+e.target.dataset.ci].rooms[+e.target.dataset.ri][key] = parseFloat(e.target.value);
            });
        });
    }
    document.querySelectorAll('.btn-delete-room').forEach(btn => {
        btn.addEventListener('click', () => {
            const ci = +btn.dataset.ci, ri = +btn.dataset.ri;
            if (confirm(`Delete room "${workingCircuits[ci].rooms[ri]?.name}"?`)) {
                workingCircuits[ci].rooms.splice(ri, 1);
                renderCircuitsList();
            }
        });
    });
    document.querySelectorAll('.room-trv-cb').forEach(cb => {
        cb.addEventListener('change', e => {
            const ci = +e.target.dataset.ci, ri = +e.target.dataset.ri;
            const ieee = e.target.dataset.ieee;
            const room = workingCircuits[ci].rooms[ri];
            room.trv_ieees = room.trv_ieees || [];
            room.trvs = Array.isArray(room.trvs) ? room.trvs : [];
            if (e.target.checked) {
                if (!room.trv_ieees.includes(ieee)) room.trv_ieees.push(ieee);
                if (!room.trvs.some(t => t.ieee === ieee)) room.trvs.push({ ieee });
            } else {
                room.trv_ieees = room.trv_ieees.filter(t => t !== ieee);
                room.trvs = room.trvs.filter(t => t.ieee !== ieee);
            }
            renderCircuitsList();
        });
    });
    // Room sensor selection
    document.querySelectorAll('.room-sensor').forEach(el => {
        el.addEventListener('change', e => {
            const ci = +e.target.dataset.ci, ri = +e.target.dataset.ri;
            const room = workingCircuits[ci].rooms[ri];
            room.temperature_sensor_ieee = e.target.value || null;
            // Sensible default: when a sensor gets picked, default mode to
            // 'advisory'; when it's cleared, mode must be 'off'.
            if (!room.temperature_sensor_ieee) {
                room.external_temp_mode = 'off';
            } else if (!room.external_temp_mode || room.external_temp_mode === 'off') {
                room.external_temp_mode = 'advisory';
            }
            renderCircuitsList();  // re-render so ext-mode enabled state updates
        });
    });
    document.querySelectorAll('.room-ext-mode').forEach(el => {
        el.addEventListener('change', e => {
            const ci = +e.target.dataset.ci, ri = +e.target.dataset.ri;
            workingCircuits[ci].rooms[ri].external_temp_mode = e.target.value;
        });
    });

    // ---- Dimensions block (room-level) ----
    function getDim(ci, ri) {
        const room = workingCircuits[ci].rooms[ri];
        if (!room.dimensions) {
            room.dimensions = {
                floor_area_m2: null, ceiling_height_m: 2.4,
                walls: { external_m2: 0, party_m2: 0, internal_m2: 0 },
                windows: [], doors: [],
                floor_type: 'unknown', ceiling_type: 'unknown',
            };
        }
        if (!room.dimensions.walls) room.dimensions.walls = { external_m2: 0, party_m2: 0, internal_m2: 0 };
        if (!room.dimensions.windows) room.dimensions.windows = [];
        if (!room.dimensions.doors) room.dimensions.doors = [];
        return room.dimensions;
    }

    function parseNumOrNull(s) {
        const v = parseFloat(s);
        return isNaN(v) ? null : v;
    }

    // Scalar fields
    const scalarMap = [
        ['.dim-floor-area',      d => ({ ...d, floor_area_m2: parseNumOrNull(event.target.value) })],
    ];
    document.querySelectorAll('.dim-floor-area').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.floor_area_m2 = parseNumOrNull(e.target.value);
        });
    });
    document.querySelectorAll('.dim-ceiling-height').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.ceiling_height_m = parseNumOrNull(e.target.value) || 2.4;
        });
    });
    document.querySelectorAll('.dim-wall-ext').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.walls.external_m2 = parseNumOrNull(e.target.value) || 0;
        });
    });
    document.querySelectorAll('.dim-wall-party').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.walls.party_m2 = parseNumOrNull(e.target.value) || 0;
        });
    });
    document.querySelectorAll('.dim-wall-int').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.walls.internal_m2 = parseNumOrNull(e.target.value) || 0;
        });
    });
    document.querySelectorAll('.dim-floor-type').forEach(el => {
        el.addEventListener('change', e => {
            getDim(+e.target.dataset.ci, +e.target.dataset.ri).floor_type = e.target.value;
        });
    });
    document.querySelectorAll('.dim-ceiling-type').forEach(el => {
        el.addEventListener('change', e => {
            getDim(+e.target.dataset.ci, +e.target.dataset.ri).ceiling_type = e.target.value;
        });
    });

    // Window rows
    document.querySelectorAll('.btn-window-add').forEach(btn => {
        btn.addEventListener('click', () => {
            const d = getDim(+btn.dataset.ci, +btn.dataset.ri);
            d.windows.push({ area_m2: 1.0, glazing: 'double', orientation: 'unknown' });
            renderCircuitsList();
        });
    });
    document.querySelectorAll('.window-area').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.windows[+e.target.dataset.wi].area_m2 = parseNumOrNull(e.target.value) || 0;
        });
    });
    document.querySelectorAll('.window-glazing').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.windows[+e.target.dataset.wi].glazing = e.target.value;
        });
    });
    document.querySelectorAll('.window-orient').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.windows[+e.target.dataset.wi].orientation = e.target.value;
        });
    });
    document.querySelectorAll('.btn-window-del').forEach(btn => {
        btn.addEventListener('click', () => {
            const d = getDim(+btn.dataset.ci, +btn.dataset.ri);
            d.windows.splice(+btn.dataset.wi, 1);
            renderCircuitsList();
        });
    });

    // Door rows
    document.querySelectorAll('.btn-door-add').forEach(btn => {
        btn.addEventListener('click', () => {
            const d = getDim(+btn.dataset.ci, +btn.dataset.ri);
            d.doors.push({ area_m2: 1.9, type: 'internal' });
            renderCircuitsList();
        });
    });
    document.querySelectorAll('.door-area').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.doors[+e.target.dataset.di].area_m2 = parseNumOrNull(e.target.value) || 0;
        });
    });
    document.querySelectorAll('.door-type').forEach(el => {
        el.addEventListener('change', e => {
            const d = getDim(+e.target.dataset.ci, +e.target.dataset.ri);
            d.doors[+e.target.dataset.di].type = e.target.value;
        });
    });
    document.querySelectorAll('.btn-door-del').forEach(btn => {
        btn.addEventListener('click', () => {
            const d = getDim(+btn.dataset.ci, +btn.dataset.ri);
            d.doors.splice(+btn.dataset.di, 1);
            renderCircuitsList();
        });
    });

    document.querySelectorAll('.btn-thermal-preview').forEach(btn => {
        btn.addEventListener('click', async () => {
            const ci = +btn.dataset.ci;
            const ri = +btn.dataset.ri;
            const circuit = workingCircuits[ci];
            const room = circuit?.rooms?.[ri];
            if (!room) return;
            const circuitId = encodeURIComponent(circuit.id);
            const roomId = encodeURIComponent(room.id);

            // Scoped DOM — find the slot inside the same circuit+room combo,
            // NOT by global ID (multiple rooms may share the same room.id).
            const out = document.querySelector(
                `.thermal-preview-slot[data-ci="${ci}"][data-ri="${ri}"]`
            );
            if (!out) return;

            out.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Computing…`;
            btn.disabled = true;
            try {
                // Circuit-scoped endpoint — disambiguates when multiple
                // circuits have rooms sharing the same id.
                const res = await fetch(
                    `/api/heating/circuits/${circuitId}/rooms/${roomId}/thermal`
                );
                const json = await res.json();
                if (!json.success) {
                    out.innerHTML = `<div class="text-danger">${escapeHtml(json.error || 'Failed')}</div>`;
                    return;
                }
                out.innerHTML = renderThermalResult(json.thermal, json.meta);
            } catch (e) {
                out.innerHTML = `<div class="text-danger">Request failed: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
            }
        });
    });

    // Radiator capacity + description
    function ensureRadiator(room) {
        if (!room.radiator || typeof room.radiator !== 'object') room.radiator = {};
        return room.radiator;
    }
    document.querySelectorAll('.dim-rad-watts').forEach(el => {
        el.addEventListener('change', e => {
            const ci = +e.target.dataset.ci, ri = +e.target.dataset.ri;
            const room = workingCircuits[ci].rooms[ri];
            const v = parseFloat(e.target.value);
            if (isNaN(v) || v <= 0) {
                // Empty or invalid: strip the radiator block so config stays tidy
                if (room.radiator) {
                    delete room.radiator.watts_at_dt50;
                    if (!room.radiator.description) delete room.radiator;
                }
            } else {
                ensureRadiator(room).watts_at_dt50 = Math.round(v);
            }
        });
    });
    document.querySelectorAll('.dim-rad-desc').forEach(el => {
        el.addEventListener('change', e => {
            const ci = +e.target.dataset.ci, ri = +e.target.dataset.ri;
            const room = workingCircuits[ci].rooms[ri];
            const v = e.target.value.trim();
            if (!v) {
                if (room.radiator) {
                    delete room.radiator.description;
                    if (!room.radiator.watts_at_dt50) delete room.radiator;
                }
            } else {
                ensureRadiator(room).description = v.slice(0, 100);
            }
        });
    });

    // Radiator sizing preview
    document.querySelectorAll('.btn-sizing-preview').forEach(btn => {
        btn.addEventListener('click', async () => {
            const ci = +btn.dataset.ci, ri = +btn.dataset.ri;
            const circuit = workingCircuits[ci];
            const room = circuit?.rooms?.[ri];
            if (!room) return;
            const out = document.querySelector(
                `.sizing-preview-slot[data-ci="${ci}"][data-ri="${ri}"]`
            );
            if (!out) return;

            out.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Computing sizing…`;
            btn.disabled = true;
            try {
                const res = await fetch(
                    `/api/heating/circuits/${encodeURIComponent(circuit.id)}/rooms/${encodeURIComponent(room.id)}/sizing`
                );
                const json = await res.json();
                if (!json.success) {
                    out.innerHTML = `<div class="text-danger">${escapeHtml(json.error || 'Failed')}</div>`;
                    return;
                }
                out.innerHTML = renderSizingResult(json.sizing, json.meta);
            } catch (e) {
                out.innerHTML = `<div class="text-danger">Request failed: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
            }
        });
    });


    document.querySelectorAll('.btn-preheat-preview').forEach(btn => {
        btn.addEventListener('click', async () => {
            const ci = +btn.dataset.ci, ri = +btn.dataset.ri;
            const circuit = workingCircuits[ci];
            const room = circuit?.rooms?.[ri];
            if (!room) return;
            const out = document.querySelector(
                `.preheat-preview-slot[data-ci="${ci}"][data-ri="${ri}"]`
            );
            if (!out) return;

            out.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Computing pre-heat…`;
            btn.disabled = true;
            try {
                const res = await fetch(
                    `/api/heating/circuits/${encodeURIComponent(circuit.id)}/rooms/${encodeURIComponent(room.id)}/preheat`
                );
                const json = await res.json();
                if (!json.success) {
                    out.innerHTML = `<div class="text-danger">${escapeHtml(json.error || 'Failed')}</div>`;
                    return;
                }
                out.innerHTML = renderPreheatResult(json.preheat, json.meta);
            } catch (e) {
                out.innerHTML = `<div class="text-danger">Request failed: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
            }
        });
    });

}

function renderPreheatResult(p, meta) {
    if (!p) return '<div class="text-muted">No data</div>';

    const warnings = (p.warnings || []).map(w =>
        `<li class="text-warning">${escapeHtml(w)}</li>`).join('');

    const headline = (() => {
        if (!p.reachable) {
            return `<div>
                <i class="fas fa-exclamation-triangle text-warning me-1"></i>
                <strong>Cannot reach target</strong> at current flow temp
                ${p.steady_state_temp_c != null ? `— radiator can only push room to <strong>${p.steady_state_temp_c}°C</strong>` : ''}
            </div>`;
        }
        if (p.minutes_needed == null || p.minutes_needed === 0) {
            return `<div class="text-success">
                <i class="fas fa-check me-1"></i>
                Already at target
            </div>`;
        }
        const mins = Math.round(p.minutes_needed);
        const hrs = Math.floor(mins / 60);
        const rem = mins % 60;
        const timeStr = hrs ? `${hrs}h ${rem}m` : `${mins}m`;
        return `<div>
            <i class="fas fa-hourglass-half me-1"></i>
            Pre-heat needed: <strong class="fs-5 text-info">${timeStr}</strong>
        </div>`;
    })();

    const confBadge = p.confidence === 'high'
        ? '<span class="badge bg-success">high confidence</span>'
        : p.confidence === 'medium'
        ? '<span class="badge bg-warning text-dark">medium confidence</span>'
        : p.confidence === 'low'
        ? '<span class="badge bg-secondary">low confidence</span>'
        : '<span class="badge bg-secondary">no data</span>';

    return `
        <div class="card card-body bg-light p-2">
            <div class="d-flex justify-content-between mb-2 align-items-start">
                ${headline}
                <div>${confBadge}</div>
            </div>

            <div class="row g-2 small">
                <div class="col-md-6">
                    <strong>Now</strong>
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        <li>Current indoor: <strong>${p.from_temp_c?.toFixed(1)}°C</strong></li>
                        <li>Target: <strong>${p.to_temp_c?.toFixed(1)}°C</strong></li>
                        <li>Outdoor: <strong>${p.outdoor_temp_c?.toFixed(1)}°C</strong></li>
                    </ul>
                </div>
                <div class="col-md-6">
                    <strong>Model inputs</strong>
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        <li>Heat loss: ${p.w_per_k != null ? `<strong>${p.w_per_k} W/K</strong>` : '—'}</li>
                        <li>Time constant τ: ${p.tau_seconds != null ? `<strong>${(p.tau_seconds / 60).toFixed(0)} min</strong>` : '—'}</li>
                        <li>Radiator effective: ${p.radiator_watts_effective != null ? `<strong>${Math.round(p.radiator_watts_effective)} W</strong>` : '—'}</li>
                        <li>Steady-state ceiling: ${p.steady_state_temp_c != null ? `<strong>${p.steady_state_temp_c}°C</strong>` : '—'}</li>
                    </ul>
                </div>
            </div>

            ${warnings ? `<ul class="small mt-2 mb-0">${warnings}</ul>` : ''}
            <div class="small text-muted mt-1">
                Based on Newton's law of cooling, applied in reverse. Accuracy
                improves once measured τ stabilises (typically after ~14 days
                of cool-down samples).
            </div>
        </div>`;
}

function renderThermalResult(t, meta) {
    if (!t) return '<div class="text-muted">No data</div>';
    const bd = t.static_breakdown || {};
    const headerMeta = (meta && (meta.circuit_name || meta.room_name)) ? `
        <div class="small text-muted mb-1">
            ${meta.circuit_name ? `<i class="fas fa-stream me-1"></i>${escapeHtml(meta.circuit_name)} › ` : ''}
            ${meta.room_name ? `<strong>${escapeHtml(meta.room_name)}</strong>` : ''}
        </div>` : '';
    const ambiguousWarning = meta?.ambiguous_id ? `
        <div class="alert alert-warning small py-1 px-2 mb-2">
            <i class="fas fa-exclamation-triangle me-1"></i>
            Found ${meta.match_count} rooms with id <code>${escapeHtml(t.room_id)}</code>.
            Showing the first match — rename your rooms to make the ids unique.
        </div>` : '';

    const fmt = v => v == null ? '—' : `${Number(v).toFixed(1)} W/K`;
    const pct = (v, total) => {
        if (!v || !total) return '';
        return ` <span class="text-muted">(${Math.round(100 * v / total)}%)</span>`;
    };
    const staticTotal = t.static_w_per_k || 0;

    const warnings = (t.warnings || []).map(w =>
        `<li class="text-warning">${escapeHtml(w)}</li>`).join('');

    const confidenceLabel = t.measured_confidence >= 0.7
        ? '<span class="badge bg-success">high confidence</span>'
        : t.measured_confidence >= 0.3
        ? '<span class="badge bg-warning text-dark">medium confidence</span>'
        : '<span class="badge bg-secondary">low / none</span>';

    return `
        <div class="card card-body bg-light p-2">
            ${headerMeta}
            ${ambiguousWarning}
            <div class="d-flex justify-content-between mb-2">
                <div>
                    <strong>Blended heat loss:</strong>
                    <span class="fs-5 text-primary">${fmt(t.blended_w_per_k)}</span>
                </div>
                <div>${confidenceLabel}</div>
            </div>

            <div class="row g-2 small">
                <div class="col-md-6">
                    <strong>Static (from dimensions)</strong>: ${fmt(t.static_w_per_k)}
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        <li>Walls (external): ${fmt(bd.walls_external)}${pct(bd.walls_external, staticTotal)}</li>
                        <li>Party walls: ${fmt(bd.walls_party)}${pct(bd.walls_party, staticTotal)}</li>
                        <li>Windows: ${fmt(bd.windows)}${pct(bd.windows, staticTotal)}</li>
                        <li>Doors: ${fmt(bd.doors)}${pct(bd.doors, staticTotal)}</li>
                        <li>Floor: ${fmt(bd.floor)}${pct(bd.floor, staticTotal)}</li>
                        <li>Ceiling: ${fmt(bd.ceiling)}${pct(bd.ceiling, staticTotal)}</li>
                        <li>Ventilation: ${fmt(bd.ventilation)}${pct(bd.ventilation, staticTotal)}</li>
                    </ul>
                </div>
                <div class="col-md-6">
                    <strong>Measured (from telemetry)</strong>: ${fmt(t.measured_w_per_k)}
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        <li>Samples analysed: <strong>${t.measured_sample_count || 0}</strong></li>
                        <li>Best R²: ${t.measured_r2 != null ? t.measured_r2.toFixed(2) : '—'}</li>
                        <li>Time constant τ: ${t.tau_seconds != null ? (t.tau_seconds / 60).toFixed(0) + ' min' : '—'}</li>
                        <li>Insulation: <code>${escapeHtml(meta.insulation)}</code></li>
                        <li>Source: <code>${escapeHtml((meta.sensor_ieee || 'none').slice(-8))}</code></li>
                    </ul>
                </div>
            </div>

            ${warnings ? `<ul class="small mt-2 mb-0">${warnings}</ul>` : ''}
            <div class="small text-muted mt-1">
                Lower W/K = better insulated. Typical UK room: 30–80 W/K.
                Used in Phase 4 for BTU / radiator sizing.
            </div>
        </div>`;
}

function renderSizingResult(s, meta) {
    if (!s) return '<div class="text-muted">No data</div>';

    const fmt = v => v == null ? '—' : `${Math.round(v).toLocaleString()} W`;
    const fmtBtu = v => v == null ? '—' : `${Math.round(v).toLocaleString()} BTU/hr`;

    const warnings = (s.warnings || []).map(w =>
        `<li class="text-warning">${escapeHtml(w)}</li>`).join('');

    const statusBadge = (() => {
        switch (s.status) {
            case 'adequate':
                return '<span class="badge bg-success"><i class="fas fa-check me-1"></i>Adequate</span>';
            case 'undersized':
                return `<span class="badge bg-danger">
                    <i class="fas fa-exclamation-triangle me-1"></i>
                    Undersized by ${Math.round(s.deficit_watts)} W
                </span>`;
            case 'oversized':
                return `<span class="badge bg-warning text-dark">
                    <i class="fas fa-info-circle me-1"></i>
                    Oversized by ${Math.round(s.surplus_watts)} W
                </span>`;
            default:
                return '<span class="badge bg-secondary">No installed data</span>';
        }
    })();

    const installedLine = s.installed_watts_at_dt50 != null ? `
        <li>Installed (ΔT50 rating): <strong>${fmt(s.installed_watts_at_dt50)}</strong>
            ${meta?.radiator_description ? `<small class="text-muted d-block">${escapeHtml(meta.radiator_description)}</small>` : ''}
        </li>
        ${s.installed_watts_at_flow_temp != null
            ? `<li>Effective output at flow ${s.flow_temperature_c}°C: <strong>${fmt(s.installed_watts_at_flow_temp)}</strong></li>`
            : ''}
    ` : `
        <li class="text-muted">
            No installed capacity recorded.
            Enter radiator rating above and re-check to see fit analysis.
        </li>`;

    const nothingToShow = s.required_watts == null;
    if (nothingToShow) {
        return `
            <div class="card card-body bg-light p-2">
                <div class="small text-muted">
                    Can't compute sizing yet —
                    ${(s.warnings || []).length ? escapeHtml(s.warnings[0]) : 'set room dimensions first.'}
                </div>
            </div>`;
    }

    return `
        <div class="card card-body bg-light p-2">
            <div class="d-flex justify-content-between align-items-baseline mb-2">
                <div>
                    <strong>Required radiator output:</strong>
                    <span class="fs-5 text-success">${fmt(s.required_watts_with_margin)}</span>
                    <small class="text-muted">(${fmtBtu(s.required_btu_hr)})</small>
                </div>
                <div>${statusBadge}</div>
            </div>

            <div class="row g-2 small">
                <div class="col-md-6">
                    <strong>Calculation</strong>
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        <li>Room target: <strong>${s.target_temp_c}°C</strong></li>
                        <li>Design outdoor: <strong>${s.design_outdoor_c}°C</strong></li>
                        <li>ΔT: <strong>${s.delta_t}°C</strong></li>
                        <li>Heat loss: <strong>${s.w_per_k != null ? s.w_per_k + ' W/K' : '—'}</strong></li>
                        <li>Raw requirement: ${fmt(s.required_watts)}</li>
                        <li>With ${Math.round((s.oversize_factor - 1) * 100)}% margin: <strong>${fmt(s.required_watts_with_margin)}</strong></li>
                    </ul>
                </div>
                <div class="col-md-6">
                    <strong>Installed</strong>
                    <ul class="mb-1 mt-1" style="list-style:none;padding-left:0;">
                        ${installedLine}
                    </ul>
                </div>
            </div>

            ${warnings ? `<ul class="small mt-2 mb-0">${warnings}</ul>` : ''}
            <div class="small text-muted mt-1">
                Sized to maintain ${s.target_temp_c}°C when outdoor is ${s.design_outdoor_c}°C.
                Lower flow temps (condensing boilers, heat pumps) mean rated radiators
                deliver less than their ΔT50 number.
            </div>
        </div>`;
}

async function fillRoomPreheatSlots() {
    const slots = document.querySelectorAll('[id^="preheat-"][data-circuit-id]');
    if (!slots.length) return;

    // Batch the fetches — keep concurrency modest so we don't thrash the
    // backend on a large setup
    const CONCURRENCY = 3;
    const queue = Array.from(slots);

    async function workOne() {
        while (queue.length) {
            const slot = queue.shift();
            if (!slot) return;
            const circuitId = slot.dataset.circuitId;
            const roomId = slot.dataset.roomId;
            try {
                const res = await fetch(
                    `/api/heating/circuits/${encodeURIComponent(circuitId)}/rooms/${encodeURIComponent(roomId)}/preheat`
                );
                const json = await res.json();
                if (!json.success) {
                    slot.innerHTML = `<i class="fas fa-times-circle text-muted me-1"></i>
                        <span class="text-muted small">${escapeHtml(json.error || 'pre-heat unavailable')}</span>`;
                    continue;
                }
                slot.innerHTML = renderPreheatSnippet(json.preheat, json.meta);
            } catch (e) {
                slot.innerHTML = `<i class="fas fa-times-circle text-muted me-1"></i>
                    <span class="text-muted small">pre-heat lookup failed</span>`;
            }
        }
    }

    await Promise.all(Array.from({ length: CONCURRENCY }, workOne));
}

function renderPreheatSnippet(p, meta) {
    if (!p) return '';
    if (!p.reachable) {
        return `<i class="fas fa-exclamation-triangle text-warning me-1"></i>
            <span class="text-warning">Can't reach target at current flow temp</span>`;
    }
    if (p.minutes_needed == null || p.minutes_needed === 0) {
        return `<i class="fas fa-check text-success me-1"></i>
            <span class="text-success">Already at target</span>`;
    }
    const confidenceColour = p.confidence === 'high'
        ? 'text-success'
        : p.confidence === 'medium'
        ? 'text-warning'
        : 'text-muted';
    const mins = Math.round(p.minutes_needed);
    const hrs = Math.floor(mins / 60);
    const rem = mins % 60;
    const timeStr = hrs ? `${hrs}h ${rem}m` : `${mins}m`;
    return `<i class="fas fa-hourglass-half me-1"></i>
        Pre-heat: <strong>${timeStr}</strong>
        <span class="${confidenceColour} small ms-1">(${p.confidence} confidence)</span>`;
}

async function saveControllerSettings() {
    const btn = document.getElementById('btnControllerSave');
    const status = document.getElementById('controllerSettingsStatus');
    const payload = {
        enabled: document.getElementById('ctrlEnabled').checked,
        dry_run: document.getElementById('ctrlDryRun').checked,
        circuits: workingCircuits,
    };

    // Sanity: warn if enabling without dry-run on first save
    if (payload.enabled && !payload.dry_run && !controllerConfig.enabled) {
        if (!confirm("You're enabling the controller for live operation. Are you sure? Consider enabling 'Dry-run' first to verify behaviour without sending commands.")) {
            return;
        }
    }

    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Saving…`;
    status.innerHTML = '';

    try {
        const res = await fetch('/api/heating/controller/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: payload }),
        });
        const json = await res.json();
        if (!json.success) throw new Error(json.error || 'Save failed');

        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved. ${escapeHtml(json.message || '')}</span>`;
        if (typeof window.showToast === 'function') {
            window.showToast('Controller settings saved — restart to apply', 'success');
        }
        setTimeout(() => {
            const modalEl = document.getElementById('controllerSettingsModal');
            bootstrap.Modal.getInstance(modalEl)?.hide();
            loadControllerStatus();
        }, 900);
    } catch (e) {
        status.innerHTML = `<span class="text-danger">${escapeHtml(e.message)}</span>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="fas fa-save me-1"></i> Save`;
    }
}

// ============================================================================
// HELPERS
// ============================================================================
function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function escapeAttr(s) { return escapeHtml(s); }