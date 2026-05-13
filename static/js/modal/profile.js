/**
 * Device Profile Tab
 * ------------------
 * Replaces the old Mappings tab. Provides three sub-views:
 *
 *   1. DISCOVER — full-spectrum live cluster introspection. Shows the
 *      device's endpoints / clusters / attributes / commands with
 *      readable/writable/reportable badges, and per-attribute live values.
 *
 *   2. MAP     — for each raw attribute the device is reporting, an inline
 *      editor: friendly name, scale, unit, device class, invert flag,
 *      value map. Mappings save to the IEEE override; "Promote" copies
 *      them into a model-level profile.
 *
 *   3. ASSEMBLE — pick a device type, capabilities, actions, reporting.
 *      Save as a profile that auto-applies to any device of this model.
 *
 * The same tab handles Zigbee and Matter devices — the backend hides the
 * difference behind ``/api/profiles/device/{ieee}``.
 *
 * Location: static/js/modal/profile.js
 */

import { state } from '../state.js';

// ---------------------------------------------------------------------------
// Module-level cache for the currently-open device
// ---------------------------------------------------------------------------

let _data = null;          // last response from /api/profiles/device/{ieee}
let _draft = null;         // profile draft being edited in Assemble view
let _activeSubview = 'map'; // 'discover' | 'map' | 'assemble'
let _deviceTypes = null;   // cached /api/profiles/device_types result

// ---------------------------------------------------------------------------
// Render entry points (called from device-modal.js)
// ---------------------------------------------------------------------------

export function renderProfileTab(device) {
    return `
        <div id="profileTabContent" data-ieee="${device?.ieee || ''}">
            <div class="text-center text-muted p-3">
                <i class="fas fa-spinner fa-spin"></i> Loading device profile…
            </div>
        </div>
    `;
}

export async function initProfileTab(ieee) {
    const root = document.getElementById('profileTabContent');
    if (!root) return;
    root.dataset.ieee = ieee;

    try {
        if (!_deviceTypes) {
            const r = await fetch('/api/profiles/device_types');
            const d = await r.json();
            _deviceTypes = (d?.types || []);
        }
        const r = await fetch(`/api/profiles/device/${encodeURIComponent(ieee)}`);
        _data = await r.json();
        if (!_data?.success) {
            root.innerHTML = `<div class="alert alert-danger">${_data?.error || 'Failed to load'}</div>`;
            return;
        }
        _draft = _data.profile ? _cloneDraft(_data.profile) : _newDraft(_data);
        _render(root, ieee);
    } catch (e) {
        root.innerHTML = `<div class="alert alert-danger">Error: ${_esc(e.message)}</div>`;
    }
}

/**
 * Are there any cluster_* keys in the device state that aren't mapped?
 * Used by device-modal to decide whether to show a "needs attention" dot.
 */
export function hasUnmappedKeys(device) {
    if (!device?.state) return false;
    return Object.keys(device.state).some(k => k.startsWith('cluster_'));
}

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

function _newDraft(data) {
    const ident = data.identity || {};
    return {
        id:           _slug(ident.model || ident.product_id || 'new_profile'),
        protocol:     ident.protocol || 'zigbee',
        match: {
            model:        ident.model || '',
            manufacturer: ident.manufacturer || '',
            vendor_id:    ident.vendor_id || null,
            product_id:   ident.product_id || null,
        },
        device_type:  'generic',
        capabilities: [],
        endpoints:    {},
        actions:      [],
        reporting:    [],
        meta:         { source: 'user' },
    };
}

function _cloneDraft(p) {
    return JSON.parse(JSON.stringify(p));
}

// ---------------------------------------------------------------------------
// Top-level render
// ---------------------------------------------------------------------------

