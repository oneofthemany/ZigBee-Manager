/**
 * Matter Clusters Tab — endpoint/cluster/attribute browser for Matter devices.
 * Location: static/js/modal/matter-clusters.js
 *
 * The Matter equivalent of clusters.js for Zigbee. Shows all endpoints,
 * clusters, and attributes with their current values. Supports live read
 * and write for writable attributes.
 */

// ============================================================================
// RENDER
// ============================================================================

export function renderMatterClustersTab(device) {
    const nodeId = device.state?.node_id;
    if (!nodeId) return '<div class="alert alert-warning">No Matter node ID.</div>';

    return `
        <div id="matterClustersContent">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h6 class="mb-0"><i class="fas fa-sitemap me-1"></i> Matter Endpoints & Clusters</h6>
                <button class="btn btn-outline-primary btn-sm" onclick="window._matterLoadClusters(${nodeId})">
                    <i class="fas fa-sync-alt me-1"></i> Refresh
                </button>
            </div>
            <div id="matterClustersBody">
                <div class="text-center text-muted py-4">
                    <i class="fas fa-spinner fa-spin"></i> Loading attributes...
                </div>
            </div>
        </div>
    `;
}

export function initMatterClustersTab(nodeId) {
    window._matterLoadClusters(nodeId);
}

// ============================================================================
// LOAD ATTRIBUTES
// ============================================================================

window._matterLoadClusters = async function (nodeId) {
    const body = document.getElementById('matterClustersBody');
    if (!body) return;

    body.innerHTML = '<div class="text-center py-3"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        const res = await fetch(`/api/matter/nodes/${nodeId}/attributes`);
        const data = await res.json();

        if (!data.success) {
            body.innerHTML = `<div class="alert alert-danger small">${data.detail || 'Failed to load attributes'}</div>`;
            return;
        }

        if (data.endpoints.length === 0) {
            body.innerHTML = '<div class="alert alert-info small">No attributes found for this node.</div>';
            return;
        }

        body.innerHTML = `
            <div class="small text-muted mb-2">
                ${data.endpoints.length} endpoint(s), ${data.total_attributes} attribute(s)
            </div>
            <div class="accordion" id="matterEpAccordion">
                ${data.endpoints.map((ep, epIdx) => renderEndpoint(ep, epIdx, nodeId)).join('')}
            </div>
        `;
    } catch (e) {
        body.innerHTML = `<div class="alert alert-danger small">Error: ${e.message}</div>`;
    }
};

// ============================================================================
// RENDER HELPERS
// ============================================================================

function renderEndpoint(ep, epIdx, nodeId) {
    const totalAttrs = ep.clusters.reduce((sum, c) => sum + c.attributes.length, 0);

    return `
        <div class="accordion-item">
            <h2 class="accordion-header">
                <button class="accordion-button ${epIdx > 0 ? 'collapsed' : ''}" type="button"
                        data-bs-toggle="collapse" data-bs-target="#matterEp${ep.endpoint_id}">
                    Endpoint ${ep.endpoint_id}
                    <span class="ms-2 badge bg-primary">${ep.clusters.length} clusters</span>
                    <span class="ms-1 badge bg-secondary">${totalAttrs} attrs</span>
                </button>
            </h2>
            <div id="matterEp${ep.endpoint_id}" class="accordion-collapse collapse ${epIdx === 0 ? 'show' : ''}"
                 data-bs-parent="#matterEpAccordion">
                <div class="accordion-body p-2">
                    ${ep.clusters.map(cluster => renderCluster(cluster, ep.endpoint_id, nodeId)).join('')}
                </div>
            </div>
        </div>
    `;
}

