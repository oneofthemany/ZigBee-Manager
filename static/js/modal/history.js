/**
 * Device History Tab
 * Location: static/js/modal/history.js
 *
 * Renders time-series charts for attribute changes stored in DuckDB.
 * Data source: /api/telemetry/device/{ieee}/...
 */

const HOURS_OPTIONS = [
    { v: 1,   label: '1h'  },
    { v: 6,   label: '6h'  },
    { v: 24,  label: '24h' },
    { v: 72,  label: '3d'  },
    { v: 168, label: '7d'  },
];

export function renderHistoryTab(device) {
    return `
        <div class="mb-3 d-flex gap-2 align-items-center flex-wrap">
            <label class="small text-muted mb-0">Attribute</label>
            <select class="form-select form-select-sm" id="hist-attr" style="width:auto"></select>
            <label class="small text-muted mb-0 ms-2">Range</label>
            <select class="form-select form-select-sm" id="hist-hours" style="width:auto">
                ${HOURS_OPTIONS.map(o =>
                    `<option value="${o.v}" ${o.v === 24 ? 'selected' : ''}>${o.label}</option>`
                ).join('')}
            </select>
            <button class="btn btn-sm btn-outline-secondary" id="hist-refresh">
                <i class="fas fa-sync-alt"></i>
            </button>
            <span class="ms-auto small text-muted" id="hist-meta"></span>
        </div>
        <div id="hist-chart-wrap">
            <div class="text-muted small text-center py-4">Loading history…</div>
        </div>
        <div id="hist-raw" class="mt-3"></div>
    `;
}

export async function initHistoryTab(ieee) {
    const attrSel   = document.getElementById('hist-attr');
    const hoursSel  = document.getElementById('hist-hours');
    const refreshBtn = document.getElementById('hist-refresh');
    if (!attrSel || !hoursSel) return;

    // Populate attribute list
    try {
        const res = await fetch(`/api/telemetry/device/${ieee}/attributes?hours=168`);
        const json = await res.json();
        const attrs = (json.success && json.attributes) ? json.attributes : [];
        if (!attrs.length) {
            document.getElementById('hist-chart-wrap').innerHTML =
                '<div class="text-muted small text-center py-4">' +
                'No history recorded yet — data accumulates as the device reports.' +
                '</div>';
            return;
        }
        attrSel.innerHTML = attrs.map(a =>
            `<option value="${a}">${a}</option>`
        ).join('');
    } catch (e) {
        document.getElementById('hist-chart-wrap').innerHTML =
            `<div class="text-danger small py-4">Failed to load attributes: ${e.message}</div>`;
        return;
    }

    const refresh = () => _refreshHistoryChart(ieee);
    attrSel.addEventListener('change', refresh);
    hoursSel.addEventListener('change', refresh);
    refreshBtn.addEventListener('click', refresh);
    refresh();
}

async function _refreshHistoryChart(ieee) {
    const attr = document.getElementById('hist-attr')?.value;
    const hours = parseInt(document.getElementById('hist-hours')?.value || '24');
    if (!attr) return;

    const bucket = hours <= 1 ? 1 : hours <= 6 ? 2 : hours <= 24 ? 5 : hours <= 72 ? 15 : 30;

    const wrap = document.getElementById('hist-chart-wrap');
    wrap.innerHTML = '<div class="text-muted small text-center py-4">Loading…</div>';

    try {
        const res = await fetch(
            `/api/telemetry/device/${ieee}/history?attribute=${encodeURIComponent(attr)}&hours=${hours}&bucket=${bucket}`
        );
        const json = await res.json();
        if (!json.success || !json.data?.length) {
            wrap.innerHTML = '<div class="text-muted small text-center py-4">No data in this range.</div>';
            document.getElementById('hist-meta').textContent = '';
            return;
        }
        _buildHistChart(json.data, attr);
        const total = json.data.reduce((s, r) => s + (r.samples || 0), 0);
        document.getElementById('hist-meta').textContent =
            `${total} samples · ${bucket}m buckets`;
    } catch (e) {
        wrap.innerHTML = `<div class="text-danger small py-4">Query failed: ${e.message}</div>`;
    }
}

