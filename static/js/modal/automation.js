/**
 * Device Automation Tab
 * Location: static/js/modal/automation.js
 *
 * Renders threshold-based automation rules for a device.
 * Supports compound AND conditions (multiple threshold rows).
 * Includes trace log viewer for debugging rule evaluation.
 */

import { state } from '../state.js';

// Cached API data (refreshed each time tab opens)
let cachedActuators = [];
let cachedAttributes = [];
let currentSourceIeee = null;

// Condition builder rows (for the add form)
let conditionRows = [];
let conditionIdCounter = 0;

// Operator display labels
const OPERATOR_LABELS = {
    'eq': '=',
    'neq': '≠',
    'gt': '>',
    'lt': '<',
    'gte': '>=',
    'lte': '<='
};

const OPERATOR_TEXT = {
    'eq': 'equals',
    'neq': 'not equal',
    'gt': 'greater than',
    'lt': 'less than',
    'gte': 'greater or equal',
    'lte': 'less or equal'
};

// ============================================================================
// MAIN RENDERER
// ============================================================================

export function renderAutomationTab(device) {
    currentSourceIeee = device.ieee;

    return `
        <div id="automation-tab-content">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <span class="text-muted small">Direct ZigBee triggers — no MQTT delay.</span>
                <div>
                    <button class="btn btn-sm btn-outline-secondary me-1" onclick="window.showAutomationTrace()">
                        <i class="fas fa-search"></i> Trace
                    </button>
                    <button class="btn btn-sm btn-success" onclick="window.showAddAutomationForm()">
                        <i class="fas fa-plus"></i> Add Rule
                    </button>
                </div>
            </div>

            <!-- Add Rule Form (hidden) -->
            <div id="automation-add-form" class="card mb-3" style="display:none;">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-bolt"></i> New Automation Rule</strong>
                    <button class="btn btn-sm btn-outline-secondary" onclick="window.hideAddAutomationForm()">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                <div class="card-body">
                    <!-- Conditions Builder -->
                    <div class="mb-3">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <label class="form-label fw-bold small mb-0">1. When ALL of these are true...</label>
                            <button class="btn btn-sm btn-outline-primary" onclick="window.addConditionRow()">
                                <i class="fas fa-plus"></i> Add Condition
                            </button>
                        </div>
                        <div id="conditions-builder"></div>
                    </div>

                    <!-- Target Action -->
                    <div class="mb-3">
                        <label class="form-label fw-bold small">2. Then send command to...</label>
                        <div class="row g-2">
                            <div class="col-md-6">
                                <select class="form-select form-select-sm" id="auto-target"
                                        onchange="window.onAutomationTargetChange(this)">
                                    <option value="">Loading actuators...</option>
                                </select>
                            </div>
                            <div class="col-md-3">
                                <select class="form-select form-select-sm" id="auto-command">
                                    <option value="">Select...</option>
                                </select>
                            </div>
                            <div class="col-md-3">
                                <input type="text" class="form-control form-control-sm" id="auto-command-value"
                                       placeholder="Value (opt)">
                            </div>
                        </div>
                    </div>

                    <!-- Options -->
                    <div class="row g-2 mb-3">
                        <div class="col-md-4">
                            <label class="form-label small text-muted mb-0">Cooldown (seconds)</label>
                            <input type="number" class="form-control form-control-sm" id="auto-cooldown"
                                   value="5" min="0" max="3600">
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small text-muted mb-0">Endpoint (optional)</label>
                            <input type="number" class="form-control form-control-sm" id="auto-endpoint"
                                   placeholder="Auto">
                        </div>
                        <div class="col-md-4 d-flex align-items-end">
                            <button class="btn btn-primary btn-sm w-100" onclick="window.saveAutomationRule()">
                                <i class="fas fa-save"></i> Save Rule
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Trace Log (hidden) -->
            <div id="automation-trace-panel" class="card mb-3" style="display:none;">
                <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-search"></i> Automation Trace Log</strong>
                    <div>
                        <button class="btn btn-sm btn-outline-light me-1" onclick="window.refreshAutomationTrace()">
                            <i class="fas fa-sync-alt"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-light" onclick="window.hideAutomationTrace()">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                </div>
                <div class="card-body p-0" style="max-height: 400px; overflow-y: auto;">
                    <div id="automation-trace-content" class="font-monospace small p-2">
                        Loading trace...
                    </div>
                </div>
            </div>

            <!-- Rules List -->
            <div id="automation-rules-list">
                <div class="text-center text-muted py-3">
                    <i class="fas fa-spinner fa-spin"></i> Loading rules...
                </div>
            </div>
        </div>
    `;
}


