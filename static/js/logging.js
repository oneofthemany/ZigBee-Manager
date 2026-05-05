/**
 * Debug Logging & Packet Capture
 * Handles log display, filtering, and debug packet inspection
 */

import { state } from './state.js';
import { getTimestamp } from './utils.js';
import { analysePacket, renderPacketAnalysis } from './packet-analysis.js';
import { initPacketFlow } from './packet-flow.js';

// Debug packets cache and sort state
let _debugPacketCache = [];
let _debugSortState = { col: 'time', dir: 'desc' };

/**
 * Add log entry to the log buffer
 */
export function addLogEntry(log) {
    if (!log.timestamp) {
        log.timestamp = getTimestamp();
    }

    // Keep buffer size reasonable
    state.allLogs.push(log);
    if (state.allLogs.length > 2000) state.allLogs.shift();
    
    // Use requestAnimationFrame for smoother UI updates during packet bursts
    requestAnimationFrame(renderLogs);
}

/**
 * Render visible logs based on current filters
 */
export function renderLogs() {
    const container = document.getElementById('logs');
    if (!container) return;

    // 1. Get Filter Values
    const levelFilter = document.getElementById('logLevelFilter').value;
    const ieeeFilter = (document.getElementById('logIeeeFilter')?.value || '').toLowerCase();
    const attrFilter = (document.getElementById('logAttrFilter')?.value || '').toLowerCase();
    const keywordFilter = document.getElementById('logKeywordFilter')?.value || '';

    // Parse Keyword Filter (Support for exclusion via '!')
    const excludeMode = keywordFilter.startsWith('!');
    const keyword = excludeMode ? keywordFilter.substring(1).toLowerCase() : keywordFilter.toLowerCase();

    // 2. Filter Logs
    const visibleLogs = state.allLogs.filter(l => {
        // A. Level Filter
        if (levelFilter !== 'ALL' && l.level !== levelFilter) return false;

        // B. Verbosity Filter (Hardcoded spam reduction)
        if (!state.verboseLogging) {
            const spamPatterns = [
                // Keep these since they are common INFO/DEBUG logs
                "pending publish calls", "MQTT command:", "Sending command:",
                "Polled state =", "Polling 0x", "Polled ", "CRC error",
                "Updating state with applied settings",
                // ADDITION: Suppress basic cluster command logs from base handler if only INFO
                "cluster_command callback!"
            ];
            if (spamPatterns.some(p => l.message.includes(p)) && l.level !== 'DEBUG') return false;
        }

        // C. IEEE / Name Filter
        if (ieeeFilter) {
            const inIeee = l.ieee && l.ieee.toLowerCase().includes(ieeeFilter);
            const inName = l.device_name && l.device_name.toLowerCase().includes(ieeeFilter);
            const inMsg = l.message.toLowerCase().includes(ieeeFilter);
            if (!inIeee && !inName && !inMsg) return false;
        }

        // D. Attribute Filter
        if (attrFilter) {
            if (l.attribute && l.attribute.toLowerCase().includes(attrFilter)) return true;
            if (l.message.toLowerCase().includes(attrFilter)) return true;
            return false;
        }

        // E. Keyword / Exclude Filter
        if (keyword) {
            const msgLower = l.message.toLowerCase();
            const matches = msgLower.includes(keyword);
            if (excludeMode && matches) return false;
            if (!excludeMode && !matches) return false;
        }

        return true;
    }).slice(-150);

    // 3. Render HTML
    const html = visibleLogs.map(l => {
        let color = '#ccc';
        if (l.level === 'INFO') color = '#4CAF50';
        else if (l.level === 'WARNING') color = '#FFC107';
        else if (l.level === 'ERROR') color = '#F44336';
        else if (l.level === 'DEBUG') color = '#2196F3';

        let content = l.message;
        if (keyword && !excludeMode) {
            const reg = new RegExp(`(${keyword})`, 'gi');
            content = content.replace(reg, '<span class="bg-warning text-dark px-1">$1</span>');
        }

        return `<div class="border-bottom border-secondary log-entry py-1">` +
               `<span class="small me-2" style="color: #b0b0b0; opacity: 0.8;">[${l.timestamp}]</span>` +
               `<span style="color:${color}" class="fw-bold me-2">[${l.level}]</span>` +
               `<span>${content}</span>` +
               `</div>`;
    }).join('');

    if (container.innerHTML !== html) {
        container.innerHTML = html;
        if (container.scrollHeight - container.scrollTop - container.clientHeight < 200) {
            container.scrollTop = container.scrollHeight;
        }
    }
}

