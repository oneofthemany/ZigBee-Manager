/**
 * System Telemetry Tab
 * Location: static/js/system-telemetry.js
 *
 * Fluid real-time system monitoring with:
 *   - Surgical DOM updates (no innerHTML rebuilds during refresh)
 *   - CSS transitions on gauge bars and colours
 *   - Persistent SVG chart with polyline point updates
 *   - Staggered refresh: gauges 5s, chart 30s, DB stats 60s
 */

import { state } from './state.js';

let _gaugeTimer = null;
let _chartTimer = null;
let _dbTimer = null;
let _chartBuilt = false;
let _chartData = [];

const SERIES = [
    { key: 'cpu_percent', label: 'CPU %', color: '#0d6efd', id: 'cpu' },
    { key: 'mem_percent', label: 'Memory %', color: '#198754', id: 'mem' },
    { key: 'cpu_temp',    label: 'CPU Temp', color: '#dc3545', id: 'temp' },
];

// ============================================================================
// INIT
// ============================================================================

export function initSystemTab() {
    const tab = document.querySelector('button[data-bs-target="#system"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => _startTab());
        tab.addEventListener('hidden.bs.tab', () => _stopTab());
    }
}

function _startTab() {
    const container = document.getElementById('system-content');
    if (!container) return;

    // Only build skeleton once
    if (!document.getElementById('sys-gauges')) {
        container.innerHTML = _renderSkeleton();
    }

    // Initial fetch
    _refreshGauges();
    _refreshChart();
    _refreshDbStats();

    // Staggered intervals
    _stopTab();
    _gaugeTimer = setInterval(_refreshGauges, 5000);
    _chartTimer = setInterval(_refreshChart, 30000);
    _dbTimer    = setInterval(_refreshDbStats, 60000);
}

function _stopTab() {
    if (_gaugeTimer) { clearInterval(_gaugeTimer); _gaugeTimer = null; }
    if (_chartTimer) { clearInterval(_chartTimer); _chartTimer = null; }
    if (_dbTimer)    { clearInterval(_dbTimer);    _dbTimer = null; }
}

// ============================================================================
// SKELETON (rendered once)
// ============================================================================

function _renderSkeleton() {
    return `
    <style>
        .sys-bar { transition: width 0.8s ease, background-color 0.5s ease; }
        .sys-val { transition: color 0.5s ease; }
        #sys-chart-svg polyline { transition: points 0.6s ease; }
    </style>

    <!-- Gauges -->
    <div class="row g-3 mb-3" id="sys-gauges">
        ${_gaugeCard('cpu',  'CPU',         'microchip',        80, 95)}
        ${_gaugeCard('mem',  'Memory',      'memory',           80, 90)}
        ${_gaugeCard('temp', 'Temperature', 'thermometer-half', 75, 85)}
        ${_gaugeCard('disk', 'Disk',        'hdd',              85, 95)}
        ${_gaugeCard('proc', 'Process',     'cogs',             0,  0)}
        ${_gaugeCard('load', 'Load / Uptime','tachometer-alt',  0,  0)}
    </div>

    <!-- Alerts -->
    <div id="sys-alerts" class="mb-3"></div>

    <!-- History Chart -->
    <div class="card mb-3">
        <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
            <strong class="small"><i class="fas fa-chart-line me-1"></i> System History</strong>
            <div class="d-flex gap-2 align-items-center">
                <select class="form-select form-select-sm" id="sys-history-hours" style="width:auto" onchange="window._sysRefreshChart()">
                    <option value="1" selected>Last 1h</option>
                    <option value="6">Last 6h</option>
                    <option value="24">Last 24h</option>
                    <option value="72">Last 3 days</option>
                    <option value="168">Last 7 days</option>
                </select>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._sysRefreshChart()"><i class="fas fa-sync-alt"></i></button>
            </div>
        </div>
        <div class="card-body p-2" id="sys-history-chart" style="min-height:220px">
            <div class="text-muted small text-center py-4"><i class="fas fa-spinner fa-spin"></i> Loading history...</div>
        </div>
    </div>

    <!-- DB Stats -->
    <div class="card">
        <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
            <strong class="small"><i class="fas fa-database me-1"></i> Telemetry Database</strong>
            <button class="btn btn-sm btn-outline-danger" onclick="window._sysPrune()" title="Prune old data">
                <i class="fas fa-broom me-1"></i> Prune
            </button>
        </div>
        <div class="card-body py-2" id="sys-db-stats">
            <span class="text-muted small"><i class="fas fa-spinner fa-spin"></i></span>
        </div>
    </div>`;
}