// ============================================================================
// ASYNC INITIALISATION
// ============================================================================

export async function initAutomationTab(ieee) {
    currentSourceIeee = ieee;

    try {
        const [rulesRes, attrsRes, actuatorsRes] = await Promise.all([
            fetch(`/api/automations?source_ieee=${encodeURIComponent(ieee)}`),
            fetch(`/api/automations/device/${encodeURIComponent(ieee)}/attributes`),
            fetch('/api/automations/actuators')
        ]);

        const rules = await rulesRes.json();
        cachedAttributes = await attrsRes.json();
        cachedActuators = await actuatorsRes.json();

        renderRulesList(rules);
        populateTargetSelect();

        // Reset condition builder
        conditionRows = [];
        conditionIdCounter = 0;

    } catch (e) {
        console.error("Failed to load automation data:", e);
        const list = document.getElementById('automation-rules-list');
        if (list) {
            list.innerHTML = `<div class="alert alert-danger">Failed to load: ${e.message}</div>`;
        }
    }
}


// ============================================================================
// RULES LIST RENDERING
// ============================================================================

function renderRulesList(rules) {
    const container = document.getElementById('automation-rules-list');
    if (!container) return;

    if (!rules || rules.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted py-4">
                <i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>
                No automation rules for this device.<br>
                <small>Click <strong>Add Rule</strong> to create one.</small>
            </div>`;
        return;
    }

    let html = '';
    rules.forEach(rule => {
        const conditions = rule.conditions || [];
        const a = rule.action || {};
        const enabled = rule.enabled !== false;
        const targetName = rule.target_name || rule.target_ieee;
        const valueDisplay = a.value !== null && a.value !== undefined ? ` = ${a.value}` : '';
        const epDisplay = a.endpoint_id ? ` (EP${a.endpoint_id})` : '';
        const cooldownDisplay = rule.cooldown ? `${rule.cooldown}s` : '5s';

        // Render conditions
        let condHtml = '';
        conditions.forEach((c, idx) => {
            const opLabel = OPERATOR_LABELS[c.operator] || c.operator;
            const prefix = idx === 0 ?
                '<strong class="text-primary">IF</strong>' :
                '<strong class="text-warning">AND</strong>';
            condHtml += `<div class="small">${prefix} <code>${c.attribute}</code>
                <span class="badge bg-light text-dark border">${opLabel}</span>
                <code>${c.value}</code></div>`;
        });

        html += `
            <div class="card mb-2 ${enabled ? '' : 'opacity-50'}" id="rule-${rule.id}">
                <div class="card-body py-2 px-3">
                    <div class="d-flex justify-content-between align-items-center">
                        <div class="flex-grow-1">
                            ${condHtml}
                            <div class="small mt-1">
                                <strong class="text-success">THEN</strong>
                                <span class="badge bg-info text-dark">${a.command}${valueDisplay}</span>
                                <i class="fas fa-arrow-right text-muted mx-1"></i>
                                <span title="${rule.target_ieee}">${targetName}${epDisplay}</span>
                            </div>
                        </div>
                        <div class="d-flex gap-1 ms-2">
                            <span class="badge bg-secondary" title="Cooldown">${cooldownDisplay}</span>
                            <button class="btn btn-sm ${enabled ? 'btn-outline-success' : 'btn-outline-secondary'}"
                                    onclick="window.toggleAutomationRule('${rule.id}')"
                                    title="${enabled ? 'Disable' : 'Enable'}">
                                <i class="fas fa-${enabled ? 'toggle-on' : 'toggle-off'}"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-danger"
                                    onclick="window.deleteAutomationRule('${rule.id}')"
                                    title="Delete">
                                <i class="fas fa-trash"></i>
                            </button>
                        </div>
                    </div>
                </div>
            </div>`;
    });

    container.innerHTML = html;
}


// ============================================================================
// CONDITION BUILDER
// ============================================================================

function renderConditionRow(rowId) {
    const attrOptions = cachedAttributes.map(attr => {
        const icon = attr.type === 'boolean' ? '⚡' : attr.type === 'float' ? '📊' : '📈';
        return `<option value="${attr.attribute}"
            data-type="${attr.type}"
            data-operators='${JSON.stringify(attr.operators)}'
            data-current="${attr.current_value}">
            ${icon} ${attr.attribute} (${attr.current_value})
        </option>`;
    }).join('');

    return `
        <div class="row g-2 mb-2 align-items-center condition-row" id="cond-row-${rowId}">
            <div class="col-auto">
                <span class="badge ${rowId === 0 ? 'bg-primary' : 'bg-warning text-dark'} small">
                    ${rowId === 0 ? 'IF' : 'AND'}
                </span>
            </div>
            <div class="col">
                <select class="form-select form-select-sm cond-attribute" data-row="${rowId}"
                        onchange="window.onConditionAttributeChange(${rowId}, this)">
                    <option value="">Select attribute...</option>
                    ${attrOptions}
                </select>
            </div>
            <div class="col-auto">
                <select class="form-select form-select-sm cond-operator" data-row="${rowId}"
                        style="width: 100px;">
                    <option value="">Op...</option>
                </select>
            </div>
            <div class="col">
                <input type="text" class="form-control form-control-sm cond-value" data-row="${rowId}"
                       placeholder="Value">
            </div>
            <div class="col-auto">
                ${rowId > 0 ? `<button class="btn btn-sm btn-outline-danger" onclick="window.removeConditionRow(${rowId})" title="Remove"><i class="fas fa-times"></i></button>` : '<div style="width:32px"></div>'}
            </div>
        </div>
        <div class="small text-muted mb-2 ms-5" id="cond-hint-${rowId}"></div>
    `;
}

function refreshConditionsBuilder() {
    const container = document.getElementById('conditions-builder');
    if (!container) return;

    if (conditionRows.length === 0) {
        // Auto-add first row
        conditionRows.push(conditionIdCounter++);
    }

    container.innerHTML = conditionRows.map(id => renderConditionRow(id)).join('');
}


// ============================================================================
// FORM HELPERS
// ============================================================================

function populateTargetSelect() {
    const select = document.getElementById('auto-target');
    if (!select) return;

    select.innerHTML = '<option value="">Select target device...</option>';
    cachedActuators.forEach(dev => {
        if (dev.ieee === currentSourceIeee) return;
        select.innerHTML += `<option value="${dev.ieee}"
            data-commands='${JSON.stringify(dev.commands)}'>
            ${dev.friendly_name} (${dev.model})
        </option>`;
    });
}

function populateCommandsFor(commands) {
    const select = document.getElementById('auto-command');
    const valueInput = document.getElementById('auto-command-value');
    if (!select) return;

    select.innerHTML = '<option value="">Select command...</option>';
    (commands || []).forEach(cmd => {
        select.innerHTML += `<option value="${cmd.command}" data-type="${cmd.type || 'button'}">
            ${cmd.label || cmd.command}${cmd.endpoint_id ? ' (EP' + cmd.endpoint_id + ')' : ''}
        </option>`;
    });
    if (valueInput) valueInput.value = '';
}


// ============================================================================
// TRACE LOG
// ============================================================================

async function loadTraceLog() {
    const container = document.getElementById('automation-trace-content');
    if (!container) return;

    try {
        const res = await fetch('/api/automations/trace');
        const entries = await res.json();

        if (!entries || entries.length === 0) {
            container.innerHTML = '<div class="text-muted p-2">No trace entries yet. Trigger a state change on the source device.</div>';
            return;
        }

        // Show newest first
        const reversed = [...entries].reverse();
        let html = '';

        reversed.forEach(entry => {
            const ts = new Date(entry.timestamp * 1000).toLocaleTimeString();
            const phase = entry.phase || '';
            const result = entry.result || '';
            const ruleId = entry.rule_id || '';

            // Color coding by result
            let color = 'text-muted';
            if (result === 'SUCCESS' || result === 'FIRING') color = 'text-success';
            else if (result === 'NO_MATCH' || result === 'NOT_RELEVANT' || result === 'DISABLED') color = 'text-secondary';
            else if (result === 'BLOCKED') color = 'text-warning';
            else if (result.includes('FAIL') || result.includes('ERROR') || result === 'EXCEPTION' || result.includes('MISSING')) color = 'text-danger';
            else if (result === 'EVALUATING' || result === 'CALLING') color = 'text-info';

            html += `<div class="border-bottom py-1 ${color}">`;
            html += `<span class="text-muted">${ts}</span> `;
            html += `<span class="badge bg-dark">${phase}</span> `;
            html += `<span class="badge ${color === 'text-danger' ? 'bg-danger' : color === 'text-success' ? 'bg-success' : color === 'text-warning' ? 'bg-warning text-dark' : 'bg-secondary'}">${result}</span> `;
            if (ruleId && ruleId !== '-') html += `<code>${ruleId}</code> `;
            html += `${entry.message || ''}`;

            // Show condition details if present
            if (entry.conditions && entry.conditions.length > 0) {
                html += `<div class="ms-3 mt-1">`;
                entry.conditions.forEach(c => {
                    const condColor = c.result === 'PASS' ? 'text-success' : 'text-danger';
                    html += `<div class="${condColor}">`;
                    html += `  #${c.index} ${c.attribute} ${c.operator} ${c.threshold_raw || c.threshold || '?'}`;
                    html += ` → actual: ${c.actual_raw || '?'} (${c.actual_type || '?'})`;
                    html += ` [${c.result}]`;
                    if (c.value_source) html += ` src:${c.value_source}`;
                    if (c.reason) html += ` — ${c.reason}`;
                    html += `</div>`;
                });
                html += `</div>`;
            }

            // Show error/traceback
            if (entry.error) {
                html += `<div class="ms-3 text-danger">${entry.error}</div>`;
            }

            html += `</div>`;
        });

        container.innerHTML = html;

    } catch (e) {
        container.innerHTML = `<div class="text-danger p-2">Failed to load trace: ${e.message}</div>`;
    }
}


