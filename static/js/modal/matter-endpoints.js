/**
 * Matter Endpoint Explorer — scan, label, and define Matter device endpoints.
 * Location: static/js/modal/matter-endpoints.js
 *
 * Adds an "Endpoints" tab to the Matter device modal that:
 *   1. Scans raw attributes into a structured endpoint map
 *   2. Shows roles, tags, switch features per endpoint
 *   3. Lets users generate and edit device definitions
 *   4. Saves definitions to config/matter_definitions/
 */

 // ============================================================================
 // IMPORTS
 // ============================================================================

 import { renderRotaryBindingsSection, initRotaryBindings } from './rotary-bindings.js';

// ============================================================================
// STATE
// ============================================================================

let _currentNodeId = null;
let _currentDraft = null;
let _existingDef = null;

// ============================================================================
// TAB RENDERER
// ============================================================================

export function renderMatterEndpointsTab(device) {
    const nodeId = device.state?.node_id;
    if (!nodeId) return '<div class="alert alert-warning">No Matter node ID.</div>';

    return `
        <div id="matterEndpointsContent">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h6 class="mb-0"><i class="fas fa-project-diagram me-1"></i> Endpoint Explorer</h6>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-primary" onclick="window._matterScanEndpoints(${nodeId})">
                        <i class="fas fa-search me-1"></i> Scan
                    </button>
                    <button class="btn btn-outline-success" onclick="window._matterGenerateDef(${nodeId})" id="matterGenDefBtn">
                        <i class="fas fa-magic me-1"></i> Generate Definition
                    </button>
                </div>
            </div>
            <div id="matterEndpointsBody">
                <div class="text-center text-muted py-4">
                    <i class="fas fa-info-circle me-1"></i> Click <strong>Scan</strong> to map this device's endpoints.
                </div>
            </div>
            <div id="matterDefEditor" class="d-none mt-3"></div>
        </div>
    `;
}

export function initMatterEndpointsTab(nodeId) {
    _currentNodeId = nodeId;
    _checkExistingDefinition(nodeId);
}

// ============================================================================
// SCAN ENDPOINTS
// ============================================================================

window._matterScanEndpoints = async function (nodeId) {
    const body = document.getElementById('matterEndpointsBody');
    if (!body) return;

    body.innerHTML = '<div class="text-center py-3"><i class="fas fa-spinner fa-spin"></i> Scanning endpoints...</div>';

    try {
        const res = await fetch(`/api/matter/nodes/${nodeId}/scan-endpoints`);
        const data = await res.json();

        if (!data.success) {
            body.innerHTML = `<div class="alert alert-danger small">${data.detail || 'Scan failed'}</div>`;
            return;
        }

        body.innerHTML = _renderScanResults(data, nodeId);

        // Init rotary bindings UI
        const sourceIeee = `matter_${nodeId}`;
        initRotaryBindings(sourceIeee, data.endpoints);

    } catch (e) {
        body.innerHTML = `<div class="alert alert-danger small">Error: ${e.message}</div>`;
    }
};

function _renderScanResults(data, nodeId) {
    const eps = data.endpoints || [];
    const nonRoot = eps.filter(ep => ep.endpoint_id !== 0);

    return `
        <div class="small text-muted mb-2">
            <strong>${data.friendly_name}</strong> — ${data.manufacturer} ${data.model}
            — ${nonRoot.length} functional endpoint(s)
        </div>
        ${_existingDef ? `
            <div class="alert alert-success py-2 small">
                <i class="fas fa-check-circle me-1"></i> Definition loaded:
                <strong>${_existingDef.product_id}</strong>
                <button class="btn btn-sm btn-outline-primary float-end py-0"
                        onclick="window._matterEditDef()">
                    <i class="fas fa-edit me-1"></i> Edit
                </button>
            </div>
        ` : ''}
        <div class="row g-2">
            ${eps.map(ep => _renderEndpointCard(ep)).join('')}
            ${renderRotaryBindingsSection(data.ieee || `matter_${nodeId}`, eps)}
        </div>
    `;
}

