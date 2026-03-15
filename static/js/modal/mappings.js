/**
 * Device Mappings Tab
 * Location: static/js/modal/mappings.js
 *
 * Allows users to map raw generic cluster keys (cluster_XXXX_attr_XXXX)
 * to friendly names with optional scale, unit, and device_class.
 */

// ============================================================================
// STATE
// ============================================================================
let _mappingsData = null;

// ============================================================================
// RENDER
// ============================================================================

export function renderMappingsTab(device) {
    return `
        <div id="mappingsTabContent">
            <div class="text-center text-muted p-3">
                <i class="fas fa-spinner fa-spin"></i> Loading mappings...
            </div>
        </div>
    `;
}

export async function initMappingsTab(ieee) {
    const container = document.getElementById('mappingsTabContent');
    if (!container) return;

    try {
        const res = await fetch(`/api/device_overrides/${ieee}`);
        _mappingsData = await res.json();

        if (!_mappingsData.success) {
            container.innerHTML = `<div class="alert alert-danger">${_mappingsData.error || 'Failed to load'}</div>`;
            return;
        }

        renderMappingsContent(container, ieee);
    } catch (e) {
        container.innerHTML = `<div class="alert alert-danger">Error: ${e.message}</div>`;
    }
}

function renderMappingsContent(container, ieee) {
    const { unmapped_keys, ieee_mappings, model_definition, model, manufacturer } = _mappingsData;

    // Separate already-mapped keys
    const mappedKeys = Object.keys(ieee_mappings || {});
    const trulyUnmapped = (unmapped_keys || []).filter(k => !mappedKeys.includes(k));

    let html = '';

    // ── Model Definition Status ──
    if (model_definition) {
        html += `
            <div class="alert alert-success py-2 mb-3">
                <i class="fas fa-check-circle"></i>
                <strong>Model definition active</strong> for ${model || 'Unknown'} (${manufacturer || 'Unknown'})
            </div>
        `;
    }

    // ── Existing Mappings ──
    if (mappedKeys.length > 0) {
        html += `
            <div class="card mb-3">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <span class="fw-bold"><i class="fas fa-tags"></i> Active Mappings</span>
                    <span class="badge bg-primary">${mappedKeys.length}</span>
                </div>
                <div class="card-body p-0">
                    <table class="table table-sm table-hover mb-0">
                        <thead><tr>
                            <th>Raw Key</th><th>Friendly Name</th><th>Scale</th><th>Unit</th><th></th>
                        </tr></thead>
                        <tbody>
        `;

        for (const [rawKey, mapping] of Object.entries(ieee_mappings)) {
            const m = typeof mapping === 'string' ? { name: mapping } : mapping;
            html += `
                <tr>
                    <td><code class="small">${rawKey}</code></td>
                    <td><strong>${m.name}</strong></td>
                    <td>${m.scale || 1}</td>
                    <td>${m.unit || '—'}</td>
                    <td class="text-end">
                        <button class="btn btn-sm btn-outline-danger"
                                onclick="window._removeMappingClick('${ieee}', '${rawKey}')">
                            <i class="fas fa-trash"></i>
                        </button>
                    </td>
                </tr>
            `;
        }

        html += `</tbody></table></div></div>`;
    }

    // ── Unmapped Keys ──
    if (trulyUnmapped.length > 0) {
        html += `
            <div class="card mb-3">
                <div class="card-header bg-warning bg-opacity-25 d-flex justify-content-between align-items-center">
                    <span class="fw-bold"><i class="fas fa-question-circle"></i> Unmapped Attributes</span>
                    <span class="badge bg-warning text-dark">${trulyUnmapped.length}</span>
                </div>
                <div class="card-body p-0">
                    <table class="table table-sm table-hover mb-0">
                        <thead><tr>
                            <th>Raw Key</th><th>Current Value</th><th></th>
                        </tr></thead>
                        <tbody>
        `;

        for (const key of trulyUnmapped) {
            html += `
                <tr>
                    <td><code class="small">${key}</code></td>
                    <td class="small text-muted" id="val-${key.replace(/[^a-z0-9_]/g, '_')}">—</td>
                    <td class="text-end">
                        <button class="btn btn-sm btn-outline-primary"
                                onclick="window._openMapDialog('${ieee}', '${key}')">
                            <i class="fas fa-tag"></i> Map
                        </button>
                    </td>
                </tr>
            `;
        }

        html += `</tbody></table></div></div>`;
    }

    // ── No keys at all ──
    if (mappedKeys.length === 0 && trulyUnmapped.length === 0) {
        html += `
            <div class="alert alert-info">
                <i class="fas fa-info-circle"></i>
                No generic cluster attributes detected on this device.
                All clusters have dedicated handlers.
            </div>
        `;
    }

    // ── Model Definition Editor ──
    html += `
        <div class="card">
            <div class="card-header bg-light">
                <span class="fw-bold"><i class="fas fa-file-code"></i> Model Definition</span>
            </div>
            <div class="card-body">
                <p class="small text-muted mb-2">
                    Save current mappings as a model definition so all <strong>${model || 'Unknown'}</strong>
                    devices get the same mappings automatically.
                </p>
                <button class="btn btn-sm btn-outline-success"
                        onclick="window._promoteToModelDef('${ieee}')"
                        ${mappedKeys.length === 0 ? 'disabled' : ''}>
                    <i class="fas fa-upload"></i> Promote to Model Definition
                </button>
            </div>
        </div>
    `;

    container.innerHTML = html;

    // Fill in current values from device state
    _fillCurrentValues(ieee, trulyUnmapped);
}