function _render(root, ieee) {
    const { identity, profile, ieee_pin } = _data;
    const friendlyType = profile
        ? `${profile.device_type} · ${profile.protocol}`
        : 'No profile applied';
    const matchLine = profile
        ? `Matched <code>${_esc(profile.id)}</code> ${profile.meta?.source === 'bundled' ? '(bundled)' : '(user)'}`
        : 'This device is using built-in handlers only. Map attributes below or build a profile.';

    root.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h6 class="mb-0">${_esc(identity.model || '?')} <small class="text-muted">·</small> <small class="text-muted">${_esc(identity.manufacturer || '?')}</small></h6>
                <div class="small text-muted">${matchLine}</div>
            </div>
            <div>
                <span class="badge bg-secondary">${_esc(friendlyType)}</span>
                ${ieee_pin ? '<span class="badge bg-warning text-dark ms-1" title="This device is pinned to a specific profile, overriding model match">PINNED</span>' : ''}
            </div>
        </div>

        <ul class="nav nav-pills nav-justified mb-3" id="profileSubNav" role="tablist">
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'discover' ? 'active' : ''}" data-sub="discover"><i class="fas fa-search"></i> Discover</button></li>
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'map' ? 'active' : ''}"      data-sub="map"><i class="fas fa-tags"></i> Map</button></li>
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'assemble' ? 'active' : ''}" data-sub="assemble"><i class="fas fa-cube"></i> Assemble</button></li>
        </ul>

        <div id="profileSubBody"></div>
    `;

    root.querySelectorAll('#profileSubNav button').forEach(btn => {
        btn.onclick = () => {
            _activeSubview = btn.dataset.sub;
            _render(root, ieee);
        };
    });

    const body = root.querySelector('#profileSubBody');
    if (_activeSubview === 'discover') _renderDiscover(body, ieee);
    if (_activeSubview === 'map')      _renderMap(body, ieee);
    if (_activeSubview === 'assemble') _renderAssemble(body, ieee);
}

// ===========================================================================
// SUBVIEW 1: DISCOVER
// ===========================================================================

function _renderDiscover(container, ieee) {
    const topo = _data.topology || { endpoints: {} };
    const epIds = Object.keys(topo.endpoints || {}).sort((a, b) => parseInt(a) - parseInt(b));

    let html = `
        <div class="d-flex justify-content-between align-items-center mb-2">
            <div class="small text-muted">Live tree of the device's endpoints, clusters, attributes and commands.</div>
            <button class="btn btn-sm btn-outline-primary" id="profileIntrospectBtn">
                <i class="fas fa-sync"></i> Full introspection
            </button>
        </div>
        <div id="profileIntrospectStatus" class="mb-2"></div>
    `;

    if (!epIds.length) {
        html += `<div class="alert alert-warning">No topology cached yet for this device. Hit "Full introspection" to interrogate it.</div>`;
    } else {
        html += '<div class="accordion" id="profileEpAccordion">';
        for (const epId of epIds) {
            const ep = topo.endpoints[epId];
            const clusterIds = Object.keys(ep.clusters || {}).sort();
            html += `
                <div class="accordion-item">
                    <h2 class="accordion-header">
                        <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#profileEp${epId}">
                            <strong>Endpoint ${_esc(epId)}</strong>
                            <span class="badge bg-secondary ms-2">${clusterIds.length} clusters</span>
                        </button>
                    </h2>
                    <div id="profileEp${epId}" class="accordion-collapse collapse" data-bs-parent="#profileEpAccordion">
                        <div class="accordion-body p-2">
                            ${_renderClusterList(ep, epId)}
                        </div>
                    </div>
                </div>
            `;
        }
        html += '</div>';
    }

    container.innerHTML = html;
    const btn = container.querySelector('#profileIntrospectBtn');
    if (btn) btn.onclick = () => _runIntrospection(ieee);
}

function _renderClusterList(ep, epId) {
    const clusterIds = Object.keys(ep.clusters || {}).sort();
    if (!clusterIds.length) return '<div class="small text-muted">No clusters cached.</div>';
    let out = '<div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Cluster</th><th>Attrs</th><th>Dir</th></tr></thead><tbody>';
    for (const cid of clusterIds) {
        const cl = ep.clusters[cid];
        const attrCount = Object.keys(cl.attributes || {}).length;
        out += `
            <tr style="cursor:pointer" data-ep="${epId}" data-cluster="${cid}" class="profile-cluster-row">
                <td><code>${_esc(cid)}</code> <small class="text-muted">${_esc(cl.name || '')}</small></td>
                <td>${attrCount}</td>
                <td>${_esc(cl.direction || 'in')}</td>
            </tr>
            <tr class="profile-cluster-detail d-none" data-detail-ep="${epId}" data-detail-cluster="${cid}">
                <td colspan="3" class="bg-light">${_renderAttrTable(cl, epId, cid)}</td>
            </tr>
        `;
    }
    out += '</tbody></table></div>';
    // Defer click binding (innerHTML hasn't been written yet)
    setTimeout(() => {
        document.querySelectorAll('.profile-cluster-row').forEach(r => {
            r.onclick = () => {
                const detail = document.querySelector(
                    `.profile-cluster-detail[data-detail-ep="${r.dataset.ep}"][data-detail-cluster="${r.dataset.cluster}"]`
                );
                if (detail) detail.classList.toggle('d-none');
            };
        });
    }, 0);
    return out;
}

function _renderAttrTable(cluster, epId, clusterId) {
    const attrIds = Object.keys(cluster.attributes || {}).sort();
    if (!attrIds.length) return '<div class="small text-muted p-2">No attributes cached on this cluster.</div>';
    let out = '<table class="table table-sm table-borderless mb-0"><thead><tr><th>Attr</th><th>Name</th><th>Type</th><th>Value</th><th class="text-end">Access</th></tr></thead><tbody>';
    for (const aid of attrIds) {
        const a = cluster.attributes[aid];
        const access = [
            a.readable ? '<span class="badge bg-success">R</span>' : '',
            a.writable ? '<span class="badge bg-primary">W</span>' : '',
            a.reportable === true ? '<span class="badge bg-info">Report</span>' : '',
        ].filter(Boolean).join(' ');
        out += `
            <tr>
                <td><code>${_esc(aid)}</code></td>
                <td>${_esc(a.name || '')}</td>
                <td><small>${_esc(a.type || '')}</small></td>
                <td><small><code>${_esc(_fmtVal(a.value))}</code></small></td>
                <td class="text-end">${access || '<small class="text-muted">?</small>'}</td>
            </tr>
        `;
    }
    out += '</tbody></table>';
    return out;
}

async function _runIntrospection(ieee) {
    const status = document.getElementById('profileIntrospectStatus');
    const btn = document.getElementById('profileIntrospectBtn');
    if (btn) { btn.disabled = true; }
    if (status) status.innerHTML = '<div class="alert alert-info py-2 small"><i class="fas fa-spinner fa-spin"></i> Walking every cluster — about 1 second per cluster, plus per-attribute reads. Battery devices may take a minute or two.</div>';
    try {
        const r = await fetch(`/api/profiles/introspect/${encodeURIComponent(ieee)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pace_seconds: 1.0 }),
        });
        const d = await r.json();
        const okCount = d.ok_count ?? (d.results?.length || 0);
        const errCount = d.error_count ?? (d.errors?.length || 0);

        let html;
        if (d.success) {
            html = `<div class="alert alert-success py-2 small">
                Introspection finished — ${okCount} cluster${okCount === 1 ? '' : 's'} interrogated.
            </div>`;
        } else if (okCount > 0) {
            html = `<div class="alert alert-warning py-2 small">
                <strong>Partial success:</strong> ${okCount} OK, ${errCount} failed.
                ${_renderIntrospectErrors(d.errors || [])}
            </div>`;
        } else {
            html = `<div class="alert alert-danger py-2 small">
                <strong>Introspection failed for all ${errCount} clusters.</strong>
                Most likely the device is asleep (battery sensors / TRVs) or out of range.
                Wake it (press a button / open the cover) and try again, or use the
                tree below to introspect one cluster at a time.
                ${_renderIntrospectErrors(d.errors || [])}
            </div>`;
        }
        if (status) status.innerHTML = html;
        // Always reload the topology — even partial results are worth showing
        await initProfileTab(ieee);
    } catch (e) {
        if (status) status.innerHTML = `<div class="alert alert-danger py-2 small">${_esc(e.message)}</div>`;
    } finally {
        if (btn) btn.disabled = false;
    }
}