export function filterLogs() { renderLogs(); }
export function toggleVerboseLogging() {
    state.verboseLogging = !state.verboseLogging;
    const btn = document.getElementById('verboseLogBtn');
    if (btn) {
        if (state.verboseLogging) {
            btn.classList.replace('btn-outline-secondary', 'btn-warning');
            btn.innerHTML = '<i class="fas fa-eye"></i> Verbose';
        } else {
            btn.classList.replace('btn-warning', 'btn-outline-secondary');
            btn.innerHTML = '<i class="fas fa-eye-slash"></i> Standard';
        }
    }
    renderLogs();
}
export function clearLogs() { state.allLogs = []; renderLogs(); }

export async function checkDebugStatus() {
    try {
        const res = await fetch('/api/debug/status');
        const data = await res.json();
        updateDebugStatus(data);
    } catch (e) { console.error(e); }
}

export function updateDebugStatus(data) {
    state.debugEnabled = data.enabled || false;
    const badge = document.getElementById('debugStatusBadge');
    const enableBtn = document.getElementById('debugEnableBtn');
    const disableBtn = document.getElementById('debugDisableBtn');
    if (state.debugEnabled) {
        if (badge) badge.innerHTML = '<span class="badge bg-success">Active</span>';
        if (enableBtn) enableBtn.classList.add('d-none');
        if (disableBtn) disableBtn.classList.remove('d-none');
    } else {
        if (badge) badge.innerHTML = '<span class="badge bg-secondary">Disabled</span>';
        if (enableBtn) enableBtn.classList.remove('d-none');
        if (disableBtn) disableBtn.classList.add('d-none');
    }
}

export async function toggleDebug(enable) {
    const endpoint = enable ? '/api/debug/enable' : '/api/debug/disable';
    await fetch(endpoint, { method: 'POST' });
    checkDebugStatus();
}

/**
 * Handle Live Packet Stream (WebSocket)
 * Adds the packet to the table immediately without full refresh
 */
