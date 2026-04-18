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
    const callBadge = c.calling_for_heat
        ? `<span class="badge bg-danger"><i class="fas fa-fire me-1"></i>Calling for heat</span>`
        : `<span class="badge bg-secondary">Idle</span>`;
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
                ${trvLines || '<div class="small text-muted">No TRVs</div>'}
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
        const [cfgRes, devRes] = await Promise.all([
            fetch('/api/heating/controller/config').then(r => r.json()),
            fetch('/api/heating/controller/devices').then(r => r.json()).catch(() => ({ success: false })),
        ]);
        if (!cfgRes.success) throw new Error(cfgRes.error || 'Config load failed');

        controllerConfig = cfgRes.config;
        controllerDevices = devRes.success
            ? { receivers: devRes.receivers || [], thermostats: devRes.thermostats || [] }
            : { receivers: [], thermostats: [] };
        workingCircuits = JSON.parse(JSON.stringify(controllerConfig.circuits || []));

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

                <label class="form-label small mb-1">
                    TRVs <span class="badge bg-secondary">${(room.trv_ieees || []).length}</span>
                </label>
                <div class="list-group" style="max-height:200px; overflow-y:auto;">${trvCheckboxes}</div>
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
            const n = (workingCircuits[ci].rooms || []).length + 1;
            workingCircuits[ci].rooms = workingCircuits[ci].rooms || [];
            workingCircuits[ci].rooms.push({
                id: `room_${n}`, name: `Room ${n}`,
                target_temp: 21, night_setback: 17, min_temp: 16,
                trv_ieees: [], schedule: [],
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
            if (e.target.checked) {
                if (!room.trv_ieees.includes(ieee)) room.trv_ieees.push(ieee);
            } else {
                room.trv_ieees = room.trv_ieees.filter(t => t !== ieee);
            }
            renderCircuitsList();  // re-render so other rooms' available lists update
        });
    });
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