function _gaugeCard(id, label, icon, warn, crit) {
    return `
    <div class="col-md-2 col-sm-4 col-6">
        <div class="card h-100">
            <div class="card-body py-2 px-2">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span class="text-muted small"><i class="fas fa-${icon} me-1"></i>${label}</span>
                    <span id="sys-val-${id}" class="fw-bold sys-val" style="font-size:1.1rem">—</span>
                </div>
                ${(warn > 0) ? `<div class="progress" style="height:4px">
                    <div id="sys-bar-${id}" class="progress-bar sys-bar bg-success" role="progressbar" style="width:0%"></div>
                </div>` : ''}
                <div id="sys-sub-${id}" class="text-muted mt-1" style="font-size:0.68rem;line-height:1.2"></div>
            </div>
        </div>
    </div>`;
}

// ============================================================================
// GAUGE UPDATES (surgical — textContent + attribute only)
// ============================================================================

async function _refreshGauges() {
    try {
        const res = await fetch('/api/telemetry/system/current');
        const d = await res.json();
        if (!d || d.error) return;
        _updateGauges(d);
    } catch (e) { /* silent */ }
}

function _updateGauges(d) {
    // CPU
    _setVal('cpu', d.cpu_percent != null ? `${d.cpu_percent.toFixed(0)}%` : '—');
    _setBar('cpu', d.cpu_percent, 80, 95);
    _setSub('cpu', d.cpu_freq ? `${d.cpu_freq.toFixed(0)} MHz` : '');

    // Memory
    const memGB = d.mem_used ? (d.mem_used / 1073741824).toFixed(1) : '?';
    const memTotGB = d.mem_total ? (d.mem_total / 1073741824).toFixed(1) : '?';
    _setVal('mem', d.mem_percent != null ? `${d.mem_percent.toFixed(0)}%` : '—');
    _setBar('mem', d.mem_percent, 80, 90);
    _setSub('mem', `${memGB} / ${memTotGB} GB`);

    // Temperature
    const parts = [];
    if (d.cpu_temp != null) parts.push(`CPU ${d.cpu_temp.toFixed(0)}°C`);
    if (d.gpu_temp != null) parts.push(`GPU ${d.gpu_temp.toFixed(0)}°C`);
    _setVal('temp', d.cpu_temp != null ? `${d.cpu_temp.toFixed(0)}°C` : '—');
    _setBar('temp', d.cpu_temp, 75, 85);
    _setSub('temp', parts.join(' · ') || 'No sensors');

    // Disk
    const dskGB = d.disk_used ? (d.disk_used / 1073741824).toFixed(1) : '?';
    const dskTotGB = d.disk_total ? (d.disk_total / 1073741824).toFixed(1) : '?';
    _setVal('disk', d.disk_percent != null ? `${d.disk_percent.toFixed(0)}%` : '—');
    _setBar('disk', d.disk_percent, 85, 95);
    _setSub('disk', `${dskGB} / ${dskTotGB} GB`);

    // Process (no bar)
    const rss = d.process_rss ? (d.process_rss / 1048576).toFixed(0) : '?';
    _setVal('proc', `${rss} MB`);
    _setSub('proc', d.process_threads ? `${d.process_threads} threads` : '');

    // Load + Uptime (no bar)
    _setVal('load', d.load_1m != null ? d.load_1m.toFixed(2) : '—');
    let loadSub = '';
    if (d.load_5m != null) loadSub = `5m: ${d.load_5m.toFixed(2)} · 15m: ${d.load_15m?.toFixed(2) || '?'}`;
    if (d.uptime_secs) {
        const days = Math.floor(d.uptime_secs / 86400);
        const hrs = Math.floor((d.uptime_secs % 86400) / 3600);
        const up = days > 0 ? `${days}d ${hrs}h` : `${hrs}h`;
        loadSub = `uptime ${up}` + (loadSub ? ` · ${loadSub}` : '');
    }
    _setSub('load', loadSub);

    // Alerts
    _updateAlerts(d.active_alerts);
}