// ============================================================================
// WINDOW-EXPOSED EVENT HANDLERS
// ============================================================================

window.onConditionAttributeChange = function(rowId, selectEl) {
    const selected = selectEl.options[selectEl.selectedIndex];
    if (!selected || !selected.value) return;

    const type = selected.dataset.type;
    const operators = JSON.parse(selected.dataset.operators || '["eq","neq"]');
    const currentValue = selected.dataset.current;

    // Populate operator select for this row
    const opSelect = document.querySelector(`#cond-row-${rowId} .cond-operator`);
    if (opSelect) {
        opSelect.innerHTML = '';
        operators.forEach(op => {
            opSelect.innerHTML += `<option value="${op}">${OPERATOR_LABELS[op]} ${OPERATOR_TEXT[op]}</option>`;
        });
    }

    // Hint
    const hint = document.getElementById(`cond-hint-${rowId}`);
    if (hint) {
        hint.textContent = `Current: ${currentValue} (${type})`;
    }

    // Pre-fill for booleans
    const valueInput = document.querySelector(`#cond-row-${rowId} .cond-value`);
    if (valueInput && type === 'boolean') {
        valueInput.value = String(currentValue).toLowerCase() === 'true' ? 'true' : 'false';
        valueInput.setAttribute('placeholder', 'true / false');
    } else if (valueInput) {
        valueInput.value = '';
        valueInput.setAttribute('placeholder', 'Value');
    }
};

