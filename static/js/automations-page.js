/**
 * Automations Page (Global Tab)
 * Location: static/js/automations-page.js
 *
 * Shows ALL automation rules across all devices with inline edit.
 * Reuses the existing modal/automation.js form builder via shared DOM IDs.
 * When editing, the source device is locked to the rule's source_ieee.
 */

import { state } from './state.js';
import { initAutomationTab } from './modal/automation.js';

const OP = { eq:'=', neq:'≠', gt:'>', lt:'<', gte:'≥', lte:'≤', in:'∈', nin:'∉', changed:'Δ' };

let allRulesCache = [];
let filterDevice = '';
let filterState = '';

// ============================================================================
// INIT
// ============================================================================

export function initAutomationsPage() {
    const tab = document.querySelector('button[data-bs-target="#automations"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadAutomationsPage());
    }
}

export async function loadAutomationsPage() {
    const container = document.getElementById('automations-content');
    if (!container) return;
    container.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-spinner fa-spin"></i> Loading automations...</div>`;

    try {
        const [rulesRes, devsRes] = await Promise.all([
            fetch('/api/automations'),
            fetch('/api/automations/devices')
        ]);
        allRulesCache = await rulesRes.json();
        const devices = await devsRes.json();

        _renderPage(container, devices);
    } catch (e) {
        container.innerHTML = `<div class="alert alert-danger"><i class="fas fa-exclamation-triangle"></i> ${e.message}</div>`;
    }
}

// ============================================================================
// PAGE RENDER
// ============================================================================

function _renderPage(container, devices) {
    // Device lookup
    const devMap = {};
    devices.forEach(d => { devMap[d.ieee] = d; });

    // Get unique source devices that have rules
    const sourcesWithRules = [...new Set(allRulesCache.map(r => r.source_ieee))];

    container.innerHTML = `
        <!-- Header -->
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div class="d-flex align-items-center gap-3">
                <span class="text-muted small">All automation rules across devices.</span>
                <span class="badge bg-primary">${allRulesCache.length} rule${allRulesCache.length !== 1 ? 's' : ''}</span>
            </div>
            <div class="d-flex gap-2">
                <select class="form-select form-select-sm" id="ap-filter-dev" style="width:auto;max-width:220px" onchange="window._apFilterDev(this.value)">
                    <option value="">All Devices</option>
                    ${sourcesWithRules.map(ieee => {
                        const d = devMap[ieee];
                        return `<option value="${ieee}">${d ? d.friendly_name : ieee}</option>`;
                    }).join('')}
                </select>
                <select class="form-select form-select-sm" id="ap-filter-state" style="width:auto" onchange="window._apFilterState(this.value)">
                    <option value="">All States</option>
                    <option value="matched">Matched</option>
                    <option value="unmatched">Unmatched</option>
                    <option value="disabled">Disabled</option>
                </select>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apRefresh()"><i class="fas fa-sync-alt"></i></button>
                <button class="btn btn-sm btn-success" onclick="window._apCreate()"><i class="fas fa-plus"></i> New Rule</button>
            </div>
        </div>

        <!-- Create Rule Panel (hidden by default) -->
        <div id="ap-create-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <strong><i class="fas fa-bolt"></i> New Automation</strong>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apCloseCreate()"><i class="fas fa-times"></i></button>
            </div>
            <div class="card-body">
                <div class="mb-3">
                    <label class="form-label small fw-bold">Source Device (trigger)</label>
                    <select class="form-select form-select-sm" id="ap-source-select" onchange="window._apSourceSelected(this.value)">
                        <option value="">Select a device...</option>
                        ${devices.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('')}
                    </select>
                </div>
                <div id="ap-form-host"></div>
            </div>
        </div>

        <!-- Edit Rule Panel (hidden by default) -->
        <div id="ap-edit-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <strong><i class="fas fa-edit"></i> Edit Automation — <span id="ap-edit-device-name"></span></strong>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apCloseEdit()"><i class="fas fa-times"></i></button>
            </div>
            <div class="card-body" id="ap-edit-host"></div>
        </div>

        <!-- Rules List -->
        <div id="ap-rules-list"></div>
    `;

    _renderRulesList(devMap);
}