// ============================================================================
// CURRENT VALUE DISPLAY
// ============================================================================

function _fillCurrentValues(ieee, keys) {
    // Try to get current device state from the cache
    try {
        const devState = window._getDeviceState?.(ieee) || {};
        for (const key of keys) {
            const el = document.getElementById(`val-${key.replace(/[^a-z0-9_]/g, '_')}`);
            if (el && devState[key] !== undefined) {
                el.textContent = JSON.stringify(devState[key]);
            }
        }
    } catch (e) { /* silent */ }
}

// ============================================================================
// MAPPING DIALOG
// ============================================================================

window._openMapDialog = function(ieee, rawKey) {
    // Parse cluster/attr from key for context
    const parts = rawKey.match(/cluster_([0-9a-f]+)_attr_([0-9a-f]+)/);
    const clusterHex = parts ? `0x${parts[1].toUpperCase()}` : '?';
    const attrHex = parts ? `0x${parts[2].toUpperCase()}` : '?';

    const html = `
        <div class="modal fade" id="mapAttrModal" tabindex="-1">
            <div class="modal-dialog modal-sm">
                <div class="modal-content">
                    <div class="modal-header">
                        <h6 class="modal-title"><i class="fas fa-tag"></i> Map Attribute</h6>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="mb-2 small">
                            <strong>Cluster:</strong> ${clusterHex} &nbsp;
                            <strong>Attribute:</strong> ${attrHex}
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Friendly Name <span class="text-danger">*</span></label>
                            <input type="text" id="mapName" class="form-control form-control-sm"
                                   placeholder="e.g. temperature, contact, humidity">
                        </div>
                        <div class="row mb-3">
                            <div class="col-6">
                                <label class="form-label">Scale (divisor)</label>
                                <input type="number" id="mapScale" class="form-control form-control-sm"
                                       value="1" step="any" title="Raw value will be divided by this">
                            </div>
                            <div class="col-6">
                                <label class="form-label">Unit</label>
                                <input type="text" id="mapUnit" class="form-control form-control-sm"
                                       placeholder="°C, %, lux">
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Device Class (HA)</label>
                            <select id="mapDeviceClass" class="form-select form-select-sm">
                                <option value="">None</option>
                                <option value="temperature">Temperature</option>
                                <option value="humidity">Humidity</option>
                                <option value="illuminance">Illuminance</option>
                                <option value="pressure">Pressure</option>
                                <option value="battery">Battery</option>
                                <option value="power">Power</option>
                                <option value="energy">Energy</option>
                                <option value="voltage">Voltage</option>
                                <option value="current">Current</option>
                                <option value="carbon_dioxide">CO₂</option>
                                <option value="pm25">PM2.5</option>
                                <option value="motion">Motion</option>
                                <option value="door">Door</option>
                                <option value="window">Window</option>
                            </select>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button class="btn btn-sm btn-primary" onclick="window._saveMapping('${ieee}', '${rawKey}')">
                            <i class="fas fa-save"></i> Save
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove any existing dialog
    document.getElementById('mapAttrModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', html);
    new bootstrap.Modal(document.getElementById('mapAttrModal')).show();
};

// ============================================================================
// SAVE / REMOVE MAPPING
// ============================================================================

window._saveMapping = async function(ieee, rawKey) {
    const name = document.getElementById('mapName')?.value?.trim();
    if (!name) {
        alert('Friendly name is required');
        return;
    }

    const scale = parseFloat(document.getElementById('mapScale')?.value) || 1;
    const unit = document.getElementById('mapUnit')?.value?.trim() || '';
    const deviceClass = document.getElementById('mapDeviceClass')?.value || '';

    try {
        const res = await fetch('/api/device_overrides/ieee_mapping', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ieee, raw_key: rawKey,
                friendly_name: name,
                scale, unit, device_class: deviceClass
            })
        });
        const data = await res.json();

        if (data.success) {
            bootstrap.Modal.getInstance(document.getElementById('mapAttrModal'))?.hide();
            // Refresh mappings tab
            await initMappingsTab(ieee);
        } else {
            alert('Failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

window._removeMappingClick = async function(ieee, rawKey) {
    if (!confirm(`Remove mapping for ${rawKey}?`)) return;

    try {
        const res = await fetch('/api/device_overrides/ieee_mapping', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, raw_key: rawKey })
        });
        const data = await res.json();

        if (data.success) {
            await initMappingsTab(ieee);
        } else {
            alert('Failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

// ============================================================================
// PROMOTE TO MODEL DEFINITION
// ============================================================================

window._promoteToModelDef = async function(ieee) {
    if (!_mappingsData) return;

    const { model, manufacturer, ieee_mappings } = _mappingsData;

    if (!model) {
        alert('Device has no model identifier — cannot create model definition.');
        return;
    }

    if (!confirm(
        `This will create a model definition for "${model}" (${manufacturer}).\n\n` +
        `All devices of this model will automatically use these mappings.\nContinue?`
    )) return;

    // Convert IEEE mappings to model definition format
    const clusters = {};
    for (const [rawKey, mapping] of Object.entries(ieee_mappings || {})) {
        const parts = rawKey.match(/cluster_([0-9a-f]+)_attr_([0-9a-f]+)/);
        if (!parts) continue;

        const clusterHex = `0x${parts[1].toUpperCase()}`;
        const attrHex = `0x${parts[2].toUpperCase()}`;

        if (!clusters[clusterHex]) clusters[clusterHex] = { attributes: {} };

        const m = typeof mapping === 'string' ? { name: mapping } : mapping;
        clusters[clusterHex].attributes[attrHex] = m;
    }

    try {
        const res = await fetch('/api/device_overrides/definition', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, manufacturer, definition: { clusters } })
        });
        const data = await res.json();

        if (data.success) {
            alert(`Model definition created for "${model}". All matching devices will use these mappings.`);
            await initMappingsTab(ieee);
        } else {
            alert('Failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
};

// ============================================================================
// HELPER: Check if device has generic/unmapped content
// ============================================================================

export function hasGenericContent(device) {
    if (!device.state) return false;
    return Object.keys(device.state).some(k => k.startsWith('cluster_'));
}