export function handleLivePacket(p) {
    const tbody = document.querySelector('#debugPacketsContent tbody');
    if (!tbody) return;

    // Apply client-side filtering for live packets to match current filter state
    const importanceFilter = document.getElementById('packetImportanceFilter')?.value || '';
    const ieeeFilter = document.getElementById('packetIeeeFilter')?.value?.trim().toLowerCase() || '';
    const clusterFilter = document.getElementById('packetClusterFilter')?.value?.trim() || '';

    // Check importance filter (Critical/High = IAS Zone, Occupancy clusters)
    if (importanceFilter) {
        const importantClusters = [0x0500, 0x0406]; // IAS Zone, Occupancy
        if ((importanceFilter === 'critical' || importanceFilter === 'high') &&
            !importantClusters.includes(p.cluster)) {
            return; // Skip this packet
        }
    }

    // Check IEEE filter (partial match, case-insensitive)
    if (ieeeFilter && !p.ieee?.toLowerCase().includes(ieeeFilter)) {
        return; // Skip this packet
    }

    // Check cluster filter (supports hex 0x0406 or decimal 1030)
    if (clusterFilter) {
        const clusterInt = clusterFilter.startsWith('0x')
            ? parseInt(clusterFilter, 16)
            : parseInt(clusterFilter, 10);

        if (!isNaN(clusterInt) && p.cluster !== clusterInt) {
            return; // Skip this packet
        }
    }

    // Packet passes filters, add it to the table
    const ieeeShort = p.ieee ? p.ieee.substring(p.ieee.length - 8) : 'N/A';
    const device = state.deviceCache[p.ieee] || {};
    const devName = device.friendly_name || p.friendly_name || device.name || 'Unknown';
    const devModel = device.model || device.model_id || '-';
    const isMatter = p.protocol === 'matter';

    // Protocol badge
    const protoBadge = isMatter
        ? '<span class="badge bg-info me-1" style="font-size:9px">Matter</span>'
        : '<span class="badge bg-success me-1" style="font-size:9px">Zigbee</span>';

    // Direction badge for Matter TX packets
    const dirBadge = p.direction === 'TX'
        ? '<span class="badge bg-warning me-1" style="font-size:9px">TX</span>'
        : '';

    // Generate a unique ID for this packet row
    // We use a timestamp-random combo to ensure uniqueness for DOM IDs
    const rowId = `live-packet-${Date.now()}-${Math.floor(Math.random() * 1000)}`;

    let analysis;
    try {
        if (isMatter) {
            // Matter packets use a simplified analysis
            analysis = {
                cluster_name: p.cluster_name || `Cluster ${p.cluster || '?'}`,
                command: p.event || p.data?.command_name || 'event',
                summary: p.summary || ''
            };
        } else {
            analysis = analysePacket(p);
        }
    } catch (e) {
        console.warn('Packet analyser not available:', e);
        analysis = {
            cluster_name: p.cluster_name || `0x${(p.cluster_id || 0).toString(16).padStart(4, '0')}`,
            command: p.decoded?.command_name || p.decoded?.command_id_hex || 'Unknown',
            summary: ''
        };
    }

    let rowClass = '';
    if (isMatter) rowClass = 'table-primary';
    else if (p.cluster === 0xEF00) rowClass = 'table-warning';
    else if (p.cluster === 0x0406) rowClass = 'table-info';

    // 1. The Summary Row
    const summaryHtml = `
        <tr class="${rowClass}" style="cursor: pointer; animation: highlight 1s" onclick="togglePacketDetails('${rowId}')">
            <td class="small">${p.timestamp_str || new Date(p.timestamp * 1000).toLocaleTimeString()}</td>
            <td class="small fw-bold text-truncate" style="max-width: 150px;" title="${devName}">${protoBadge}${dirBadge}${devName}</td>
            <td class="small text-muted text-truncate" style="max-width: 100px;" title="${devModel}">${devModel}</td>
            <td class="small text-muted" title="${p.ieee}">${ieeeShort}</td>
            <td>${analysis.cluster_name}</td>
            <td class="small">${analysis.command}</td>
            <td class="small">${analysis.summary || '-'}</td>
            <td class="text-center">
                <i class="fas fa-chevron-down" id="icon-${rowId}"></i>
            </td>
        </tr>
    `;

    // 2. The Detailed Row (Hidden by default)
    // ADDED text-light to fix visibility on dark background
    let detailsHtml = `<tr id="${rowId}" style="display: none;">
        <td colspan="8" class="bg-dark text-light">
            <div class="p-3">`;

    try {
        detailsHtml += renderPacketAnalysis(p);
    } catch (e) {
        detailsHtml += `<div class="alert alert-warning">Packet analyser error: ${e.message}</div>`;
    }

    detailsHtml += `
                <div class="mt-3">
                    <strong class="d-block mb-2">Raw Packet Data:</strong>
                    <pre class="bg-black text-light p-2 rounded small" style="max-height: 300px; overflow-y: auto;">${JSON.stringify(p.decoded, null, 2)}</pre>
                </div>
            </div>
        </td>
    </tr>`;

    // Insert both rows
    tbody.insertAdjacentHTML('afterbegin', detailsHtml); // Detail row first (so it ends up below summary when prepending)
    tbody.insertAdjacentHTML('afterbegin', summaryHtml); // Summary row on top

    if (tbody.rows.length > 200) {
        tbody.lastElementChild.remove();
        tbody.lastElementChild.remove();
    }
}

export async function viewDebugPackets() {
    const modal = new bootstrap.Modal(document.getElementById('debugPacketsModal'));
    modal.show();
    // Kick off the packet-flow panel — pulls a one-shot snapshot so the
    // bar at the top of the modal is populated immediately, then live
    // updates take over via the `packet_flow` WS message handler.
    try { initPacketFlow(); } catch (e) { console.debug('initPacketFlow failed', e); }
    // Ensure refresh is called to fetch the full data history
    await refreshDebugPackets();
}

