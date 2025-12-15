/**
 * Debug Logging & Packet Capture
 * Handles log display, filtering, and debug packet inspection
 */

import { state } from './state.js';
import { getTimestamp } from './utils.js';
import { analysePacket, renderPacketAnalysis } from './packet-analysis.js';

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
    const devName = device.friendly_name || device.name || 'Unknown';
    const devModel = device.model || device.model_id || '-';

    // Generate a unique ID for this packet row
    // We use a timestamp-random combo to ensure uniqueness for DOM IDs
    const rowId = `live-packet-${Date.now()}-${Math.floor(Math.random() * 1000)}`;

    let analysis;
    try {
        analysis = analysePacket(p);
    } catch (e) {
        console.warn('Packet analyser not available:', e);
        analysis = {
            cluster_name: p.cluster_name || `0x${(p.cluster_id || 0).toString(16).padStart(4, '0')}`,
            command: p.decoded?.command_name || p.decoded?.command_id_hex || 'Unknown',
            summary: ''
        };
    }

    let rowClass = '';
    if (p.cluster === 0xEF00) rowClass = 'table-warning';
    else if (p.cluster === 0x0406) rowClass = 'table-info';

    // 1. The Summary Row
    const summaryHtml = `
        <tr class="${rowClass}" style="cursor: pointer; animation: highlight 1s" onclick="togglePacketDetails('${rowId}')">
            <td class="small">${p.timestamp_str}</td>
            <td class="small fw-bold text-truncate" style="max-width: 150px;" title="${devName}">${devName}</td>
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
    // CRITICAL FIX: Ensure refresh is called to fetch the full data history
    await refreshDebugPackets();
}

export async function refreshDebugPackets() {
    const content = document.getElementById('debugPacketsContent');
    content.innerHTML = '<div class="text-center p-4"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        // Read filter values from the UI
        const importanceFilter = document.getElementById('packetImportanceFilter')?.value || '';
        const ieeeFilter = document.getElementById('packetIeeeFilter')?.value?.trim() || '';
        const clusterFilter = document.getElementById('packetClusterFilter')?.value?.trim() || '';

        // Build query parameters
        const params = new URLSearchParams({ limit: '100' });

        if (importanceFilter) {
            params.append('importance', importanceFilter);
        }
        if (ieeeFilter) {
            params.append('ieee', ieeeFilter);
        }
        if (clusterFilter) {
            // Convert hex string to decimal if it starts with 0x, otherwise parse as decimal
            const clusterInt = clusterFilter.startsWith('0x')
                ? parseInt(clusterFilter, 16)
                : parseInt(clusterFilter, 10);

            if (!isNaN(clusterInt)) {
                params.append('cluster', clusterInt.toString());
            }
        }

        const res = await fetch(`/api/debug/packets?${params.toString()}`);
        const data = await res.json();

        if (data.success) {
            let html = '<table class="table table-sm table-hover"><thead><tr>' +
                '<th width="10%">Time</th>' +
                '<th width="15%">Device</th>' +
                '<th width="10%">Type</th>' +
                '<th width="10%">IEEE</th>' +
                '<th width="15%">Cluster</th>' +
                '<th width="15%">Cmd</th>' +
                '<th width="20%">Summary</th>' +
                '<th width="5%"></th>' +
                '</tr></thead><tbody>';

            if (data.packets.length === 0) {
                html += '<tr><td colspan="8" class="text-center text-muted py-3">No packets match the current filters</td></tr>';
            } else {
                data.packets.forEach((p, idx) => {
                    try {
                        const ieeeShort = p.ieee ? p.ieee.substring(p.ieee.length - 8) : 'N/A';
                        const device = state.deviceCache[p.ieee] || {};
                        const devName = device.friendly_name || device.name || 'Unknown';
                        const devModel = device.model || device.model_id || '-';
                        const rowId = `packet-${idx}`;

                        let analysis;
                        try {
                            analysis = analysePacket(p);
                        } catch (e) {
                            console.warn('Packet analyser not available:', e);
                            analysis = {
                                cluster_name: p.cluster_name || `0x${(p.cluster_id || 0).toString(16).padStart(4, '0')}`,
                                command: p.decoded?.command_name || p.decoded?.command_id_hex || 'Unknown',
                                summary: ''
                            };
                        }

                        let rowClass = '';
                        if (p.cluster === 0xEF00) rowClass = 'table-warning';
                        else if (p.cluster === 0x0406) rowClass = 'table-info';

                        html += `<tr class="${rowClass}" style="cursor: pointer;" onclick="togglePacketDetails('${rowId}')">
                            <td class="small">${p.timestamp_str}</td>
                            <td class="small fw-bold text-truncate" style="max-width: 150px;" title="${devName}">${devName}</td>
                            <td class="small text-muted text-truncate" style="max-width: 100px;" title="${devModel}">${devModel}</td>
                            <td class="small text-muted" title="${p.ieee || 'N/A'}">${ieeeShort}</td>
                            <td>${analysis.cluster_name}</td>
                            <td class="small">${analysis.command}</td>
                            <td class="small">${analysis.summary || '-'}</td>
                            <td class="text-center">
                                <i class="fas fa-chevron-down" id="icon-${rowId}"></i>
                            </td>
                        </tr>`;

                        // ADDED text-light to fix visibility on dark background
                        html += `<tr id="${rowId}" style="display: none;">
                            <td colspan="8" class="bg-dark text-light">
                                <div class="p-3">`;

                        try {
                            html += renderPacketAnalysis(p);
                        } catch (e) {
                            html += `<div class="alert alert-warning">Packet analyser not available. Showing raw data:</div>`;
                        }

                        html += `
                                    <div class="mt-3">
                                        <strong class="d-block mb-2">Raw Packet Data:</strong>
                                        <pre class="bg-black text-light p-2 rounded small" style="max-height: 300px; overflow-y: auto;">${JSON.stringify(p.decoded, null, 2)}</pre>
                                    </div>
                                </div>
                            </td>
                        </tr>`;
                    } catch (rowError) {
                        console.error('Error rendering packet row:', rowError);
                        html += `<tr><td colspan="8" class="text-danger small">Error rendering packet ${idx}: ${rowError.message}</td></tr>`;
                    }
                });
            }
            html += '</tbody></table>';
            content.innerHTML = html;
        }
    } catch (e) {
        console.error('Full error details:', e);
        content.innerHTML = `<div class="alert alert-danger m-3">Error loading packets: ${e.message}<br><small>Check console for details</small></div>`;
    }
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