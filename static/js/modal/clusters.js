/**
 * Device Clusters Tab
 * Location: static/js/modal/clusters.js
 */

import { getClusterName } from './config.js';

export function renderCapsTab(device) {
    if (!device.capabilities) return '<div class="alert alert-warning">No capability data.</div>';
    let html = `<div class="accordion" id="epAccordion">`;
    device.capabilities.forEach((ep, idx) => {
        const inputs = (ep.inputs||[]).map(c => {
            const name = getClusterName(c.id, c.name);
            return `<span class="badge bg-primary text-white border m-1" role="button" style="cursor:pointer"
                          title="Click to discover attributes"
                          onclick="window.discoverClusterAttributes('${device.ieee}', ${ep.id}, ${c.id}, this)">
                        <i class="fas fa-arrow-right fa-xs me-1"></i>${name} (0x${c.id.toString(16)})
                    </span>`;
        }).join('');

        const outputs = (ep.outputs||[]).map(c => {
            const name = getClusterName(c.id, c.name);
            return `<span class="badge bg-secondary text-white border m-1" role="button" style="cursor:pointer"
                          title="Click to discover attributes"
                          onclick="window.discoverClusterAttributes('${device.ieee}', ${ep.id}, ${c.id}, this)">
                        <i class="fas fa-arrow-left fa-xs me-1"></i>${name} (0x${c.id.toString(16)})
                    </span>`;
        }).join('');

        html += `
            <div class="accordion-item">
                <h2 class="accordion-header">
                    <button class="accordion-button ${idx !== 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${ep.id}">
                        Endpoint ${ep.id} <span class="ms-2 badge bg-primary">${ep.profile || '?'}</span>
                    </button>
                </h2>
                <div id="collapse${ep.id}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#epAccordion">
                    <div class="accordion-body">
                        <small class="text-muted d-block mb-2"><i class="fas fa-arrow-right"></i> Input Clusters (click to inspect):</small>
                        <div class="d-flex flex-wrap mb-3">${inputs}</div>
                        <small class="text-muted d-block mb-2"><i class="fas fa-arrow-left"></i> Output Clusters:</small>
                        <div class="d-flex flex-wrap mb-3">${outputs}</div>
                        <div id="attr-panel-${ep.id}" class="mt-2"></div>
                    </div>
                </div>
            </div>`;
    });
    html += `</div>`;
    return html;
}

/**
 * Discover attributes on a cluster — on-demand via click
 */
window.discoverClusterAttributes = async function(ieee, epId, clusterId, badgeEl) {
    const panel = document.getElementById(`attr-panel-${epId}`);
    if (!panel) return;

    // Toggle: if same cluster is already shown, collapse
    if (panel.dataset.activeCluster === `${clusterId}`) {
        panel.innerHTML = '';
        panel.dataset.activeCluster = '';
        return;
    }

    panel.dataset.activeCluster = `${clusterId}`;
    panel.innerHTML = `
        <div class="text-center py-3">
            <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
            <span class="ms-2 small text-muted">Discovering attributes on 0x${clusterId.toString(16).padStart(4, '0')}...</span>
        </div>`;

    try {
        const res = await fetch('/api/device/discover_attributes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, endpoint_id: epId, cluster_id: clusterId })
        });
        const data = await res.json();

        if (!data.success) {
            panel.innerHTML = `<div class="alert alert-danger small py-1">${data.error}</div>`;
            return;
        }

        const attrs = data.attributes || [];
        if (attrs.length === 0) {
            panel.innerHTML = `<div class="alert alert-info small py-1">No attributes discovered.</div>`;
            return;
        }

        let tableHtml = `
            <div class="card mt-2">
                <div class="card-header py-1 bg-white d-flex justify-content-between align-items-center">
                    <span class="fw-bold small">
                        <i class="fas fa-list"></i> EP${epId} — ${data.cluster_id}
                        <span class="badge bg-secondary ms-1">${attrs.length} attrs</span>
                    </span>
                    <button class="btn btn-sm btn-outline-secondary py-0" onclick="this.closest('.card').remove(); document.getElementById('attr-panel-${epId}').dataset.activeCluster=''">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                <div class="card-body p-0">
                    <div class="table-responsive" style="max-height: 400px; overflow-y: auto">
                        <table class="table table-sm table-hover mb-0" style="font-size: 0.78rem">
                            <thead class="table-light sticky-top">
                                <tr>
                                    <th>ID</th>
                                    <th>Name</th>
                                    <th>Type</th>
                                    <th class="text-center">Read</th>
                                    <th class="text-center">Write</th>
                                    <th>Value</th>
                                </tr>
                            </thead>
                            <tbody>`;

        attrs.forEach(a => {
            const readBadge = a.readable
                ? '<span class="badge bg-success">R</span>'
                : '<span class="badge bg-secondary">—</span>';
            const writeBadge = a.writable === true
                ? '<span class="badge bg-success">W</span>'
                : a.writable === false
                    ? '<span class="badge bg-danger">RO</span>'
                    : '<span class="badge bg-warning text-dark">?</span>';

            let displayValue = a.value;
            if (displayValue === null || displayValue === undefined) {
                displayValue = '<span class="text-muted">—</span>';
            } else if (typeof displayValue === 'object') {
                displayValue = `<code class="small">${JSON.stringify(displayValue)}</code>`;
            }

            tableHtml += `
                <tr>
                    <td class="text-monospace">${a.id}</td>
                    <td class="fw-bold">${a.name}</td>
                    <td class="text-muted small">${a.type || ''}</td>
                    <td class="text-center">${readBadge}</td>
                    <td class="text-center">${writeBadge}</td>
                    <td class="text-break" style="max-width:200px">${displayValue}</td>
                </tr>`;
        });

        tableHtml += `</tbody></table></div></div></div>`;
        panel.innerHTML = tableHtml;

    } catch (err) {
        panel.innerHTML = `<div class="alert alert-danger small py-1">Request failed: ${err.message}</div>`;
    }
};