function _renderIntrospectErrors(errors) {
    if (!errors || !errors.length) return '';
    // Group errors by message so a wall of timeouts collapses to a single row
    const groups = new Map();
    for (const e of errors) {
        const key = e.error || 'unknown';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(`EP${e.ep}/${e.cluster}`);
    }
    const rows = [...groups.entries()].map(([msg, where]) => `
        <li><strong>${_esc(msg)}</strong> — ${_esc(where.join(', '))}</li>
    `).join('');
    return `
        <details class="mt-2">
            <summary class="small">Show ${errors.length} error${errors.length === 1 ? '' : 's'}</summary>
            <ul class="mt-1 small mb-0">${rows}</ul>
        </details>
    `;
}

// ===========================================================================
// SUBVIEW 2: MAP
// ===========================================================================

function _renderMap(container, ieee) {
    const mappings = _data.ieee_mappings || {};
    const unmapped = _data.unmapped_keys || [];
    const rawState = _data.raw_state || {};

    let html = `<div class="small text-muted mb-2">Map raw cluster attributes to friendly names. Changes save instantly and apply to this device only. Use "Promote to profile" below to make them apply to every device of this model.</div>`;

    // ── Active mappings ──
    const mapped = Object.keys(mappings);
    if (mapped.length) {
        html += `
            <div class="card mb-3">
                <div class="card-header bg-light d-flex justify-content-between">
                    <span><i class="fas fa-tags"></i> <strong>Mapped (${mapped.length})</strong></span>
                </div>
                <div class="table-responsive">
                    <table class="table table-sm mb-0">
                        <thead><tr><th>Raw key</th><th>Name</th><th>Scale</th><th>Unit</th><th>Class</th><th>Inv</th><th></th></tr></thead>
                        <tbody>
                            ${mapped.map(k => _renderMappedRow(k, mappings[k], ieee)).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    }

    // ── Unmapped ──
    if (unmapped.length) {
        html += `
            <div class="card mb-3">
                <div class="card-header bg-light"><i class="fas fa-question-circle"></i> <strong>Unmapped raw keys (${unmapped.length})</strong></div>
                <div class="table-responsive">
                    <table class="table table-sm mb-0">
                        <thead><tr><th>Raw key</th><th>Name</th><th>Current value</th><th class="text-end"></th></tr></thead>
                        <tbody>
                            ${unmapped.map(k => {
                                const friendly = (_data.friendly_labels || {})[k] || '';
                                return `
                                    <tr>
                                        <td><code>${_esc(k)}</code></td>
                                        <td><strong>${_esc(friendly)}</strong></td>
                                        <td><small><code>${_esc(_fmtVal(rawState[k]))}</code></small></td>
                                        <td class="text-end">
                                            <button class="btn btn-sm btn-outline-primary"
                                                    onclick="window._profileOpenMapDialog('${ieee}','${k}')">
                                                <i class="fas fa-plus"></i> Map
                                            </button>
                                        </td>
                                    </tr>
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    } else if (!mapped.length) {
        html += `<div class="alert alert-info">Nothing to map — this device has no raw <code>cluster_*</code> keys yet. Trigger it (open a contact, press a button, etc.) and re-open this tab.</div>`;
    }

    // ── Promote button ──
    if (mapped.length) {
        html += `
            <div class="d-grid gap-2 d-md-flex justify-content-md-end">
                <button class="btn btn-success" onclick="window._profilePromoteToModel('${ieee}')">
                    <i class="fas fa-cube"></i> Promote to model profile
                </button>
            </div>
        `;
    }

    container.innerHTML = html;
}

function _renderMappedRow(rawKey, mapping, ieee) {
    const m = typeof mapping === 'string' ? { name: mapping } : (mapping || {});
    const friendly = (_data.friendly_labels || {})[rawKey] || '';
    return `
        <tr>
            <td>
                <code>${_esc(rawKey)}</code>
                ${friendly ? `<br><small class="text-muted">${_esc(friendly)}</small>` : ''}
            </td>
            <td><strong>${_esc(m.name || '')}</strong></td>
            <td>${m.scale || ''}</td>
            <td>${_esc(m.unit || '')}</td>
            <td>${_esc(m.device_class || '')}</td>
            <td>${m.invert ? '<i class="fas fa-check text-success"></i>' : ''}</td>
            <td class="text-end">
                <button class="btn btn-sm btn-outline-secondary" onclick="window._profileOpenMapDialog('${ieee}','${rawKey}', true)">
                    <i class="fas fa-pen"></i>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="window._profileRemoveMap('${ieee}','${rawKey}')">
                    <i class="fas fa-times"></i>
                </button>
            </td>
        </tr>
    `;
}

// ── Map dialog (single attribute) ────────────────────────────────────────────

window._profileOpenMapDialog = function(ieee, rawKey, editing) {
    const existing = (_data.ieee_mappings || {})[rawKey] || {};
    const m = typeof existing === 'string' ? { name: existing } : existing;

    // Suggest a name based on raw key heuristics
    const suggestion = _suggestName(rawKey);

    let modal = document.getElementById('profileMapDialog');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div class="modal fade" id="profileMapDialog" tabindex="-1">
                <div class="modal-dialog">
                    <div class="modal-content">
                        <div class="modal-header"><h5 class="modal-title">Map attribute</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                        <div class="modal-body"></div>
                        <div class="modal-footer">
                            <button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                            <button class="btn btn-primary" id="profileMapSave">Save</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap);
        modal = document.getElementById('profileMapDialog');
    }
    modal.querySelector('.modal-body').innerHTML = `
        <div class="mb-2"><small class="text-muted">Raw key:</small> <code>${_esc(rawKey)}</code></div>
        <div class="mb-2">
            <label class="form-label small">Friendly name</label>
            <input type="text" class="form-control form-control-sm" id="pmapName" value="${_esc(m.name || suggestion)}" placeholder="e.g. temperature, contact, battery_remaining">
        </div>
        <div class="row g-2">
            <div class="col-4">
                <label class="form-label small">Scale (÷)</label>
                <input type="number" step="any" class="form-control form-control-sm" id="pmapScale" value="${m.scale ?? ''}" placeholder="1">
            </div>
            <div class="col-4">
                <label class="form-label small">Unit</label>
                <input type="text" class="form-control form-control-sm" id="pmapUnit" value="${_esc(m.unit || '')}" placeholder="°C, %, V…">
            </div>
            <div class="col-4">
                <label class="form-label small">Device class</label>
                <input type="text" class="form-control form-control-sm" id="pmapClass" value="${_esc(m.device_class || '')}" placeholder="door, temperature…">
            </div>
        </div>
        <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" id="pmapInvert" ${m.invert ? 'checked' : ''}>
            <label class="form-check-label small" for="pmapInvert">Invert value (for booleans that report "0 = open")</label>
        </div>
    `;
    modal.querySelector('#profileMapSave').onclick = async () => {
        const body = {
            ieee,
            raw_key:       rawKey,
            friendly_name: modal.querySelector('#pmapName').value.trim(),
            scale:         parseFloat(modal.querySelector('#pmapScale').value) || 1,
            unit:          modal.querySelector('#pmapUnit').value.trim(),
            device_class:  modal.querySelector('#pmapClass').value.trim(),
            invert:        modal.querySelector('#pmapInvert').checked,
        };
        if (!body.friendly_name) { alert('Friendly name required'); return; }
        try {
            const r = await fetch('/api/profiles/ieee_mapping', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const d = await r.json();
            if (d.success) {
                bootstrap.Modal.getInstance(modal)?.hide();
                await initProfileTab(ieee);
            } else {
                alert(d.error || 'Save failed');
            }
        } catch (e) { alert(e.message); }
    };
    new bootstrap.Modal(modal).show();
};

window._profileRemoveMap = async function(ieee, rawKey) {
    if (!confirm(`Remove mapping for ${rawKey}?`)) return;
    const r = await fetch('/api/profiles/ieee_mapping', {
        method:  'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ ieee, raw_key: rawKey }),
    });
    const d = await r.json();
    if (d.success) await initProfileTab(ieee);
    else alert(d.error || 'Failed');
};

// ── Promote IEEE mappings to a model profile ────────────────────────────────

window._profilePromoteToModel = async function(ieee) {
    const ident = _data.identity || {};
    if (!ident.model && !ident.product_id) {
        alert('Device has no model identifier — cannot create a profile.');
        return;
    }
    if (!confirm(
        `Create a model profile for "${ident.model || ident.product_id}"?\n\n`
        + `All matching devices will automatically use these mappings.`
    )) return;

    // Build a fresh profile draft from the current IEEE mappings
    const ieee_mappings = _data.ieee_mappings || {};
    const endpoints = { '1': { clusters: {}, role: 'primary' } };
    for (const [rawKey, mapping] of Object.entries(ieee_mappings)) {
        const m = rawKey.match(/cluster_([0-9a-f]+)_attr_([0-9a-f]+)/);
        if (!m) continue;
        const clusterHex = `0x${m[1].toUpperCase()}`;
        const attrHex    = `0x${m[2].toUpperCase()}`;
        const cl = endpoints['1'].clusters[clusterHex] ||= { attributes: {} };
        cl.attributes[attrHex] = (typeof mapping === 'string') ? { name: mapping } : mapping;
    }

    const profile = {
        id:          _slug(ident.model || ident.product_id),
        protocol:    ident.protocol || 'zigbee',
        match: {
            model:        ident.model || '',
            manufacturer: ident.manufacturer || '',
            vendor_id:    ident.vendor_id || null,
            product_id:   ident.product_id || null,
        },
        device_type:  'generic',
        endpoints,
    };

    try {
        const r = await fetch('/api/profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profile),
        });
        const d = await r.json();
        if (!d.success) { alert(d.error || 'Save failed'); return; }
        // Apply immediately
        await fetch(`/api/profiles/apply/${encodeURIComponent(ieee)}`, { method: 'POST' });
        await initProfileTab(ieee);
        // Jump to Assemble so the user can refine the new profile
        _activeSubview = 'assemble';
        _render(document.getElementById('profileTabContent'), ieee);
    } catch (e) { alert(e.message); }
};

// ===========================================================================
// SUBVIEW 3: ASSEMBLE
// ===========================================================================

function _renderAssemble(container, ieee) {
    const types = _deviceTypes || [];
    const d = _draft;

    container.innerHTML = `
        <div class="card mb-3">
            <div class="card-header bg-light"><i class="fas fa-id-card"></i> <strong>Profile</strong></div>
            <div class="card-body">
                <div class="row g-2">
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Profile ID</label>
                        <input class="form-control form-control-sm" id="pdId" value="${_esc(d.id)}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Device type</label>
                        <select class="form-select form-select-sm" id="pdType">
                            ${types.map(t => `<option value="${t.id}" ${t.id === d.device_type ? 'selected' : ''}>${_esc(t.label)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Match model</label>
                        <input class="form-control form-control-sm" id="pdMatchModel" value="${_esc(d.match?.model || '')}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Match manufacturer</label>
                        <input class="form-control form-control-sm" id="pdMatchManuf" value="${_esc(d.match?.manufacturer || '')}">
                    </div>
                    ${d.protocol === 'matter' ? `
                        <div class="col-6">
                            <label class="form-label small">Vendor ID</label>
                            <input type="number" class="form-control form-control-sm" id="pdMatchVendor" value="${d.match?.vendor_id ?? ''}">
                        </div>
                        <div class="col-6">
                            <label class="form-label small">Product ID</label>
                            <input class="form-control form-control-sm" id="pdMatchProd" value="${_esc(d.match?.product_id || '')}">
                        </div>
                    ` : ''}
                    <div class="col-12">
                        <label class="form-label small">Capabilities (comma-separated)</label>
                        <input class="form-control form-control-sm" id="pdCaps" value="${_esc((d.capabilities || []).join(', '))}">
                        <div class="form-text">Suggestions for the chosen type appear automatically when type changes.</div>
                    </div>
                </div>
            </div>
        </div>

        ${_renderAssembleActions(d)}
        ${_renderAssembleReporting(d)}
        ${_renderAssembleAttributes(d)}

        <div class="d-flex justify-content-between mt-3">
            <div>
                <button class="btn btn-outline-secondary btn-sm" id="pdExport">
                    <i class="fas fa-download"></i> Export JSON
                </button>
                <button class="btn btn-outline-secondary btn-sm" id="pdImport">
                    <i class="fas fa-upload"></i> Import JSON
                </button>
            </div>
            <div>
                <button class="btn btn-outline-danger btn-sm" id="pdDelete">
                    <i class="fas fa-trash"></i> Delete profile
                </button>
                <button class="btn btn-primary" id="pdSave">
                    <i class="fas fa-save"></i> Save &amp; apply
                </button>
            </div>
        </div>
    `;

    // Bind events
    container.querySelector('#pdType').onchange = (e) => {
        d.device_type = e.target.value;
        const t = (_deviceTypes || []).find(t => t.id === d.device_type);
        if (t && (!d.capabilities || !d.capabilities.length)) {
            d.capabilities = [...(t.capabilities || [])];
        }
        _renderAssemble(container, ieee);
    };
    container.querySelector('#pdSave').onclick = () => _saveAssemble(ieee, container);
    container.querySelector('#pdDelete').onclick = () => _deleteProfile(ieee);
    container.querySelector('#pdExport').onclick = () => _exportProfile();
    container.querySelector('#pdImport').onclick = () => _importProfile(ieee);

    // Cluster & attribute edit, action add, reporting add, etc.
    _bindAssembleControls(container, ieee);
}

function _renderAssembleActions(d) {
    const rows = (d.actions || []).map((a, i) => `
        <tr>
            <td><input class="form-control form-control-sm pd-act-id" value="${_esc(a.id)}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-act-label" value="${_esc(a.label || '')}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-act-ep" value="${a.ep ?? 1}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-act-cluster" value="${_esc(a.cluster || '')}" data-i="${i}" placeholder="0x0006"></td>
            <td><input class="form-control form-control-sm pd-act-cmd" value="${_esc(a.command || '')}" data-i="${i}" placeholder="0x02"></td>
            <td class="text-end"><button class="btn btn-sm btn-outline-danger pd-act-del" data-i="${i}"><i class="fas fa-times"></i></button></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light d-flex justify-content-between">
                <span><i class="fas fa-bolt"></i> <strong>Actions</strong> <small class="text-muted">— shown on Control tab</small></span>
                <button class="btn btn-sm btn-outline-primary" id="pdActAdd"><i class="fas fa-plus"></i> Add</button>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0">
                    <thead><tr><th>ID</th><th>Label</th><th>EP</th><th>Cluster</th><th>Command</th><th></th></tr></thead>
                    <tbody id="pdActRows">${rows || '<tr><td colspan="6" class="text-center text-muted small">No actions yet.</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    `;
}

function _renderAssembleReporting(d) {
    const rows = (d.reporting || []).map((r, i) => `
        <tr>
            <td><input type="number" class="form-control form-control-sm pd-rep-ep" value="${r.ep ?? 1}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-rep-cluster" value="${_esc(r.cluster || '')}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-rep-attr" value="${_esc(r.attr || '')}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-rep-min" value="${r.min}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-rep-max" value="${r.max}" data-i="${i}"></td>
            <td><input type="number" step="any" class="form-control form-control-sm pd-rep-delta" value="${r.delta}" data-i="${i}"></td>
            <td class="text-end"><button class="btn btn-sm btn-outline-danger pd-rep-del" data-i="${i}"><i class="fas fa-times"></i></button></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light d-flex justify-content-between">
                <span><i class="fas fa-broadcast-tower"></i> <strong>Reporting</strong> <small class="text-muted">— applied on save and on each interview</small></span>
                <button class="btn btn-sm btn-outline-primary" id="pdRepAdd"><i class="fas fa-plus"></i> Add</button>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0">
                    <thead><tr><th>EP</th><th>Cluster</th><th>Attr</th><th>Min (s)</th><th>Max (s)</th><th>Δ</th><th></th></tr></thead>
                    <tbody id="pdRepRows">${rows || '<tr><td colspan="7" class="text-center text-muted small">No reporting configured.</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    `;
}

function _renderAssembleAttributes(d) {
    // Render the cluster/attribute matrix as a flat editable table
    const rows = [];
    for (const [epId, ep] of Object.entries(d.endpoints || {})) {
        for (const [cid, cluster] of Object.entries(ep.clusters || {})) {
            for (const [aid, attr] of Object.entries(cluster.attributes || {})) {
                rows.push({ ep: epId, cluster: cid, attr: aid, ...attr });
            }
        }
    }
    const html = rows.map((r, i) => `
        <tr>
            <td><small><code>EP${r.ep} / ${r.cluster} / ${r.attr}</code></small></td>
            <td><input class="form-control form-control-sm pd-attr-name" data-i="${i}" value="${_esc(r.name || '')}"></td>
            <td><input class="form-control form-control-sm pd-attr-scale" data-i="${i}" value="${r.scale ?? ''}" placeholder="1"></td>
            <td><input class="form-control form-control-sm pd-attr-unit" data-i="${i}" value="${_esc(r.unit || '')}"></td>
            <td><input class="form-control form-control-sm pd-attr-class" data-i="${i}" value="${_esc(r.device_class || '')}"></td>
            <td class="text-center"><input type="checkbox" class="form-check-input pd-attr-inv" data-i="${i}" ${r.invert ? 'checked' : ''}></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light">
                <i class="fas fa-tags"></i> <strong>Attribute mappings</strong>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0">
                    <thead><tr><th>Location</th><th>Name</th><th>Scale</th><th>Unit</th><th>Class</th><th class="text-center">Inv</th></tr></thead>
                    <tbody id="pdAttrRows" data-rows='${JSON.stringify(rows).replace(/'/g, "&#39;")}'>
                        ${html || '<tr><td colspan="6" class="text-center text-muted small">No attributes mapped. Use the Map tab first.</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function _bindAssembleControls(container, ieee) {
    container.querySelector('#pdActAdd')?.addEventListener('click', () => {
        _draft.actions = _draft.actions || [];
        _draft.actions.push({ id: 'new_action', label: 'New action', ep: 1, cluster: '0x0006', command: '0x02' });
        _renderAssemble(container, ieee);
    });
    container.querySelector('#pdRepAdd')?.addEventListener('click', () => {
        _draft.reporting = _draft.reporting || [];
        _draft.reporting.push({ ep: 1, cluster: '0x0402', attr: '0x0000', min: 60, max: 300, delta: 10 });
        _renderAssemble(container, ieee);
    });
    container.querySelectorAll('.pd-act-del').forEach(btn => {
        btn.onclick = () => { _draft.actions.splice(+btn.dataset.i, 1); _renderAssemble(container, ieee); };
    });
    container.querySelectorAll('.pd-rep-del').forEach(btn => {
        btn.onclick = () => { _draft.reporting.splice(+btn.dataset.i, 1); _renderAssemble(container, ieee); };
    });
}

async function _saveAssemble(ieee, container) {
    // Pull all form state back into _draft before saving
    _draft.id          = container.querySelector('#pdId').value.trim();
    _draft.device_type = container.querySelector('#pdType').value;
    _draft.match.model        = container.querySelector('#pdMatchModel').value.trim();
    _draft.match.manufacturer = container.querySelector('#pdMatchManuf').value.trim();
    if (_draft.protocol === 'matter') {
        const v = container.querySelector('#pdMatchVendor')?.value;
        _draft.match.vendor_id  = v ? parseInt(v) : null;
        _draft.match.product_id = container.querySelector('#pdMatchProd')?.value.trim() || null;
    }
    _draft.capabilities = container.querySelector('#pdCaps').value.split(',').map(s => s.trim()).filter(Boolean);

    // Actions
    const actRows = container.querySelectorAll('#pdActRows tr');
    const actions = [];
    actRows.forEach(row => {
        const idInput = row.querySelector('.pd-act-id'); if (!idInput) return;
        actions.push({
            id:      idInput.value.trim(),
            label:   row.querySelector('.pd-act-label').value.trim(),
            ep:      parseInt(row.querySelector('.pd-act-ep').value) || 1,
            cluster: row.querySelector('.pd-act-cluster').value.trim(),
            command: row.querySelector('.pd-act-cmd').value.trim(),
        });
    });
    _draft.actions = actions;

    // Reporting
    const repRows = container.querySelectorAll('#pdRepRows tr');
    const reporting = [];
    repRows.forEach(row => {
        const ep = row.querySelector('.pd-rep-ep'); if (!ep) return;
        reporting.push({
            ep:      parseInt(ep.value) || 1,
            cluster: row.querySelector('.pd-rep-cluster').value.trim(),
            attr:    row.querySelector('.pd-rep-attr').value.trim(),
            min:     parseInt(row.querySelector('.pd-rep-min').value) || 30,
            max:     parseInt(row.querySelector('.pd-rep-max').value) || 600,
            delta:   parseFloat(row.querySelector('.pd-rep-delta').value) || 1,
        });
    });
    _draft.reporting = reporting;

    // Attribute mappings — pull back into endpoints structure
    const rowsJson = container.querySelector('#pdAttrRows')?.dataset.rows;
    if (rowsJson) {
        let baseRows = [];
        try { baseRows = JSON.parse(rowsJson); } catch {}
        const newEndpoints = {};
        baseRows.forEach((r, i) => {
            const name  = container.querySelector(`.pd-attr-name[data-i="${i}"]`)?.value.trim() || '';
            if (!name) return;
            const scale = parseFloat(container.querySelector(`.pd-attr-scale[data-i="${i}"]`)?.value) || 1;
            const unit  = container.querySelector(`.pd-attr-unit[data-i="${i}"]`)?.value.trim() || '';
            const cls   = container.querySelector(`.pd-attr-class[data-i="${i}"]`)?.value.trim() || '';
            const inv   = container.querySelector(`.pd-attr-inv[data-i="${i}"]`)?.checked || false;
            const ep    = newEndpoints[r.ep] ||= { role: 'primary', clusters: {} };
            const cl    = ep.clusters[r.cluster] ||= { attributes: {} };
            const m     = { name };
            if (scale !== 1) m.scale = scale;
            if (unit)        m.unit  = unit;
            if (cls)         m.device_class = cls;
            if (inv)         m.invert = true;
            cl.attributes[r.attr] = m;
        });
        if (Object.keys(newEndpoints).length) _draft.endpoints = newEndpoints;
    }

    try {
        const r = await fetch('/api/profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_draft),
        });
        const d = await r.json();
        if (!d.success) { alert(d.error || 'Save failed'); return; }
        // Apply to this device immediately
        await fetch(`/api/profiles/apply/${encodeURIComponent(ieee)}`, { method: 'POST' });
        await initProfileTab(ieee);
    } catch (e) { alert(e.message); }
}

async function _deleteProfile(ieee) {
    if (!_draft.id) return;
    if (!confirm(`Delete profile "${_draft.id}"?\n\nDevices using it will fall back to built-in handlers.`)) return;
    const r = await fetch(`/api/profiles/${encodeURIComponent(_draft.id)}`, { method: 'DELETE' });
    const d = await r.json();
    if (d.success) await initProfileTab(ieee);
    else alert(d.error || 'Delete failed');
}

async function _exportProfile() {
    if (!_draft?.id) { alert('Save the profile first.'); return; }
    const r = await fetch(`/api/profiles/export/${encodeURIComponent(_draft.id)}`);
    const d = await r.json();
    if (!d.success) { alert(d.error || 'Export failed'); return; }
    const blob = new Blob([JSON.stringify(d.profile, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${d.profile.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
}

function _importProfile(ieee) {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.json,application/json';
    inp.onchange = async () => {
        const f = inp.files?.[0]; if (!f) return;
        const text = await f.text();
        let profile;
        try { profile = JSON.parse(text); } catch (e) { alert('Invalid JSON'); return; }
        const r = await fetch('/api/profiles/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile }),
        });
        const d = await r.json();
        if (!d.success) { alert(d.error || 'Import failed'); return; }
        await initProfileTab(ieee);
    };
    inp.click();
}

// ===========================================================================
// HELPERS
// ===========================================================================

function _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function _fmtVal(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
}

function _slug(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '');
}

function _suggestName(rawKey) {
    const friendly = (_data.friendly_labels || {})[rawKey] || '';
    // Friendly is "Cluster Name · attribute_name" — slugify the attr part
    const parts = friendly.split('·');
    if (parts.length === 2) {
        const slug = _slug(parts[1].trim());
        if (slug) return slug;
    }
    // Legacy fallback
    const m = rawKey.match(/cluster_([0-9a-f]+)_attr_([0-9a-f]+)/);
    if (!m) return '';
    const cid = parseInt(m[1], 16);
    const aid = parseInt(m[2], 16);
    const t = {
        '1024_0': 'illuminance', '1026_0': 'temperature', '1029_0': 'humidity',
        '1030_0': 'occupancy',   '1280_0': 'contact',
        '1_32':   'battery_voltage', '1_33': 'battery_remaining',
        '6_0':    'state',           '8_0':  'brightness',
    };
    return t[`${cid}_${aid}`] || '';
}