export async function refreshDebugPackets() {
    const content = document.getElementById('debugPacketsContent');
    content.innerHTML = '<div class="text-center p-4"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        const importanceFilter = document.getElementById('packetImportanceFilter')?.value || '';
        const ieeeFilter       = document.getElementById('packetIeeeFilter')?.value?.trim() || '';
        const clusterFilter    = document.getElementById('packetClusterFilter')?.value?.trim() || '';

        const params = new URLSearchParams({ limit: '100' });

        if (importanceFilter) params.append('importance', importanceFilter);
        if (ieeeFilter)       params.append('ieee', ieeeFilter);
        if (clusterFilter) {
            const clusterInt = clusterFilter.startsWith('0x')
                ? parseInt(clusterFilter, 16)
                : parseInt(clusterFilter, 10);
            if (!isNaN(clusterInt)) params.append('cluster', clusterInt.toString());
        }

        const res  = await fetch(`/api/debug/packets?${params.toString()}`);
        const data = await res.json();

        if (data.success) {
            _debugPacketCache = data.packets;
            renderDebugPacketTable();
        }
    } catch (e) {
        console.error('Full error details:', e);
        document.getElementById('debugPacketsContent').innerHTML =
            `<div class="alert alert-danger m-3">Error loading packets: ${e.message}<br><small>Check console for details</small></div>`;
    }
}

export async function exportDebugPackets() {
    const importanceFilter = document.getElementById('packetImportanceFilter')?.value || '';
    const ieeeFilter       = document.getElementById('packetIeeeFilter')?.value?.trim() || '';
    const clusterFilter    = document.getElementById('packetClusterFilter')?.value?.trim() || '';

    // Build query — fetch all matching packets (high limit) respecting current filters
    const params = new URLSearchParams({ limit: '10000' });
    if (importanceFilter) params.append('importance', importanceFilter);
    if (ieeeFilter)       params.append('ieee', ieeeFilter);
    if (clusterFilter) {
        const clusterInt = clusterFilter.startsWith('0x')
            ? parseInt(clusterFilter, 16)
            : parseInt(clusterFilter, 10);
        if (!isNaN(clusterInt)) params.append('cluster', clusterInt.toString());
    }

    try {
        const res  = await fetch(`/api/debug/packets?${params.toString()}`);
        const data = await res.json();

        if (!data.success) {
            alert('Failed to fetch packets: ' + (data.error || 'unknown error'));
            return;
        }

        const packets = data.packets || [];
        if (packets.length === 0) {
            alert('No packets to export.');
            return;
        }

        // Enrich each packet with device info + analysis
        const enriched = packets.map(p => {
            const device   = state.deviceCache[p.ieee] || {};
            const isMatter = p.protocol === 'matter';
            let analysis;
            try {
                if (isMatter) {
                    analysis = {
                        cluster_name: p.cluster_name || `Cluster ${p.cluster || '?'}`,
                        command:      p.event || p.data?.command_name || 'event',
                        summary:      p.summary || ''
                    };
                } else {
                    analysis = analysePacket(p);
                }
            } catch {
                analysis = {
                    cluster_name: p.cluster_name || `0x${(p.cluster || 0).toString(16).padStart(4, '0')}`,
                    command:      p.decoded?.command_name || p.decoded?.command_id_hex || 'Unknown',
                    summary:      ''
                };
            }

            return {
                time:     p.timestamp ? new Date(p.timestamp * 1000).toISOString() : null,
                time_raw: p.timestamp,
                device:   device.friendly_name || p.friendly_name || device.name || 'Unknown',
                type:     device.model || device.model_id || null,
                ieee:     p.ieee || null,
                protocol: p.protocol || 'zigbee',
                endpoint: p.endpoint || null,
                cluster:  p.cluster !== undefined ? `0x${p.cluster.toString(16).padStart(4, '0')}` : null,
                cluster_name: analysis.cluster_name,
                command:  analysis.command,
                summary:  analysis.summary,
                importance: p.importance || null,
                raw:      p.decoded || p.data || p,
            };
        });

        const filterInfo = [];
        if (importanceFilter) filterInfo.push(`importance=${importanceFilter}`);
        if (ieeeFilter)       filterInfo.push(`ieee=${ieeeFilter}`);
        if (clusterFilter)    filterInfo.push(`cluster=${clusterFilter}`);

        const payload = {
            exported_at: new Date().toISOString(),
            filters:     filterInfo.length ? filterInfo.join(', ') : 'none',
            count:       enriched.length,
            packets:     enriched
        };

        // Build filename with filter hints
        const ts = new Date().toISOString().replace(/[:.]/g, '-').split('T')[0];
        const parts = ['packets', ts];
        if (ieeeFilter)       parts.push(ieeeFilter.replace(/:/g, ''));
        if (clusterFilter)    parts.push(clusterFilter.replace(/^0x/i, 'c'));
        if (importanceFilter) parts.push(importanceFilter);
        const filename = parts.join('_') + '.json';

        // Trigger download
        const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        a.href     = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }, 100);

    } catch (e) {
        console.error('Export error:', e);
        alert('Export failed: ' + e.message);
    }
}