// ============================================================================
// RULES LIST
// ============================================================================

function _renderRulesList(devMap) {
    const el = document.getElementById('ap-rules-list');
    if (!el) return;

    let rules = allRulesCache;
    if (filterDevice) rules = rules.filter(r => r.source_ieee === filterDevice);
    if (filterState === 'disabled') rules = rules.filter(r => r.enabled === false);
    else if (filterState === 'matched') rules = rules.filter(r => r._state === 'matched' && r.enabled !== false);
    else if (filterState === 'unmatched') rules = rules.filter(r => r._state !== 'matched' && r.enabled !== false);

    if (!rules.length) {
        el.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>No automations found.</div>`;
        return;
    }

    // Group by source device
    const grouped = {};
    rules.forEach(r => {
        const src = r.source_ieee;
        if (!grouped[src]) grouped[src] = [];
        grouped[src].push(r);
    });

    let html = '';
    for (const [ieee, deviceRules] of Object.entries(grouped)) {
        const dev = devMap ? devMap[ieee] : null;
        const devName = dev ? dev.friendly_name : ieee;
        const devModel = dev ? `<span class="text-muted small ms-2">${dev.manufacturer || ''} ${dev.model || ''}</span>` : '';

        html += `<div class="mb-3">
            <h6 class="border-bottom pb-1 mb-2"><i class="fas fa-microchip text-muted me-1"></i> ${devName}${devModel}</h6>`;

        deviceRules.forEach(rule => {
            const en = rule.enabled !== false;
            const st = rule._state || 'unknown';
            const stBadge = st === 'matched'
                ? '<span class="badge bg-success ms-1">matched</span>'
                : st === 'unmatched'
                    ? '<span class="badge bg-secondary ms-1">unmatched</span>'
                    : '<span class="badge bg-dark ms-1">init</span>';
            const runBadge = rule._running ? '<span class="badge bg-warning text-dark ms-1">⏳ running</span>' : '';
            const nm = rule.name ? `<strong>${rule.name}</strong> ` : `<span class="text-muted">${rule.id}</span> `;

            // Conditions summary
            let cH = '';
            (rule.conditions || []).forEach((c, i) => {
                const prefix = i === 0 ? '<strong class="text-primary">IF</strong>' : '<strong class="text-warning">AND</strong>';
                let cDesc;
                if (c.type === 'time_window') {
                    const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
                    const dayStr = (!c.days || c.days.length === 7) ? 'Every day' : c.days.map(d => DAY_NAMES[d]).join(', ');
                    const neg = c.negate ? '<span class="badge bg-danger ms-1">NOT</span>' : '';
                    cDesc = `${neg} Time <code>${c.time_from} → ${c.time_to}</code> <span class="text-muted">${dayStr}</span>`;
                } else {
                    const sus = c.sustain ? `<span class="badge bg-info text-dark ms-1">⏱${c.sustain}s</span>` : '';
                    const dispVal = Array.isArray(c.value) ? c.value.join(', ') : c.value;
                    cDesc = `<code>${c.attribute}</code> ${OP[c.operator] || c.operator} <code>${dispVal}</code>${sus}`;
                }
                cH += `<div class="small">${prefix} ${cDesc}</div>`;
            });

            // Prerequisites summary
            (rule.prerequisites || []).forEach(p => {
                const neg = p.negate ? '<span class="badge bg-danger ms-1">NOT</span>' : '';
                let pDesc;
                if (p.type === 'time_window') {
                    const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
                    const dayStr = (!p.days || p.days.length === 7) ? 'Every day' : p.days.map(d => DAY_NAMES[d]).join(', ');
                    pDesc = `${neg} Time <code>${p.time_from} → ${p.time_to}</code> <span class="text-muted">${dayStr}</span>`;
                } else {
                    pDesc = `${neg} <code>${p.device_name || p.ieee || '?'}</code> ${p.attribute} ${OP[p.operator] || p.operator} <code>${p.value}</code>`;
                }
                cH += `<div class="small"><strong class="text-info">CHECK</strong> ${pDesc}</div>`;
            });

            // Sequence summaries
            const thenSummary = _seqSummary(rule.then_sequence, 'THEN', 'success');
            const elseSummary = _seqSummary(rule.else_sequence, 'ELSE', 'danger');

            html += `
            <div class="card mb-2 ${!en ? 'opacity-50' : ''}" id="ap-rule-${rule.id}">
                <div class="card-body py-2 px-3">
                    <div class="d-flex justify-content-between align-items-start">
                        <div class="flex-grow-1">
                            <div>${nm}${stBadge}${runBadge}
                                ${!en ? '<span class="badge bg-danger ms-1">disabled</span>' : ''}
                                ${rule.cooldown ? `<span class="badge bg-light text-dark border ms-1">⏱${rule.cooldown}s cd</span>` : ''}
                            </div>
                            ${cH}
                            ${thenSummary}
                            ${elseSummary}
                        </div>
                        <div class="d-flex gap-1 ms-2 flex-shrink-0">
                            <button class="btn btn-sm btn-outline-primary" onclick="window._apEdit('${rule.id}')" title="Edit"><i class="fas fa-edit"></i></button>
                            <button class="btn btn-sm btn-outline-${en ? 'warning' : 'success'}" onclick="window._apToggle('${rule.id}')" title="${en ? 'Disable' : 'Enable'}"><i class="fas fa-${en ? 'pause' : 'play'}"></i></button>
                            <button class="btn btn-sm btn-outline-danger" onclick="window._apDelete('${rule.id}')" title="Delete"><i class="fas fa-trash"></i></button>
                        </div>
                    </div>
                </div>
            </div>`;
        });

        html += `</div>`;
    }

    el.innerHTML = html;
}

function _seqSummary(seq, label, color) {
    if (!seq || !seq.length) return '';
    const parts = seq.map(s => {
        if (s.type === 'command') return `<span class="badge bg-${color}">${s.command}${s.value != null ? '=' + s.value : ''}</span> <small class="text-muted">${s.target_name || s.target_ieee || '?'}</small>`;
        if (s.type === 'delay') return `<span class="badge bg-warning text-dark">⏱${s.seconds}s</span>`;
        if (s.type === 'wait_for') return `<span class="badge bg-secondary">⏳ ${s.device_name || s.ieee || '?'} ${s.attribute}</span>`;
        if (s.type === 'condition') return `<span class="badge bg-dark">🔒 ${s.device_name || s.ieee || '?'} ${s.attribute}</span>`;
        if (s.type === 'if_then_else') return `<span class="badge bg-purple" style="background:#6f42c1">IF/THEN/ELSE</span>`;
        if (s.type === 'parallel') return `<span class="badge bg-dark">⚡ PARALLEL(${(s.branches || []).length})</span>`;
        return '';
    }).join(' <i class="fas fa-arrow-right text-muted small"></i> ');
    return `<div class="small mt-1"><strong class="text-${color}">${label}</strong> ${parts}</div>`;
}

// ============================================================================
// ACTIONS
// ============================================================================

async function _apRefresh() {
    await loadAutomationsPage();
}

function _apFilterDev(val) {
    filterDevice = val;
    loadAutomationsPage();
}

function _apFilterState(val) {
    filterState = val;
    loadAutomationsPage();
}

// --- Create ---

function _apCreate() {
    document.getElementById('ap-create-panel').style.display = 'block';
    document.getElementById('ap-edit-panel').style.display = 'none';
    document.getElementById('ap-source-select').value = '';
    document.getElementById('ap-form-host').innerHTML = '<div class="text-muted small">Select a source device to begin.</div>';
    document.getElementById('ap-create-panel').scrollIntoView({ behavior: 'smooth' });
}

function _apCloseCreate() {
    document.getElementById('ap-create-panel').style.display = 'none';
    document.getElementById('ap-form-host').innerHTML = '';
}

async function _apSourceSelected(ieee) {
    const host = document.getElementById('ap-form-host');
    if (!ieee) { host.innerHTML = '<div class="text-muted small">Select a source device to begin.</div>'; return; }

    // Render the automation form UI into the host
    host.innerHTML = `
        <div id="automation-tab-content">
            <div id="a-form" class="card mb-3" style="display:none"></div>
            <div id="a-trace" class="card mb-3" style="display:none">
                <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                    <strong><i class="fas fa-search"></i> Trace</strong>
                    <div class="d-flex gap-2 align-items-center">
                        <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                        <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                        <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('a-trace').style.display='none'"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
            </div>
            <div id="a-rules"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
        </div>`;

    // Init using existing modal/automation.js machinery
    await initAutomationTab(ieee);

    // Auto-open the new rule form
    if (typeof window._aShowForm === 'function') window._aShowForm();
}

// --- Edit ---

async function _apEdit(ruleId) {
    const rule = allRulesCache.find(r => r.id === ruleId);
    if (!rule) return;

    // Close create panel if open
    document.getElementById('ap-create-panel').style.display = 'none';

    const editPanel = document.getElementById('ap-edit-panel');
    const editHost = document.getElementById('ap-edit-host');
    const editDevName = document.getElementById('ap-edit-device-name');

    // Resolve device name
    try {
        const devsRes = await fetch('/api/automations/devices');
        const devs = await devsRes.json();
        const dev = devs.find(d => d.ieee === rule.source_ieee);
        editDevName.textContent = dev ? dev.friendly_name : rule.source_ieee;
    } catch (e) {
        editDevName.textContent = rule.source_ieee;
    }

    // Render automation tab structure into edit host
    editHost.innerHTML = `
        <div id="automation-tab-content">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <span class="text-muted small">Editing rule for this device.</span>
                <div>
                    <button class="btn btn-sm btn-outline-secondary me-1" onclick="window._aTrace()"><i class="fas fa-search"></i> Trace</button>
                    <button class="btn btn-sm btn-success" onclick="window._aShowForm()"><i class="fas fa-plus"></i> Add Rule</button>
                </div>
            </div>
            <div id="a-form" class="card mb-3" style="display:none"></div>
            <div id="a-trace" class="card mb-3" style="display:none">
                <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                    <strong><i class="fas fa-search"></i> Trace</strong>
                    <div class="d-flex gap-2 align-items-center">
                        <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                        <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                        <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('a-trace').style.display='none'"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
            </div>
            <div id="a-rules"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
        </div>`;

    editPanel.style.display = 'block';
    editPanel.scrollIntoView({ behavior: 'smooth' });

    // Init automation tab for this device
    await initAutomationTab(rule.source_ieee);

    // Open edit form for this specific rule
    if (typeof window._aEdit === 'function') window._aEdit(ruleId);
}

function _apCloseEdit() {
    document.getElementById('ap-edit-panel').style.display = 'none';
    document.getElementById('ap-edit-host').innerHTML = '';
    // Refresh the rules list to reflect any changes
    loadAutomationsPage();
}

// --- Toggle / Delete ---

async function _apToggle(ruleId) {
    try {
        await fetch(`/api/automations/${ruleId}/toggle`, { method: 'PATCH' });
        await loadAutomationsPage();
    } catch (e) {
        console.error('Toggle failed:', e);
    }
}

async function _apDelete(ruleId) {
    if (!confirm('Delete this automation rule?')) return;
    try {
        await fetch(`/api/automations/${ruleId}`, { method: 'DELETE' });
        await loadAutomationsPage();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

window._apRefresh = _apRefresh;
window._apFilterDev = _apFilterDev;
window._apFilterState = _apFilterState;
window._apCreate = _apCreate;
window._apCloseCreate = _apCloseCreate;
window._apSourceSelected = _apSourceSelected;
window._apEdit = _apEdit;
window._apCloseEdit = _apCloseEdit;
window._apToggle = _apToggle;
window._apDelete = _apDelete;