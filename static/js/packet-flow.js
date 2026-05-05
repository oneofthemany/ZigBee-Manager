/**
 * Packet Flow Panel
 * =================
 *
 * Renders the live packet-flow widget inside the existing
 * `#debugPacketsModal`:
 *   - global rate readout (1s / 10s / 60s)
 *   - 60-second sparkline (inline SVG, no deps)
 *   - top-talkers table
 *   - per-cluster table
 *   - anomaly badges
 *
 * Data arrives via `packet_flow` WebSocket messages (every 2s) routed by
 * websocket.js. A single REST snapshot is fetched on first init so the panel
 * isn't empty before the first WS push lands.
 */

import { state } from './state.js';

// Cache of last snapshot — also enables a quick re-render if the user opens
// the modal between WS pushes.
let _last = null;
let _initialised = false;

/**
 * One-shot initialisation. Safe to call multiple times.
 * Triggers a single REST fetch so the panel has data even before the first
 * WebSocket push arrives (~2s window otherwise).
 */
export function initPacketFlow() {
    if (_initialised) {
        // Already running — just re-render whatever we last received.
        if (_last) renderFlowPanel(_last);
        return;
    }
    _initialised = true;

    fetch('/api/debug/flow?top_n=10&history=60')
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (data && !data.error) {
                _last = data;
                renderFlowPanel(data);
            }
        })
        .catch(() => { /* silent — WS push will fill it in */ });
}

/**
 * Handle an inbound `packet_flow` WS message.
 * Called from websocket.js.
 */
export function handlePacketFlow(payload) {
    if (!payload) return;
    _last = payload;
    // Only re-render if any flow DOM is present (cheap guard, avoids work
    // when the modal hasn't been opened yet).
    if (document.getElementById('flowGlobalRate')) {
        renderFlowPanel(payload);
    }
}

/**
 * Force a clear of the panel.
 */
export function clearPacketFlow() {
    _last = null;
    const ids = ['flowGlobalRate', 'flowSparkline', 'flowAnomalies',
                 'flowTopTalkers', 'flowClusters'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
    });
}

// --- internal renderers ----------------------------------------------------

function renderFlowPanel(p) {
    renderGlobalRate(p.global || {});
    renderSparkline(p.history || []);
    renderAnomalies(p.anomalies || []);
    renderTopTalkers(p.devices || []);
    renderClusters(p.clusters || []);
}

function renderGlobalRate(g) {
    const el = document.getElementById('flowGlobalRate');
    if (!el) return;
    const r1  = (g.rate_1s  || 0).toFixed(1);
    const r10 = (g.rate_10s || 0).toFixed(1);
    const r60 = (g.rate_60s || 0).toFixed(1);
    const total = g.total || 0;
    el.innerHTML = `
        <span class="badge bg-primary me-1" title="Last 1 second">${r1} pps</span>
        <span class="badge bg-info me-1" title="Last 10 seconds">${r10} pps</span>
        <span class="badge bg-secondary me-1" title="Last 60 seconds">${r60} pps</span>
        <span class="text-muted small">total ${total.toLocaleString()}</span>`;
}

function renderSparkline(history) {
    const el = document.getElementById('flowSparkline');
    if (!el) return;
    if (!history || history.length === 0) {
        el.innerHTML = '';
        return;
    }
    const w = 200, h = 30;
    const max = Math.max(1, ...history);
    const step = w / Math.max(1, history.length - 1);
    const points = history.map((v, i) => {
        const x = i * step;
        const y = h - (v / max) * h;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    // Build a filled area below the line for visual weight.
    const areaPts = points + ` ${w},${h} 0,${h}`;
    el.innerHTML = `
        <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}"
             preserveAspectRatio="none"
             style="display:block;overflow:visible;">
            <polygon fill="rgba(13,110,253,0.15)" points="${areaPts}"/>
            <polyline fill="none" stroke="#0d6efd" stroke-width="1.5"
                      points="${points}"/>
        </svg>
        <div class="d-flex justify-content-between text-muted"
             style="font-size:9px;line-height:1;">
            <span>-60s</span><span>now</span>
        </div>`;
}

function renderAnomalies(anoms) {
    const el = document.getElementById('flowAnomalies');
    if (!el) return;
    if (!anoms.length) {
        el.innerHTML = '<span class="badge bg-success">No anomalies</span>';
        return;
    }
    el.innerHTML = anoms.slice(0, 5).map(a => {
        const dev = state.deviceCache[a.ieee] || {};
        const name = dev.friendly_name || dev.name
                  || (a.ieee ? a.ieee.substring(a.ieee.length - 8) : '—');
        const ratioTxt = a.ratio == null ? 'new' : `${a.ratio.toFixed(1)}×`;
        const tip = `baseline ${a.baseline.toFixed(1)}/min, ` +
                    `current ${a.current.toFixed(1)}/min`;
        return `<span class="badge bg-warning text-dark me-1 mb-1"
                      title="${_esc(tip)}">
                    <i class="fas fa-bolt"></i> ${_esc(name)} (${ratioTxt})
                </span>`;
    }).join('');
}

function renderTopTalkers(devs) {
    const el = document.getElementById('flowTopTalkers');
    if (!el) return;
    if (!devs.length) {
        el.innerHTML = '<div class="text-muted small">No traffic.</div>';
        return;
    }
    let html = `
        <table class="table table-sm table-borderless mb-0" style="font-size:0.8rem;">
            <thead class="text-muted">
                <tr>
                    <th>Device</th>
                    <th class="text-end">1s</th>
                    <th class="text-end">10s</th>
                    <th class="text-end">60s</th>
                    <th class="text-end" title="EWMA baseline /min">base</th>
                </tr>
            </thead>
            <tbody>`;
    devs.slice(0, 10).forEach(d => {
        const dev = state.deviceCache[d.ieee] || {};
        const name = dev.friendly_name || dev.name
                  || (d.ieee ? d.ieee.substring(d.ieee.length - 8) : '—');
        const cls = d.rate_60s >= 60 ? 'text-warning fw-bold'
                  : d.rate_60s >= 10 ? 'text-info' : '';
        html += `<tr>
            <td class="text-truncate" style="max-width:180px;"
                title="${_esc(d.ieee)}">${_esc(name)}</td>
            <td class="text-end ${cls}">${d.rate_1s.toFixed(1)}</td>
            <td class="text-end ${cls}">${d.rate_10s.toFixed(1)}</td>
            <td class="text-end ${cls}">${d.rate_60s.toFixed(1)}</td>
            <td class="text-end text-muted">${d.baseline.toFixed(1)}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
}

function renderClusters(clusters) {
    const el = document.getElementById('flowClusters');
    if (!el) return;
    if (!clusters.length) {
        el.innerHTML = '<div class="text-muted small">No traffic.</div>';
        return;
    }
    let html = `
        <table class="table table-sm table-borderless mb-0" style="font-size:0.8rem;">
            <thead class="text-muted">
                <tr>
                    <th>Cluster</th>
                    <th class="text-end">10s</th>
                    <th class="text-end">60s</th>
                </tr>
            </thead>
            <tbody>`;
    clusters.slice(0, 10).forEach(c => {
        const hex = '0x' + c.cluster.toString(16).padStart(4, '0').toUpperCase();
        html += `<tr>
            <td>${_esc(c.cluster_name)} <span class="text-muted small">${hex}</span></td>
            <td class="text-end">${c.rate_10s.toFixed(1)}</td>
            <td class="text-end">${c.rate_60s.toFixed(1)}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
}

function _esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}