function _setVal(id, text) {
    const el = document.getElementById(`sys-val-${id}`);
    if (el && el.textContent !== text) el.textContent = text;
}

function _setSub(id, text) {
    const el = document.getElementById(`sys-sub-${id}`);
    if (el && el.textContent !== text) el.textContent = text;
}

function _setBar(id, value, warn, crit) {
    const el = document.getElementById(`sys-bar-${id}`);
    if (!el || value == null) return;
    const pct = Math.min(Math.max(value, 0), 100);
    el.style.width = `${pct}%`;
    el.classList.remove('bg-success', 'bg-warning', 'bg-danger');
    if (value >= crit) el.classList.add('bg-danger');
    else if (value >= warn) el.classList.add('bg-warning');
    else el.classList.add('bg-success');

    // Value text colour
    const valEl = document.getElementById(`sys-val-${id}`);
    if (valEl) {
        valEl.classList.remove('text-success', 'text-warning', 'text-danger');
        if (value >= crit) valEl.classList.add('text-danger');
        else if (value >= warn) valEl.classList.add('text-warning');
        else valEl.classList.add('text-success');
    }
}

function _updateAlerts(alerts) {
    const el = document.getElementById('sys-alerts');
    if (!el) return;
    if (!alerts || Object.keys(alerts).length === 0) {
        if (el.innerHTML !== '') el.innerHTML = '';
        return;
    }
    const html = Object.entries(alerts).map(([m, s]) => {
        const cls = s === 'critical' ? 'danger' : 'warning';
        const ico = s === 'critical' ? 'exclamation-circle' : 'exclamation-triangle';
        return `<span class="badge bg-${cls} me-1"><i class="fas fa-${ico} me-1"></i>${m}: ${s}</span>`;
    }).join('');
    const newHtml = `<div class="alert alert-warning py-2 small mb-0"><i class="fas fa-bell me-1"></i> Active: ${html}</div>`;
    if (el.innerHTML !== newHtml) el.innerHTML = newHtml;
}

// ============================================================================
// HISTORY CHART — persistent SVG, update polyline points only
// ============================================================================

async function _refreshChart() {
    const hours = parseInt(document.getElementById('sys-history-hours')?.value || '1');
    const bucket = hours <= 1 ? 1 : hours <= 6 ? 2 : hours <= 24 ? 5 : 15;

    try {
        const res = await fetch(`/api/telemetry/system/history?hours=${hours}&bucket=${bucket}`);
        const json = await res.json();
        if (!json.success || !json.data?.length) {
            if (!_chartBuilt) {
                const el = document.getElementById('sys-history-chart');
                if (el) el.innerHTML = '<div class="text-muted small text-center py-4">No history yet — collecting every 30s.</div>';
            }
            return;
        }
        _chartData = json.data;

        if (!_chartBuilt) {
            _buildChart();
        }
        _updateChartLines();
    } catch (e) { /* silent */ }
}

