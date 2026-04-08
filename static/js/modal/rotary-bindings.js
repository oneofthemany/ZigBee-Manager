/**
 * Rotary Binding UI — bind Matter rotary dials to target device attributes.
 * Location: static/js/modal/rotary-bindings.js
 *
 * Shows in the Endpoints tab for Matter devices with rotary endpoints.
 * Lets users bind each dial to a target device + command with min/max range.
 */

// ============================================================================
// IMPORTS
// ============================================================================
import { state } from '../state.js';

// ============================================================================
// STATE
// ============================================================================
let _commandDefaults = null;

// ============================================================================
// RENDER
// ============================================================================

export function renderRotaryBindingsSection(sourceIeee, endpoints) {
    // Filter to rotary endpoints only
    const rotaryEps = (endpoints || []).filter(ep =>
        ep.role === 'rotary' && ep.endpoint_id !== 0
    );

    if (rotaryEps.length === 0) return '';

    return `
        <div class="mt-3">
            <h6 class="mb-2">
                <i class="fas fa-sync-alt me-1 text-info"></i> Rotary Bindings
                <span class="badge bg-info ms-1">${rotaryEps.length} dials</span>
            </h6>
            <p class="small text-muted mb-2">
                Map each dial position to control a device attribute (brightness, volume, temperature, etc.)
            </p>
            <div id="rotaryBindingsContainer">
                <div class="text-center py-2">
                    <i class="fas fa-spinner fa-spin"></i> Loading bindings...
                </div>
            </div>
        </div>
    `;
}

export async function initRotaryBindings(sourceIeee, endpoints) {
    const container = document.getElementById('rotaryBindingsContainer');
    if (!container) return;

    // Load command defaults
    if (!_commandDefaults) {
        try {
            const res = await fetch('/api/rotary-bindings/commands');
            const data = await res.json();
            if (data.success) _commandDefaults = data.commands;
        } catch (e) { /* non-fatal */ }
    }
    if (!_commandDefaults) _commandDefaults = {};

    // Load existing bindings
    let existingBindings = [];
    try {
        const res = await fetch(`/api/rotary-bindings/${encodeURIComponent(sourceIeee)}`);
        const data = await res.json();
        if (data.success) existingBindings = data.bindings || [];
    } catch (e) { /* non-fatal */ }

    // Get available target devices
    let targetDevices = [];
    try {
        const res = await fetch('/api/automations/actuators');
        targetDevices = await res.json();
    } catch (e) { /* non-fatal */ }

    // Also add all devices for sensor-type targets
    try {
        const res = await fetch('/api/automations/devices');
        const allDevs = await res.json();
        // Merge — actuators take priority but add any missing
        const actuatorIeees = new Set(targetDevices.map(d => d.ieee));
        for (const d of allDevs) {
            if (!actuatorIeees.has(d.ieee)) targetDevices.push(d);
        }
    } catch (e) { /* non-fatal */ }

    const rotaryEps = (endpoints || []).filter(ep =>
        ep.role === 'rotary' && ep.endpoint_id !== 0
    );

    // Group by group label to show one binding UI per dial group
    const groups = {};
    for (const ep of rotaryEps) {
        const group = ep.group || `ep${ep.endpoint_id}`;
        if (!groups[group]) {
            groups[group] = {
                label: ep.label,
                group: group,
                eps: [],
                maxPositions: ep.switch_info?.positions || 18,
                mode: 'step',      // default for paired EPs
                stepSize: 25,
            };
        }
        groups[group].eps.push(ep);
    }

    // Deduplicate: one binding card per group
    const uniqueGroups = Object.values(groups);

    container.innerHTML = uniqueGroups.map(g => {
        const rotaryKey = `${g.group}_rotary`;
        const existing = existingBindings.find(b => b.rotary_key === rotaryKey);
        return _renderBindingCard(g, rotaryKey, existing, sourceIeee, targetDevices);
    }).join('');
}