function _renderEndpointCard(ep) {
    if (ep.endpoint_id === 0 && ep.role === 'root') {
        return _renderRootCard(ep);
    }

    const roleColors = {
        button: 'warning', rotary: 'info', toggle: 'primary',
        switch: 'secondary', root: 'dark', unknown: 'secondary',
    };
    const roleIcons = {
        button: 'fa-hand-pointer', rotary: 'fa-sync-alt', toggle: 'fa-toggle-on',
        switch: 'fa-power-off', root: 'fa-server', unknown: 'fa-question',
    };

    const color = roleColors[ep.role] || 'secondary';
    const icon = roleIcons[ep.role] || 'fa-question';
    const switchInfo = ep.switch_info;

    return `
        <div class="col-md-6 col-lg-4">
            <div class="card h-100 border-${color}">
                <div class="card-header bg-${color} bg-opacity-10 py-2 d-flex justify-content-between align-items-center">
                    <span class="fw-bold small">
                        <i class="fas ${icon} me-1 text-${color}"></i>
                        EP${ep.endpoint_id}
                    </span>
                    <span class="badge bg-${color}">${ep.role}</span>
                </div>
                <div class="card-body py-2 small">
                    <div class="fw-semibold mb-1">${ep.label}</div>

                    ${ep.device_types.length ? `
                        <div class="text-muted">
                            ${ep.device_types.map(dt =>
                                `<span class="badge bg-light text-dark border me-1">${dt.type_name}</span>`
                            ).join('')}
                        </div>
                    ` : ''}

                    ${ep.tags.length ? `
                        <div class="mt-1">
                            ${ep.tags.map(t => {
                                const label = t.label ? `"${t.label}"` : t.semantic;
                                return `<span class="badge bg-info bg-opacity-25 text-dark me-1">
                                    <i class="fas fa-tag me-1"></i>${label}
                                </span>`;
                            }).join('')}
                        </div>
                    ` : ''}

                    ${switchInfo ? `
                        <div class="mt-2 border-top pt-1">
                            <div><i class="fas fa-sliders-h me-1"></i> Positions: <code>${switchInfo.positions}</code></div>
                            <div><i class="fas fa-dot-circle me-1"></i> Current: <code>${switchInfo.current_position}</code></div>
                            ${switchInfo.multi_press_max ? `<div><i class="fas fa-hand-point-up me-1"></i> Multi-press max: <code>${switchInfo.multi_press_max}</code></div>` : ''}
                            <div class="mt-1">
                                ${switchInfo.features.map(f =>
                                    `<span class="badge bg-dark bg-opacity-10 text-dark me-1">${f.replace(/_/g, ' ')}</span>`
                                ).join('')}
                            </div>
                        </div>
                    ` : ''}

                    <div class="text-muted mt-1" style="font-size:0.75rem">
                        Clusters: ${Object.keys(ep.clusters).map(c =>
                            `<code>${ep.clusters[c].cluster_name}</code>`
                        ).join(', ')}
                    </div>
                </div>
            </div>
        </div>
    `;
}

function _renderRootCard(ep) {
    return `
        <div class="col-12">
            <div class="card border-dark bg-dark bg-opacity-10 mb-2">
                <div class="card-body py-2 small">
                    <i class="fas fa-server me-1"></i>
                    <strong>EP0 — Root Node</strong>
                    <span class="text-muted ms-2">
                        ${Object.keys(ep.clusters).length} infrastructure clusters
                    </span>
                </div>
            </div>
        </div>
    `;
}

// ============================================================================
// GENERATE DEFINITION
// ============================================================================