function renderDebugPacketTable() {
    const content = document.getElementById('debugPacketsContent');
    if (!content) return;

    // Pre-compute sortable values for each packet
    const rows = _debugPacketCache.map((p, idx) => {
        const ieeeShort = p.ieee ? p.ieee.substring(p.ieee.length - 8) : 'N/A';
        const device    = state.deviceCache[p.ieee] || {};
        const devName   = device.friendly_name || p.friendly_name || device.name || 'Unknown';
        const devModel  = device.model || device.model_id || '-';
        const isMatter  = p.protocol === 'matter';
        let analysis;
        try {
            if (isMatter) {
                analysis = {
                    cluster_name: p.cluster_name || `Cluster ${p.cluster || '?'}`,
                    command:      p.event || p.data?.command_name || 'event',
                    summary:      p.summary || ''
                };
            } else {
                analysis = analysePacket(p);
            }
        } catch (e) {
            analysis = {
                cluster_name: p.cluster_name || `0x${(p.cluster || 0).toString(16).padStart(4, '0')}`,
                command:      p.decoded?.command_name || p.decoded?.command_id_hex || 'Unknown',
                summary:      ''
            };
        }
        return { p, idx, ieeeShort, devName, devModel, analysis, isMatter };
    });

    // Sort
    const { col, dir } = _debugSortState;
    rows.sort((a, b) => {
        let va, vb;
        switch (col) {
            case 'time':    va = a.p.timestamp || 0;               vb = b.p.timestamp || 0;               break;
            case 'device':  va = a.devName.toLowerCase();          vb = b.devName.toLowerCase();          break;
            case 'type':    va = a.devModel.toLowerCase();         vb = b.devModel.toLowerCase();         break;
            case 'ieee':    va = (a.p.ieee || '').toLowerCase();   vb = (b.p.ieee || '').toLowerCase();   break;
            case 'cluster': va = a.p.cluster || 0;                 vb = b.p.cluster || 0;                 break;
            case 'cmd':     va = a.analysis.command.toLowerCase(); vb = b.analysis.command.toLowerCase(); break;
            case 'summary': va = (a.analysis.summary || '').toLowerCase(); vb = (b.analysis.summary || '').toLowerCase(); break;
            default:        va = a.p.timestamp || 0;               vb = b.p.timestamp || 0;
        }
        if (va < vb) return dir === 'asc' ? -1 : 1;
        if (va > vb) return dir === 'asc' ?  1 : -1;
        return 0;
    });

    // Build sortable header
    const COLS = [
        { key: 'time',    label: 'Time',    width: '10%' },
        { key: 'device',  label: 'Device',  width: '15%' },
        { key: 'type',    label: 'Type',    width: '10%' },
        { key: 'ieee',    label: 'IEEE',    width: '10%' },
        { key: 'cluster', label: 'Cluster', width: '15%' },
        { key: 'cmd',     label: 'Cmd',     width: '15%' },
        { key: 'summary', label: 'Summary', width: '20%' },
    ];

    const headerCells = COLS.map(({ key, label, width }) => {
        const active = _debugSortState.col === key;
        const icon   = active
            ? (_debugSortState.dir === 'asc' ? 'fa-sort-up' : 'fa-sort-down')
            : 'fa-sort';
        return `<th width="${width}" class="debug-sort-hdr" data-col="${key}" style="cursor:pointer;user-select:none;">` +
               `${label} <i class="fas ${icon} ${active ? 'text-primary' : 'text-muted'} small"></i></th>`;
    }).join('');

    let html = `<table class="table table-sm table-hover">
        <thead><tr>${headerCells}<th width="20%">Summary</th><th width="5%"></th></tr></thead>
        <tbody>`;

    if (rows.length === 0) {
        html += '<tr><td colspan="8" class="text-center text-muted py-3">No packets match the current filters</td></tr>';
    } else {
        rows.forEach(({ p, idx, ieeeShort, devName, devModel, analysis, isMatter }) => {
            try {
                const rowId = `packet-${idx}`;
                const protoBadge = isMatter
                    ? '<span class="badge bg-info me-1" style="font-size:9px">Matter</span>'
                    : '<span class="badge bg-success me-1" style="font-size:9px">Zigbee</span>';
                const dirBadge = p.direction === 'TX'
                    ? '<span class="badge bg-warning me-1" style="font-size:9px">TX</span>'
                    : '';

                let rowClass = '';
                if (isMatter) rowClass = 'table-primary';
                else if (p.cluster === 0xEF00) rowClass = 'table-warning';
                else if (p.cluster === 0x0406) rowClass = 'table-info';

                html += `<tr class="${rowClass}" style="cursor:pointer;" onclick="togglePacketDetails('${rowId}')">
                    <td class="small">${p.timestamp_str || (p.timestamp ? new Date(p.timestamp * 1000).toLocaleTimeString() : '-')}</td>
                    <td class="small fw-bold text-truncate" style="max-width:150px;" title="${devName}">${protoBadge}${dirBadge}${devName}</td>
                    <td class="small text-muted text-truncate" style="max-width:100px;" title="${devModel}">${devModel}</td>
                    <td class="small text-muted" title="${p.ieee || 'N/A'}">${ieeeShort}</td>
                    <td>${analysis.cluster_name}</td>
                    <td class="small">${analysis.command}</td>
                    <td class="small">${analysis.summary || '-'}</td>
                    <td class="text-center"><i class="fas fa-chevron-down" id="icon-${rowId}"></i></td>
                </tr>`;

                html += `<tr id="${rowId}" style="display:none;">
                    <td colspan="8" class="bg-dark text-light"><div class="p-3">`;
                try {
                    if (isMatter) {
                        // Matter packet detail view
                        html += `
                            <div class="row g-3">
                                <div class="col-md-6">
                                    <strong>Event:</strong> ${p.event || 'unknown'}<br>
                                    <strong>Node ID:</strong> ${p.node_id || '?'}<br>
                                    <strong>Endpoint:</strong> ${p.endpoint || p.data?.endpoint_id || '?'}<br>
                                    <strong>Cluster:</strong> ${p.cluster || p.data?.cluster_id || '?'}<br>
                                    <strong>Direction:</strong> ${p.direction || 'RX'}
                                </div>
                                <div class="col-md-6">
                                    <strong>Summary:</strong> ${p.summary || '-'}<br>
                                    <strong>Importance:</strong> ${p.importance || 'normal'}
                                </div>
                            </div>`;
                    } else {
                        html += renderPacketAnalysis(p);
                    }
                } catch (e) {
                    html += `<div class="alert alert-warning">Packet analyser error: ${e.message}</div>`;
                }
                html += `<div class="mt-3">
                    <strong class="d-block mb-2">Raw Data:</strong>
                    <pre class="bg-black text-light p-2 rounded small" style="max-height:300px;overflow-y:auto;">${JSON.stringify(isMatter ? (p.data || p) : p.decoded, null, 2)}</pre>
                </div></div></td></tr>`;

            } catch (rowError) {
                console.error('Error rendering packet row:', rowError);
                html += `<tr><td colspan="8" class="text-danger small">Error rendering packet ${idx}: ${rowError.message}</td></tr>`;
            }
        });
    }

    html += '</tbody></table>';
    content.innerHTML = html;

    // Attach sort click handlers
    content.querySelectorAll('.debug-sort-hdr').forEach(th => {
        th.addEventListener('click', () => {
            const col = th.dataset.col;
            if (_debugSortState.col === col) {
                _debugSortState.dir = _debugSortState.dir === 'asc' ? 'desc' : 'asc';
            } else {
                _debugSortState.col = col;
                _debugSortState.dir = col === 'time' ? 'desc' : 'asc';
            }
            renderDebugPacketTable();
        });
    });
}