function _renderBindingCard(group, rotaryKey, existing, sourceIeee, targetDevices) {
    const ep = group.eps[0];
    const maxPos = group.maxPositions;
    const bound = !!existing;
    const borderColor = bound ? 'success' : 'secondary';

    const commandOptions = Object.entries(_commandDefaults || {}).map(([cmd, info]) =>
        `<option value="${cmd}" data-min="${info.min}" data-max="${info.max}"
                ${existing?.target_command === cmd ? 'selected' : ''}>
            ${info.label || cmd}
        </option>`
    ).join('');

    const deviceOptions = targetDevices.map(d =>
        `<option value="${d.ieee}" ${existing?.target_ieee === d.ieee ? 'selected' : ''}>
            ${d.friendly_name} (${d.model || d.ieee})
        </option>`
    ).join('');

    return `
        <div class="card mb-2 border-${borderColor}" id="rb-card-${rotaryKey}">
            <div class="card-header bg-${borderColor} bg-opacity-10 py-2 d-flex justify-content-between align-items-center">
                <span class="small fw-bold">
                    <i class="fas fa-sync-alt me-1 text-info"></i>
                    ${group.label || rotaryKey}
                    <span class="badge bg-dark bg-opacity-25 ms-1">${maxPos} positions</span>
                </span>
                ${bound ? '<span class="badge bg-success">Bound</span>' : '<span class="badge bg-secondary">Unbound</span>'}
            </div>
            <div class="card-body py-2 small">
                <div class="row g-2 mb-2">
                    <div class="col-md-6">
                        <label class="form-label mb-0">Target Device</label>
                        <select class="form-select form-select-sm" id="rb-target-${rotaryKey}">
                            <option value="">— Select target —</option>
                            ${deviceOptions}
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label mb-0">Command</label>
                        <select class="form-select form-select-sm" id="rb-cmd-${rotaryKey}"
                                onchange="window._rbCmdChanged('${rotaryKey}')">
                            ${commandOptions}
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label mb-0">${group.mode === 'step' ? 'Step Size' : 'Positions'}</label>
                        <input type="number" class="form-control form-control-sm" id="rb-pos-${rotaryKey}"
                               value="${existing?.step_size || group.stepSize || maxPos}" min="1" max="254">
                    </div>
                </div>
                <div class="row g-2 mb-2">
                    <div class="col-md-3">
                        <label class="form-label mb-0">Min Value</label>
                        <input type="number" class="form-control form-control-sm" id="rb-min-${rotaryKey}"
                               value="${existing?.target_min ?? _commandDefaults[existing?.target_command]?.min ?? 0}"
                               step="any">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label mb-0">Max Value</label>
                        <input type="number" class="form-control form-control-sm" id="rb-max-${rotaryKey}"
                               value="${existing?.target_max ?? _commandDefaults[existing?.target_command]?.max ?? 254}"
                               step="any">
                    </div>
                    <div class="col-md-3 d-flex align-items-end gap-2">
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" id="rb-invert-${rotaryKey}"
                                   ${existing?.invert ? 'checked' : ''}>
                            <label class="form-check-label" for="rb-invert-${rotaryKey}">Invert</label>
                        </div>
                    </div>
                    <div class="col-md-3 d-flex align-items-end justify-content-end gap-1">
                        <button class="btn btn-success btn-sm"
                                onclick="window._rbSave('${sourceIeee}', '${rotaryKey}', ${ep.endpoint_id}, ${maxPos})">
                            <i class="fas fa-save me-1"></i> ${bound ? 'Update' : 'Bind'}
                        </button>
                        ${bound ? `
                            <button class="btn btn-outline-danger btn-sm"
                                    onclick="window._rbRemove('${sourceIeee}', '${rotaryKey}')">
                                <i class="fas fa-unlink"></i>
                            </button>
                        ` : ''}
                    </div>
                </div>

                <!-- Preview bar -->
                <div class="mt-1 px-1">
                    <div class="d-flex justify-content-between" style="font-size:0.7rem">
                        <span>Pos 0 → <span id="rb-preview-min-${rotaryKey}">
                            ${existing?.target_min ?? 0}</span></span>
                        <span>Pos ${maxPos} → <span id="rb-preview-max-${rotaryKey}">
                            ${existing?.target_max ?? 254}</span></span>
                    </div>
                    <div class="progress" style="height:4px">
                        <div class="progress-bar bg-info" style="width:50%"></div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// ============================================================================
// INTERACTIONS
// ============================================================================

window._rbCmdChanged = function (rotaryKey) {
    const cmdSelect = document.getElementById(`rb-cmd-${rotaryKey}`);
    if (!cmdSelect) return;

    const selected = cmdSelect.options[cmdSelect.selectedIndex];
    const min = selected?.dataset?.min;
    const max = selected?.dataset?.max;

    const minInput = document.getElementById(`rb-min-${rotaryKey}`);
    const maxInput = document.getElementById(`rb-max-${rotaryKey}`);
    if (minInput && min !== undefined) minInput.value = min;
    if (maxInput && max !== undefined) maxInput.value = max;

    // Update preview
    const previewMin = document.getElementById(`rb-preview-min-${rotaryKey}`);
    const previewMax = document.getElementById(`rb-preview-max-${rotaryKey}`);
    if (previewMin) previewMin.textContent = min;
    if (previewMax) previewMax.textContent = max;
};

window._rbSave = async function (sourceIeee, rotaryKey, ep, maxPositions) {
    const targetIeee = document.getElementById(`rb-target-${rotaryKey}`)?.value;
    const command = document.getElementById(`rb-cmd-${rotaryKey}`)?.value;
    const stepSize = parseInt(document.getElementById(`rb-pos-${rotaryKey}`)?.value || '25');
    const min = parseFloat(document.getElementById(`rb-min-${rotaryKey}`)?.value || '0');
    const max = parseFloat(document.getElementById(`rb-max-${rotaryKey}`)?.value || '254');
    const invert = document.getElementById(`rb-invert-${rotaryKey}`)?.checked || false;

    if (!targetIeee) { alert('Select a target device.'); return; }
    if (!command) { alert('Select a command.'); return; }

    // Get CW/CCW EPs from the definition's rotary_bindings
    let mode = 'step', cwEp = ep, ccwEp = 0;
    try {
        const defRes = await fetch('/api/matter/definitions');
        const defData = await defRes.json();
        if (defData.success) {
            for (const d of defData.definitions) {
                const detailRes = await fetch(`/api/matter/definitions/${d.filename}`);
                const detail = await detailRes.json();
                if (detail.success) {
                    const rb = detail.definition?.rotary_bindings?.[rotaryKey];
                    if (rb) {
                        mode = rb.mode || 'step';
                        cwEp = rb.cw_ep || ep;
                        ccwEp = rb.ccw_ep || 0;
                        break;
                    }
                }
            }
        }
    } catch (e) { /* use defaults */ }

    try {
        const res = await fetch('/api/rotary-bindings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_ieee: sourceIeee,
                rotary_key: rotaryKey,
                ep: cwEp,
                max_positions: maxPositions,
                mode: mode,
                cw_ep: cwEp,
                ccw_ep: ccwEp,
                step_size: stepSize,
                target: {
                    ieee: targetIeee,
                    command: command,
                    min: min,
                    max: max,
                    invert: invert,
                },
            }),
        });
        const data = await res.json();
        if (data.success) {
            const card = document.getElementById(`rb-card-${rotaryKey}`);
            if (card) {
                card.className = card.className.replace('border-secondary', 'border-success');
                const badge = card.querySelector('.card-header .badge:last-child');
                if (badge) { badge.className = 'badge bg-success'; badge.textContent = 'Bound'; }
            }
        } else {
            alert('Save failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

window._rbRemove = async function (sourceIeee, rotaryKey) {
    if (!confirm(`Unbind ${rotaryKey}?`)) return;

    try {
        const res = await fetch(
            `/api/rotary-bindings/${encodeURIComponent(sourceIeee)}/${encodeURIComponent(rotaryKey)}`,
            { method: 'DELETE' }
        );
        const data = await res.json();

        if (data.success) {
            const card = document.getElementById(`rb-card-${rotaryKey}`);
            if (card) {
                card.className = card.className.replace('border-success', 'border-secondary');
                const badge = card.querySelector('.card-header .badge:last-child');
                if (badge) {
                    badge.className = 'badge bg-secondary';
                    badge.textContent = 'Unbound';
                }
            }
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};