window.onAutomationTargetChange = function(selectEl) {
    const selected = selectEl.options[selectEl.selectedIndex];
    if (!selected || !selected.value) return;
    const commands = JSON.parse(selected.dataset.commands || '[]');
    populateCommandsFor(commands);
};

window.addConditionRow = function() {
    if (conditionRows.length >= 5) {
        alert('Maximum 5 conditions per rule.');
        return;
    }
    conditionRows.push(conditionIdCounter++);
    refreshConditionsBuilder();
};

window.removeConditionRow = function(rowId) {
    conditionRows = conditionRows.filter(id => id !== rowId);
    refreshConditionsBuilder();
};

window.showAddAutomationForm = function() {
    const form = document.getElementById('automation-add-form');
    if (form) form.style.display = 'block';
    // Init with one condition row
    conditionRows = [conditionIdCounter++];
    refreshConditionsBuilder();
};

window.hideAddAutomationForm = function() {
    const form = document.getElementById('automation-add-form');
    if (form) form.style.display = 'none';
};

window.showAutomationTrace = function() {
    const panel = document.getElementById('automation-trace-panel');
    if (panel) panel.style.display = 'block';
    loadTraceLog();
};

window.hideAutomationTrace = function() {
    const panel = document.getElementById('automation-trace-panel');
    if (panel) panel.style.display = 'none';
};