function _buildHistChart(data, attr) {
    const wrap = document.getElementById('hist-chart-wrap');
    if (!wrap) return;

    const numeric = data.some(r => r.avg !== null && r.avg !== undefined);

    if (!numeric) {
        // Non-numeric: show timeline table of state changes
        wrap.innerHTML = `
            <div class="table-responsive" style="max-height:400px">
                <table class="table table-sm table-striped">
                    <thead><tr><th>Time</th><th>${attr}</th><th class="text-end">Samples</th></tr></thead>
                    <tbody>
                        ${data.map(r => `
                            <tr>
                                <td class="small font-monospace">${new Date(r.ts).toLocaleString()}</td>
                                <td class="small">${r.last_str ?? ''}</td>
                                <td class="small text-end text-muted">${r.samples}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
        return;
    }

    // Numeric: SVG with dots + line + min/max band
    const W = wrap.clientWidth || 700;
    const H = 240;
    const pad = { top: 10, right: 10, bottom: 38, left: 50 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const times = data.map(d => new Date(d.ts).getTime());
    const tMin = Math.min(...times);
    const tMax = Math.max(...times);
    const tRange = (tMax - tMin) || 1;

    const values = data.flatMap(d => [d.min, d.max, d.avg].filter(v => v !== null && v !== undefined));
    const vMin = Math.min(...values);
    const vMax = Math.max(...values);
    // Pad value range by 5% so a single point doesn't sit flush on the axis
    const vPad = ((vMax - vMin) || Math.max(Math.abs(vMin), 1)) * 0.05;
    const vLo = vMin - vPad;
    const vHi = vMax + vPad;
    const vRange = vHi - vLo || 1;

    const x = t => pad.left + ((t - tMin) / tRange) * plotW;
    const y = v => pad.top + plotH - ((v - vLo) / vRange) * plotH;

    // Min/max band — only meaningful with ≥ 2 points
    let bandPolygon = '';
    let linePolyline = '';
    if (data.length >= 2) {
        const topPts = data.map(d => `${x(new Date(d.ts).getTime())},${y(d.max)}`).join(' ');
        const botPts = data.slice().reverse().map(d => `${x(new Date(d.ts).getTime())},${y(d.min)}`).join(' ');
        bandPolygon = `<polygon points="${topPts} ${botPts}" fill="#4a90e2" fill-opacity="0.15" stroke="none"/>`;
        const avgPoints = data.map(d => `${x(new Date(d.ts).getTime())},${y(d.avg)}`).join(' ');
        linePolyline = `<polyline points="${avgPoints}" fill="none" stroke="#4a90e2" stroke-width="1.5" stroke-linejoin="round"/>`;
    }

    // Always render dots — guarantees visibility for single-point series too
    const dots = data.map(d => {
        const cx = x(new Date(d.ts).getTime());
        const cy = y(d.avg);
        const iso = new Date(d.ts).toISOString().replace('T', ' ').slice(0, 19);
        return `<circle cx="${cx}" cy="${cy}" r="2.5" fill="#4a90e2" stroke="#fff" stroke-width="1">
                    <title>${iso} — ${d.avg?.toFixed(2)} (${d.samples} samples)</title>
                </circle>`;
    }).join('');

    // Y-axis ticks
    const yTicks = 4;
    const yLabels = Array.from({ length: yTicks + 1 }, (_, i) => {
        const v = vLo + (vRange * i / yTicks);
        const yp = y(v);
        return `
            <line x1="${pad.left}" x2="${W - pad.right}" y1="${yp}" y2="${yp}" stroke="#eee" stroke-width="0.5"/>
            <text x="${pad.left - 4}" y="${yp + 3}" font-size="9" fill="#888" text-anchor="end">${v.toFixed(2)}</text>
        `;
    }).join('');

    // X-axis ticks — format depends on the range being shown
    const xTickCount = 5;
    const spanMs = tRange;
    // Rules:
    //   <= 90min window:  HH:MM:SS
    //   <= 24h window:    HH:MM
    //   <= 7d window:     MM/DD HH:MM  (first tick and midnight-crossers show MM/DD)
    //   >  7d window:     MM/DD
    const fmt = (d) => {
        const pad2 = n => String(n).padStart(2, '0');
        const MM = pad2(d.getMonth() + 1);
        const DD = pad2(d.getDate());
        const hh = pad2(d.getHours());
        const mm = pad2(d.getMinutes());
        const ss = pad2(d.getSeconds());
        if (spanMs <= 90 * 60 * 1000)          return { top: `${hh}:${mm}:${ss}`, bot: `${MM}/${DD}` };
        if (spanMs <= 24 * 60 * 60 * 1000)     return { top: `${hh}:${mm}`, bot: `${MM}/${DD}` };
        if (spanMs <= 7 * 24 * 60 * 60 * 1000) return { top: `${hh}:${mm}`, bot: `${MM}/${DD}` };
        return { top: `${MM}/${DD}`, bot: `${d.getFullYear()}` };
    };

    const xLabels = Array.from({ length: xTickCount + 1 }, (_, i) => {
        const t = tMin + (tRange * i / xTickCount);
        const xp = x(t);
        const parts = fmt(new Date(t));
        return `
            <text x="${xp}" y="${H - 18}" font-size="9" fill="#666" text-anchor="middle">${parts.top}</text>
            <text x="${xp}" y="${H - 6}"  font-size="8" fill="#aaa" text-anchor="middle">${parts.bot}</text>
        `;
    }).join('');

    wrap.innerHTML = `
        <svg id="hist-chart-svg" width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
            <g>${yLabels}</g>
            <g>${xLabels}</g>
            ${bandPolygon}
            ${linePolyline}
            ${dots}
        </svg>
    `;
}