/**
 * Download Combined Debug Log
 */
export async function downloadDebugLog() {
    try {
        const resp = await fetch('/api/debug/log_file?lines=5000');
        const rawText = await resp.text();
        const appLogs = state.allLogs;
        const appLogsJson = JSON.stringify(appLogs, null, 4);

        const htmlContent = `
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Zigbee Debug Report - ${new Date().toISOString()}</title>
    <style>
        body { font-family: monospace; background: #1e1e1e; color: #d4d4d4; margin: 0; padding: 20px; }
        h2 { color: #4ec9b0; border-bottom: 1px solid #333; padding-bottom: 10px; margin-top: 30px; }
        .container { display: flex; flex-direction: column; gap: 20px; }
        .section { background: #252526; padding: 15px; border-radius: 5px; border: 1px solid #333; }
        textarea { width: 100%; height: 400px; background: #1e1e1e; color: #ce9178; border: none; font-family: monospace; padding: 10px; box-sizing: border-box; resize: vertical; }
        pre { white-space: pre-wrap; word-wrap: break-word; color: #9cdcfe; margin: 0; }
        .btn { padding: 8px 16px; background: #0e639c; color: white; border: none; cursor: pointer; border-radius: 3px; font-family: sans-serif; text-decoration: none; display: inline-block; margin-bottom: 10px;}
        .btn:hover { background: #1177bb; }
    </style>
</head>
<body>
    <h1>Zigbee Debug Report</h1>
    <p>Generated: ${new Date().toLocaleString()}</p>

    <div class="container">
        <!-- SECTION 1: APPLICATION LOGS (JSON) -->
        <div class="section">
            <h2>1. Application Logs (Rich JSON Data)</h2>
            <p>Contains attribute updates, connection events, and parsed data.</p>
            <textarea readonly>${appLogsJson}</textarea>
        </div>

        <!-- SECTION 2: RAW SERVER LOGS -->
        <div class="section">
            <h2>2. Raw Zigbee Debug Log (Server)</h2>
            <p>Contains raw packet hex dumps, RX/TX frames, and stack traces.</p>
            <pre>${rawText.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>
        </div>
    </div>
</body>
</html>`;

        const blob = new Blob([htmlContent], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        window.open(url, '_blank');

    } catch (e) {
        console.error("Failed to generate debug report:", e);
        alert("Failed to generate report. Opening raw log instead.");
        window.open('/api/debug/log_file?lines=5000', '_blank');
    }
}

/**
 * Clear all debug packet filters and refresh the packet view
 */
export function clearDebugFilters() {
    document.getElementById('packetImportanceFilter').value = '';
    document.getElementById('packetIeeeFilter').value = '';
    document.getElementById('packetClusterFilter').value = '';
    refreshDebugPackets();
}

/**
 * Toggle packet details visibility
 */
window.togglePacketDetails = function(id) {
    const row = document.getElementById(id);
    const icon = document.getElementById(`icon-${id}`);

    if (row.style.display === 'none') {
        row.style.display = '';
        icon.classList.remove('fa-chevron-down');
        icon.classList.add('fa-chevron-up');
    } else {
        row.style.display = 'none';
        icon.classList.remove('fa-chevron-up');
        icon.classList.add('fa-chevron-down');
    }
};