window.refreshAutomationTrace = function() {
    loadTraceLog();
};


// ============================================================================
// CRUD ACTIONS
// ============================================================================

window.saveAutomationRule = async function() {
    // Gather conditions from all rows
    const conditions = [];
    let valid = true;

    conditionRows.forEach(rowId => {
        const attrSel = document.querySelector(`#cond-row-${rowId} .cond-attribute`);
        const opSel = document.querySelector(`#cond-row-${rowId} .cond-operator`);
        const valInput = document.querySelector(`#cond-row-${rowId} .cond-value`);

        const attribute = attrSel?.value;
        const operator = opSel?.value;
        const rawValue = valInput?.value;

        if (!attribute || !operator || rawValue === undefined || rawValue === '') {
            valid = false;
            return;
        }

        // Coerce value type
        let value = rawValue;
        if (value.toLowerCase() === 'true') value = true;
        else if (value.toLowerCase() === 'false') value = false;
        else if (!isNaN(value) && value !== '') value = parseFloat(value);

        conditions.push({ attribute, operator, value });
    });

    if (!valid || conditions.length === 0) {
        alert('Please fill in all condition fields (attribute, operator, value).');
        return;
    }

    const targetIeee = document.getElementById('auto-target')?.value;
    const command = document.getElementById('auto-command')?.value;
    const commandValue = document.getElementById('auto-command-value')?.value || null;
    const cooldown = parseInt(document.getElementById('auto-cooldown')?.value) || 5;
    const endpointRaw = document.getElementById('auto-endpoint')?.value;
    const endpointId = endpointRaw ? parseInt(endpointRaw) : null;

    if (!targetIeee || !command) {
        alert('Please select a target device and command.');
        return;
    }

    // Coerce command value
    let cmdVal = commandValue;
    if (cmdVal !== null && cmdVal !== '') {
        if (!isNaN(cmdVal)) cmdVal = parseFloat(cmdVal);
    } else {
        cmdVal = null;
    }

    try {
        const res = await fetch('/api/automations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_ieee: currentSourceIeee,
                conditions,
                target_ieee: targetIeee,
                command,
                command_value: cmdVal,
                endpoint_id: endpointId,
                cooldown,
                enabled: true
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            window.hideAddAutomationForm();
            await refreshAutomationRules();
        } else {
            alert('Failed: ' + (data.detail || data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

window.toggleAutomationRule = async function(ruleId) {
    try {
        const res = await fetch(`/api/automations/${ruleId}/toggle`, { method: 'PATCH' });
        if (res.ok) await refreshAutomationRules();
        else alert('Toggle failed: ' + (await res.json()).detail);
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

window.deleteAutomationRule = async function(ruleId) {
    if (!confirm('Delete this automation rule?')) return;
    try {
        const res = await fetch(`/api/automations/${ruleId}`, { method: 'DELETE' });
        if (res.ok) await refreshAutomationRules();
        else alert('Delete failed: ' + (await res.json()).detail);
    } catch (e) {
        alert('Error: ' + e.message);
    }
};


// ============================================================================
// REFRESH
// ============================================================================

async function refreshAutomationRules() {
    if (!currentSourceIeee) return;
    try {
        const res = await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`);
        const rules = await res.json();
        renderRulesList(rules);
    } catch (e) {
        console.error("Failed to refresh rules:", e);
    }
}