window._matterGenerateDef = async function (nodeId) {
    const editor = document.getElementById('matterDefEditor');
    if (!editor) return;

    editor.classList.remove('d-none');
    editor.innerHTML = '<div class="text-center py-3"><i class="fas fa-spinner fa-spin"></i> Generating definition...</div>';

    try {
        const res = await fetch(`/api/matter/nodes/${nodeId}/generate-definition`, { method: 'POST' });
        const data = await res.json();

        if (!data.success) {
            editor.innerHTML = `<div class="alert alert-danger small">${data.detail || 'Generation failed'}</div>`;
            return;
        }

        _currentDraft = data.definition;
        _renderDefEditor(editor, data.definition, false);
    } catch (e) {
        editor.innerHTML = `<div class="alert alert-danger small">Error: ${e.message}</div>`;
    }
};

window._matterEditDef = function () {
    const editor = document.getElementById('matterDefEditor');
    if (!editor || !_existingDef) return;

    editor.classList.remove('d-none');
    _currentDraft = JSON.parse(JSON.stringify(_existingDef));
    _renderDefEditor(editor, _currentDraft, true);
};

function _renderDefEditor(container, defn, isEdit) {
    const endpoints = defn.endpoints || {};
    const stateMapping = defn.state_mapping || {};

    container.innerHTML = `
        <div class="card border-primary">
            <div class="card-header bg-primary bg-opacity-10 py-2 d-flex justify-content-between align-items-center">
                <span class="fw-bold small">
                    <i class="fas fa-file-code me-1"></i>
                    ${isEdit ? 'Edit' : 'New'} Device Definition
                </span>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-secondary" onclick="window._matterToggleJson()">
                        <i class="fas fa-code me-1"></i> JSON
                    </button>
                    <button class="btn btn-success" onclick="window._matterSaveDef()">
                        <i class="fas fa-save me-1"></i> Save
                    </button>
                    <button class="btn btn-outline-danger" onclick="document.getElementById('matterDefEditor').classList.add('d-none')">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
            </div>
            <div class="card-body p-2 small">
                <!-- Identity -->
                <div class="row g-2 mb-2">
                    <div class="col-3">
                        <label class="form-label mb-0">Vendor ID</label>
                        <input type="number" class="form-control form-control-sm" id="defVendorId"
                               value="${defn.vendor_id || 0}">
                    </div>
                    <div class="col-3">
                        <label class="form-label mb-0">Product ID</label>
                        <input type="text" class="form-control form-control-sm" id="defProductId"
                               value="${defn.product_id || ''}">
                    </div>
                    <div class="col-3">
                        <label class="form-label mb-0">Model</label>
                        <input type="text" class="form-control form-control-sm" id="defModel"
                               value="${defn.model || ''}">
                    </div>
                    <div class="col-3">
                        <label class="form-label mb-0">Device Type</label>
                        <select class="form-select form-select-sm" id="defDeviceType">
                            ${['Button', 'Light', 'Switch', 'Sensor', 'Cover', 'Lock', 'Thermostat', 'Matter']
                                .map(t => `<option ${defn.device_type === t ? 'selected' : ''}>${t}</option>`)
                                .join('')}
                        </select>
                    </div>
                </div>

                <!-- Endpoints -->
                <h6 class="mb-1 mt-2"><i class="fas fa-plug me-1"></i> Endpoints</h6>
                <div class="table-responsive">
                    <table class="table table-sm table-bordered mb-2">
                        <thead>
                            <tr>
                                <th style="width:8%">EP</th>
                                <th style="width:15%">Role</th>
                                <th style="width:30%">Label</th>
                                <th style="width:15%">Group</th>
                            </tr>
                        </thead>
                        <tbody id="defEndpointRows">
                            ${Object.entries(endpoints).map(([ep, info]) => `
                                <tr data-ep="${ep}">
                                    <td><code>${ep}</code></td>
                                    <td>
                                        <select class="form-select form-select-sm def-ep-role">
                                            ${['button', 'rotary', 'toggle', 'switch', 'sensor', 'unknown']
                                                .map(r => `<option ${info.role === r ? 'selected' : ''}>${r}</option>`)
                                                .join('')}
                                        </select>
                                    </td>
                                    <td><input type="text" class="form-control form-control-sm def-ep-label"
                                              value="${info.label || ''}"></td>
                                    <td><input type="text" class="form-control form-control-sm def-ep-group"
                                              value="${info.group || ''}"></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>

                <!-- State Mapping -->
                <h6 class="mb-1"><i class="fas fa-exchange-alt me-1"></i> State Mapping</h6>
                <div class="table-responsive">
                    <table class="table table-sm table-bordered mb-2">
                        <thead>
                            <tr>
                                <th style="width:20%">State Key</th>
                                <th style="width:8%">EP</th>
                                <th style="width:10%">Cluster</th>
                                <th style="width:8%">Attr</th>
                                <th style="width:15%">Type</th>
                                <th style="width:30%">Description</th>
                                <th style="width:9%"></th>
                            </tr>
                        </thead>
                        <tbody id="defStateRows">
                            ${Object.entries(stateMapping).map(([key, m]) => _renderStateMappingRow(key, m)).join('')}
                        </tbody>
                    </table>
                    <button class="btn btn-outline-primary btn-sm" onclick="window._matterAddStateRow()">
                        <i class="fas fa-plus me-1"></i> Add Mapping
                    </button>
                </div>

                <!-- JSON view (hidden by default) -->
                <div id="defJsonView" class="d-none mt-2">
                    <textarea class="form-control form-control-sm font-monospace" id="defJsonText"
                              rows="15" style="font-size:0.75rem"></textarea>
                    <button class="btn btn-sm btn-outline-primary mt-1" onclick="window._matterApplyJson()">
                        Apply JSON
                    </button>
                </div>
            </div>
        </div>
    `;
}

function _renderStateMappingRow(key, m) {
    const types = ['raw', 'position', 'battery', 'boolean', 'on_off', 'temperature', 'percentage'];
    return `
        <tr>
            <td><input type="text" class="form-control form-control-sm def-sm-key" value="${key}"></td>
            <td><input type="number" class="form-control form-control-sm def-sm-ep" value="${m.ep || 0}" style="width:50px"></td>
            <td><input type="number" class="form-control form-control-sm def-sm-cluster" value="${m.cluster || 0}" style="width:60px"></td>
            <td><input type="number" class="form-control form-control-sm def-sm-attr" value="${m.attr || 0}" style="width:50px"></td>
            <td>
                <select class="form-select form-select-sm def-sm-type">
                    ${types.map(t => `<option ${(m.type || 'raw') === t ? 'selected' : ''}>${t}</option>`).join('')}
                </select>
            </td>
            <td><input type="text" class="form-control form-control-sm def-sm-desc" value="${m.description || ''}"></td>
            <td>
                <button class="btn btn-outline-danger btn-sm py-0" onclick="this.closest('tr').remove()">
                    <i class="fas fa-trash"></i>
                </button>
            </td>
        </tr>
    `;
}

// ============================================================================
// STATE MAPPING CRUD
// ============================================================================

window._matterAddStateRow = function () {
    const tbody = document.getElementById('defStateRows');
    if (!tbody) return;
    const row = document.createElement('tr');
    row.innerHTML = _renderStateMappingRow('new_key', { ep: 1, cluster: 59, attr: 1, type: 'position', description: '' });
    // Extract inner content (the row already wraps in <tr>)
    const temp = document.createElement('tbody');
    temp.innerHTML = _renderStateMappingRow('new_key', { ep: 1, cluster: 59, attr: 1, type: 'position', description: '' });
    tbody.appendChild(temp.firstElementChild);
};

// ============================================================================
// JSON TOGGLE
// ============================================================================

window._matterToggleJson = function () {
    const jsonView = document.getElementById('defJsonView');
    if (!jsonView) return;

    if (jsonView.classList.contains('d-none')) {
        const defn = _collectDefFromForm();
        document.getElementById('defJsonText').value = JSON.stringify(defn, null, 2);
        jsonView.classList.remove('d-none');
    } else {
        jsonView.classList.add('d-none');
    }
};

window._matterApplyJson = function () {
    try {
        const text = document.getElementById('defJsonText').value;
        const defn = JSON.parse(text);
        _currentDraft = defn;
        const editor = document.getElementById('matterDefEditor');
        if (editor) _renderDefEditor(editor, defn, !!_existingDef);
    } catch (e) {
        alert('Invalid JSON: ' + e.message);
    }
};

// ============================================================================
// COLLECT FORM DATA
// ============================================================================

function _collectDefFromForm() {
    const defn = {
        vendor_id: parseInt(document.getElementById('defVendorId')?.value || '0'),
        product_id: document.getElementById('defProductId')?.value || '',
        model: document.getElementById('defModel')?.value || '',
        manufacturer: _currentDraft?.manufacturer || '',
        device_type: document.getElementById('defDeviceType')?.value || 'Matter',
        endpoints: {},
        state_mapping: {},
        capabilities: _currentDraft?.capabilities || ['matter'],
    };

    // Collect endpoints
    document.querySelectorAll('#defEndpointRows tr').forEach(row => {
        const ep = row.dataset.ep;
        if (!ep) return;
        defn.endpoints[ep] = {
            role: row.querySelector('.def-ep-role')?.value || 'unknown',
            label: row.querySelector('.def-ep-label')?.value || '',
            group: row.querySelector('.def-ep-group')?.value || '',
        };
    });

    // Collect state mappings
    document.querySelectorAll('#defStateRows tr').forEach(row => {
        const key = row.querySelector('.def-sm-key')?.value;
        if (!key) return;
        defn.state_mapping[key] = {
            ep: parseInt(row.querySelector('.def-sm-ep')?.value || '0'),
            cluster: parseInt(row.querySelector('.def-sm-cluster')?.value || '0'),
            attr: parseInt(row.querySelector('.def-sm-attr')?.value || '0'),
            type: row.querySelector('.def-sm-type')?.value || 'raw',
            description: row.querySelector('.def-sm-desc')?.value || '',
        };
    });

    // Derive capabilities from roles
    const caps = new Set(['matter']);
    Object.values(defn.endpoints).forEach(ep => {
        if (ep.role === 'button') caps.add('button');
        if (ep.role === 'rotary') caps.add('rotary');
        if (ep.role === 'toggle') caps.add('switch');
    });
    // Keep battery if present in state_mapping
    if ('battery' in defn.state_mapping) caps.add('battery');
    defn.capabilities = [...caps].sort();

    return defn;
}

// ============================================================================
// SAVE DEFINITION
// ============================================================================

window._matterSaveDef = async function () {
    const defn = _collectDefFromForm();

    if (!defn.vendor_id || !defn.product_id) {
        alert('Vendor ID and Product ID are required.');
        return;
    }

    try {
        const res = await fetch('/api/matter/definitions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ definition: defn }),
        });
        const data = await res.json();

        if (data.success) {
            _existingDef = defn;
            alert(`Definition saved: ${data.filename}\n\nRestart the container or reload definitions for the parser to pick it up.`);

            // Reload definitions on server
            await fetch('/api/matter/definitions/reload', { method: 'POST' });
        } else {
            alert('Save failed: ' + (data.detail || data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

// ============================================================================
// CHECK EXISTING DEFINITION
// ============================================================================

async function _checkExistingDefinition(nodeId) {
    try {
        const res = await fetch(`/api/matter/nodes/${nodeId}/info`);
        const info = await res.json();
        if (!info.success) return;

        // Check if a definition is loaded for this device
        if (info.state?.definition) {
            // Definition parser is active — fetch it
            const listRes = await fetch('/api/matter/definitions');
            const listData = await listRes.json();
            if (listData.success) {
                for (const d of listData.definitions) {
                    if (d.product_id === info.state.definition) {
                        const detailRes = await fetch(`/api/matter/definitions/${d.filename}`);
                        const detailData = await detailRes.json();
                        if (detailData.success) {
                            _existingDef = detailData.definition;
                        }
                        break;
                    }
                }
            }
        }
    } catch (e) {
        // Non-fatal
    }
}