function renderCluster(cluster, epId, nodeId) {
    const isControllable = [6, 8, 768, 513, 258].includes(cluster.cluster_id);
    const headerClass = isControllable ? 'bg-success bg-opacity-10' : 'bg-light';

    return `
        <div class="card mb-2">
            <div class="card-header ${headerClass} py-1 px-2 d-flex justify-content-between align-items-center">
                <span class="small fw-bold">
                    ${isControllable ? '<i class="fas fa-gamepad me-1 text-success"></i>' : ''}
                    ${cluster.cluster_name}
                    <code class="ms-1 text-muted">(${cluster.cluster_id})</code>
                </span>
                <span class="badge bg-secondary">${cluster.attributes.length}</span>
            </div>
            <div class="card-body p-0">
                <table class="table table-sm table-striped mb-0">
                    <thead>
                        <tr class="small">
                            <th style="width:30%">Attribute</th>
                            <th style="width:10%">ID</th>
                            <th style="width:40%">Value</th>
                            <th style="width:10%">Type</th>
                            <th style="width:10%">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${cluster.attributes.map(attr => renderAttribute(attr, epId, cluster.cluster_id, nodeId)).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function renderAttribute(attr, epId, clusterId, nodeId) {
    const value = formatValue(attr.value, attr.type);
    const isWritable = isLikelyWritable(clusterId, attr.attribute_id);
    const pathId = `${epId}-${clusterId}-${attr.attribute_id}`.replace(/\//g, '-');

    return `
        <tr>
            <td class="small">${attr.attribute_name}</td>
            <td class="small"><code>${attr.attribute_id}</code></td>
            <td class="small">
                <span id="mattr-val-${pathId}">${value}</span>
            </td>
            <td class="small text-muted">${attr.type}</td>
            <td class="small">
                <div class="btn-group btn-group-sm">
                    ${isWritable ? `
                        <button class="btn btn-outline-warning py-0 px-1" title="Write"
                                onclick="window._matterWriteAttr(${nodeId}, ${epId}, ${clusterId}, ${attr.attribute_id}, '${pathId}')">
                            <i class="fas fa-pen"></i>
                        </button>
                    ` : ''}
                </div>
            </td>
        </tr>
    `;
}

function formatValue(value, type) {
    if (value === null || value === undefined) return '<span class="text-muted">null</span>';
    if (typeof value === 'boolean') {
        return value
            ? '<span class="badge bg-success">true</span>'
            : '<span class="badge bg-secondary">false</span>';
    }
    if (typeof value === 'string' && value.length > 60) {
        return `<code class="small text-break" title="${value}">${value.substring(0, 60)}...</code>`;
    }
    if (typeof value === 'object') {
        const str = JSON.stringify(value);
        if (str.length > 60) return `<code class="small text-break">${str.substring(0, 60)}...</code>`;
        return `<code class="small">${str}</code>`;
    }
    return `<code>${value}</code>`;
}

function isLikelyWritable(clusterId, attrId) {
    // Known writable attributes
    const writableAttrs = {
        6: [16387],           // OnOff: StartUpOnOff
        8: [16, 17, 16384],   // LevelControl: OnOffTransitionTime, OnLevel, StartUpCurrentLevel
        40: [5, 6],           // BasicInformation: NodeLabel, Location
        768: [16400],         // ColorControl: StartUpColorTemperatureMireds
        513: [17, 18, 27],    // Thermostat: OccupiedHeatingSetpoint, OccupiedCoolingSetpoint, SystemMode
    };
    return writableAttrs[clusterId]?.includes(attrId) || false;
}

// ============================================================================
// WRITE ATTRIBUTE
// ============================================================================

window._matterWriteAttr = async function (nodeId, epId, clusterId, attrId, pathId) {
    const currentEl = document.getElementById(`mattr-val-${pathId}`);
    const currentVal = currentEl ? currentEl.textContent.trim() : '';

    const newVal = prompt(`Write attribute ${attrId} on cluster ${clusterId}:\n\nCurrent value: ${currentVal}\n\nEnter new value:`, currentVal);
    if (newVal === null) return;

    // Try to parse as JSON (for numbers, bools, objects)
    let parsedValue;
    try {
        parsedValue = JSON.parse(newVal);
    } catch {
        parsedValue = newVal; // Keep as string
    }

    try {
        const res = await fetch(`/api/matter/nodes/${nodeId}/write-attribute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                node_id: nodeId,
                endpoint_id: epId,
                cluster_id: clusterId,
                attribute_id: attrId,
                value: parsedValue,
            }),
        });
        const data = await res.json();

        if (data.success) {
            if (currentEl) currentEl.innerHTML = `<code>${JSON.stringify(parsedValue)}</code> <i class="fas fa-check text-success"></i>`;
            // Refresh after a short delay to get the confirmed value
            setTimeout(() => window._matterLoadClusters(nodeId), 2000);
        } else {
            alert(`Write failed: ${data.error}`);
        }
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
};