function _buildChart() {
    const el = document.getElementById('sys-history-chart');
    if (!el) return;

    const W = el.clientWidth || 700;
    const H = 200;

    // Legend
    const legend = SERIES.map(s =>
        `<span class="me-3" style="font-size:0.7rem;cursor:pointer" onclick="window._sysToggleLine('${s.id}')">` +
        `<span style="color:${s.color}">●</span> ${s.label}</span>`
    ).join('');

    el.innerHTML = `
        <div class="mb-1" id="sys-chart-legend">${legend}</div>
        <svg id="sys-chart-svg" width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
            <g id="sys-chart-grid"></g>
            <g id="sys-chart-xlabels"></g>
            ${SERIES.map(s =>
                `<polyline id="sys-line-${s.id}" points="" fill="none" stroke="${s.color}" stroke-width="1.5" stroke-opacity="0.85" stroke-linejoin="round"/>`
            ).join('')}
            <line id="sys-chart-cursor" x1="0" y1="10" x2="0" y2="${H - 25}" stroke="#999" stroke-width="0.5" stroke-dasharray="3" visibility="hidden"/>
            <text id="sys-chart-tooltip" x="0" y="0" font-size="10" fill="#333" visibility="hidden"></text>
        </svg>`;

    // Hover interaction
    const svg = document.getElementById('sys-chart-svg');
    if (svg) {
        svg.addEventListener('mousemove', _chartHover);
        svg.addEventListener('mouseleave', () => {
            document.getElementById('sys-chart-cursor')?.setAttribute('visibility', 'hidden');
            document.getElementById('sys-chart-tooltip')?.setAttribute('visibility', 'hidden');
        });
    }

    _chartBuilt = true;
}

function _updateChartLines() {
    const svg = document.getElementById('sys-chart-svg');
    if (!svg || !_chartData.length) return;

    const W = svg.viewBox.baseVal.width || 700;
    const H = svg.viewBox.baseVal.height || 200;
    const pad = { top: 10, right: 10, bottom: 25, left: 35 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const times = _chartData.map(d => new Date(d.ts).getTime());
    const tMin = Math.min(...times);
    const tMax = Math.max(...times);
    const tRange = tMax - tMin || 1;

    const x = t => pad.left + ((t - tMin) / tRange) * plotW;
    const y = v => pad.top + plotH - (Math.min(Math.max(v, 0), 100) / 100) * plotH;

    // Update polyline points
    SERIES.forEach(s => {
        const line = document.getElementById(`sys-line-${s.id}`);
        if (!line) return;
        const pts = _chartData
            .filter(d => d[s.key] != null)
            .map(d => `${x(new Date(d.ts).getTime()).toFixed(1)},${y(d[s.key]).toFixed(1)}`)
            .join(' ');
        line.setAttribute('points', pts);
    });

    // Update grid (only rebuild if data range changed significantly)
    const grid = document.getElementById('sys-chart-grid');
    if (grid) {
        let gridHtml = '';
        for (let v = 0; v <= 100; v += 25) {
            const yy = y(v);
            gridHtml += `<text x="${pad.left - 4}" y="${yy + 3}" fill="#999" font-size="9" text-anchor="end">${v}</text>`;
            gridHtml += `<line x1="${pad.left}" x2="${W - pad.right}" y1="${yy}" y2="${yy}" stroke="#f0f0f0" stroke-width="0.5"/>`;
        }
        // Border
        gridHtml += `<rect x="${pad.left}" y="${pad.top}" width="${plotW}" height="${plotH}" fill="none" stroke="#e0e0e0" stroke-width="0.5"/>`;
        grid.innerHTML = gridHtml;
    }

    // Update X labels
    const xLabels = document.getElementById('sys-chart-xlabels');
    if (xLabels) {
        let xlHtml = '';
        const tickCount = Math.min(8, _chartData.length);
        const step = Math.max(1, Math.floor(_chartData.length / tickCount));
        for (let i = 0; i < _chartData.length; i += step) {
            const t = new Date(_chartData[i].ts);
            const xPos = x(t.getTime());
            const label = `${t.getHours().toString().padStart(2, '0')}:${t.getMinutes().toString().padStart(2, '0')}`;
            xlHtml += `<text x="${xPos}" y="${H - 3}" fill="#999" font-size="9" text-anchor="middle">${label}</text>`;
        }
        xLabels.innerHTML = xlHtml;
    }
}

function _chartHover(e) {
    if (!_chartData.length) return;
    const svg = document.getElementById('sys-chart-svg');
    if (!svg) return;

    const rect = svg.getBoundingClientRect();
    const svgW = svg.viewBox.baseVal.width || rect.width;
    const mouseX = (e.clientX - rect.left) / rect.width * svgW;

    const pad = { left: 35, right: 10 };
    const plotW = svgW - pad.left - pad.right;
    const frac = (mouseX - pad.left) / plotW;
    const idx = Math.round(frac * (_chartData.length - 1));

    if (idx < 0 || idx >= _chartData.length) return;
    const d = _chartData[idx];
    const t = new Date(d.ts);
    const timeStr = `${t.getHours().toString().padStart(2, '0')}:${t.getMinutes().toString().padStart(2, '0')}`;

    const cursor = document.getElementById('sys-chart-cursor');
    const tooltip = document.getElementById('sys-chart-tooltip');
    if (cursor) {
        cursor.setAttribute('x1', mouseX);
        cursor.setAttribute('x2', mouseX);
        cursor.setAttribute('visibility', 'visible');
    }
    if (tooltip) {
        const vals = SERIES.map(s => d[s.key] != null ? `${s.label}: ${d[s.key].toFixed(1)}` : null).filter(Boolean).join('  ');
        tooltip.textContent = `${timeStr}  ${vals}`;
        tooltip.setAttribute('x', Math.min(mouseX + 5, svgW - 200));
        tooltip.setAttribute('y', 20);
        tooltip.setAttribute('visibility', 'visible');
    }
}

function _sysToggleLine(id) {
    const line = document.getElementById(`sys-line-${id}`);
    if (!line) return;
    const hidden = line.getAttribute('stroke-opacity') === '0';
    line.setAttribute('stroke-opacity', hidden ? '0.85' : '0');
    line.setAttribute('stroke-width', hidden ? '1.5' : '0');
}

// ============================================================================
// DB STATS (low-frequency, innerHTML is fine here)
// ============================================================================

async function _refreshDbStats() {
    try {
        const res = await fetch('/api/telemetry/db/stats');
        const data = await res.json();
        if (!data.success) return;

        const el = document.getElementById('sys-db-stats');
        if (!el) return;

        const tables = ['system_metrics', 'packet_stats', 'device_states', 'spectrum_scans'];
        const badges = tables.map(t => {
            const count = data[t] || 0;
            const label = t.replace(/_/g, ' ');
            return `<span class="badge bg-light text-dark border me-2">${label}: <strong>${count.toLocaleString()}</strong></span>`;
        }).join('');

        const newHtml = `<div class="d-flex flex-wrap align-items-center gap-1">${badges}` +
            `<span class="badge bg-info text-white ms-2">${data.file_size_mb || 0} MB</span></div>`;

        if (el.innerHTML !== newHtml) el.innerHTML = newHtml;
    } catch (e) { /* silent */ }
}

// ============================================================================
// ACTIONS
// ============================================================================

async function _sysPrune() {
    if (!confirm('Prune telemetry data older than 7 days?')) return;
    try {
        const res = await fetch('/api/telemetry/db/prune', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            await _refreshDbStats();
        }
    } catch (e) {
        alert('Prune failed: ' + e.message);
    }
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

window._sysRefreshChart = _refreshChart;
window._sysToggleLine = _sysToggleLine;
window._sysPrune = _sysPrune;