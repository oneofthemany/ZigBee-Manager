/**
 * Packet Flow Panel
 * =================
 *
 * Renders the live packet-flow widget inside the existing
 * `#debugPacketsModal`:
 *   - global rate readout (1s / 10s / 60s)
 *   - peak 1s rate over the last hour (burst awareness)
 *   - RX / TX split + tracked-device count
 *   - 60-second sparkline (inline SVG, no deps)
 *   - hourly statistical summary: mean, stddev, CV, P50/P95/P99
 *   - top-5 peak history with timestamps + dominant device attribution
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
 */
export function initPacketFlow() {
    if (_initialised) {
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
 */
export function handlePacketFlow(payload) {
    if (!payload) return;
    _last = payload;
    if (document.getElementById('flowGlobalRate')) {
        renderFlowPanel(payload);
    }
}

/**
 * Force a clear of the panel.
 */
export function clearPacketFlow() {
    _last = null;
    const ids = ['flowGlobalRate', 'flowPeakLine', 'flowSparkline',
                 'flowAnomalies', 'flowTopTalkers', 'flowClusters',
                 'flowStatsSummary', 'flowPeakHistory'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
    });
}

// --- internal renderers ----------------------------------------------------

function renderFlowPanel(p) {
    renderGlobalRate(p.global || {});
    renderPeakLine(p.global || {});
    renderSparkline(p.history || []);
    renderStatsSummary(p.stats || {});
    renderPeakHistory(p.stats || {});
    renderAnomalies(p.anomalies || []);
    renderTopTalkers(p.devices || []);
    renderClusters(p.clusters || []);
}

function renderGlobalRate(g) {
    const el = document.getElementById('flowGlobalRate');
    if (!el) return;
    const r1   = (g.rate_1s  || 0).toFixed(1);
    const r10  = (g.rate_10s || 0).toFixed(1);
    const r60  = (g.rate_60s || 0).toFixed(1);
    const total = g.total || 0;
    const rx    = g.rx || 0;
    const tx    = g.tx || 0;
    const tDev  = g.tracked_devices  || 0;
    const tClu  = g.tracked_clusters || 0;

    const totalDir = rx + tx;
    const rxPct = totalDir > 0 ? Math.round((rx / totalDir) * 100) : 0;
    const txPct = totalDir > 0 ? 100 - rxPct : 0;

    el.innerHTML = `
        <span class="badge bg-primary me-1" title="Last 1 second">${r1} pps</span>
        <span class="badge bg-info me-1"    title="Last 10 seconds">${r10} pps</span>
        <span class="badge bg-secondary me-1" title="Last 60 seconds">${r60} pps</span>
        <span class="badge bg-success me-1" title="RX packets received cumulatively">
            RX ${rx.toLocaleString()} <small>(${rxPct}%)</small>
        </span>
        <span class="badge bg-warning text-dark me-1" title="TX packets sent cumulatively">
            TX ${tx.toLocaleString()} <small>(${txPct}%)</small>
        </span>
        <span class="text-muted small ms-1">
            total ${total.toLocaleString()}
            · ${tDev} dev · ${tClu} clu
        </span>`;
}

/**
 * Peak 1s rate over the last hour — colour-coded against the appender
 * decision matrix.
 */
function renderPeakLine(g) {
    const el = document.getElementById('flowPeakLine');
    if (!el) return;
    const peak = g.peak_1s_last_hour;
    if (peak == null || peak <= 0) {
        el.innerHTML = '';
        return;
    }
    const ageSec = g.peak_1s_age_sec;
    const ageTxt = _formatAge(ageSec);

    let cls = 'bg-success', label = 'comfortable';
    if (peak >= 500)      { cls = 'bg-danger';            label = 'heavy — appender essential'; }
    else if (peak >= 100) { cls = 'bg-warning text-dark'; label = 'busy — appender justified'; }
    else if (peak >= 30)  { cls = 'bg-info';              label = 'normal'; }

    el.innerHTML = `
        <span class="badge ${cls} me-1"
              title="Highest packets-per-second observed in the last hour">
            <i class="fas fa-bolt"></i>
            peak ${peak} pps
        </span>
        <span class="text-muted small">
            ${ageTxt ? `${ageTxt} ago · ` : ''}${label}
        </span>`;
}

function _formatAge(sec) {
    if (sec == null || sec < 0) return '';
    if (sec < 60)   return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m`;
    return `${Math.floor(sec / 3600)}h`;
}

function _formatTime(unixSec) {
    if (!unixSec) return '—';
    const d = new Date(unixSec * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
}

/**
 * Statistical summary over the last hour (mean, stddev, CV, percentiles,
 * burst counter). Hidden until at least 30 seconds of samples accumulate
 * (otherwise the numbers are too noisy to be useful).
 */
function renderStatsSummary(s) {
    const el = document.getElementById('flowStatsSummary');
    if (!el) return;
    const samples = s.samples || 0;
    if (samples < 30) {
        el.innerHTML = `
            <div class="text-muted small">
                <i class="fas fa-hourglass-start"></i>
                Collecting samples (${samples}s of 30s minimum)…
            </div>`;
        return;
    }

    const mean = (s.mean || 0).toFixed(2);
    const stddev = (s.stddev || 0).toFixed(2);
    const cv = (s.cv || 0).toFixed(2);

    // Burstiness verdict from coefficient of variation
    let cvLabel = 'steady';
    let cvCls = 'bg-success';
    if (s.cv >= 1.5)      { cvLabel = 'very bursty'; cvCls = 'bg-danger'; }
    else if (s.cv >= 0.5) { cvLabel = 'bursty';      cvCls = 'bg-warning text-dark'; }

    const burstThr = s.burst_threshold != null ? s.burst_threshold.toFixed(2) : '—';
    const burstPct = (s.burst_pct || 0).toFixed(2);
    const windowMin = Math.round(samples / 60);

    el.innerHTML = `
        <div class="d-flex flex-wrap gap-2 align-items-center">
            <small class="text-muted text-uppercase fw-bold me-1">
                Stats <span class="text-muted text-lowercase">(last ${windowMin}m)</span>
            </small>
            <span class="badge bg-light text-dark border" title="Mean packets per second">
                μ ${mean}
            </span>
            <span class="badge bg-light text-dark border" title="Standard deviation">
                σ ${stddev}
            </span>
            <span class="badge ${cvCls}" title="Coefficient of variation (σ/μ) — ${cvLabel}">
                CV ${cv} · ${cvLabel}
            </span>
            <span class="badge bg-light text-dark border" title="Median packets per second">
                P50 ${s.p50}
            </span>
            <span class="badge bg-light text-dark border" title="95th percentile — 95% of seconds at or below this rate">
                P95 ${s.p95}
            </span>
            <span class="badge bg-light text-dark border" title="99th percentile — only 1% of seconds exceeded this rate">
                P99 ${s.p99}
            </span>
            <span class="badge bg-light text-dark border" title="Maximum sample in window">
                max ${s.max}
            </span>
            <span class="badge bg-secondary"
                  title="Seconds where rate exceeded mean + 2σ (threshold: ${burstThr} pps)">
                <i class="fas fa-fire"></i>
                ${s.burst_count} bursts (${burstPct}%)
            </span>
        </div>`;
}

/**
 * Top-5 peaks of the last hour, with timestamp and dominant-device
 * attribution. The "dominant_pct" is critical for triage: if one device
 * accounts for >70% of a burst, that device is the suspect.
 */
function renderPeakHistory(s) {
    const el = document.getElementById('flowPeakHistory');
    if (!el) return;
    const peaks = s.top_peaks || [];
    if (!peaks.length) {
        el.innerHTML = '<div class="text-muted small">No peaks recorded yet.</div>';
        return;
    }

    let html = `
        <table class="table table-sm table-borderless mb-0" style="font-size:0.8rem;">
            <thead class="text-muted">
                <tr>
                    <th>When</th>
                    <th class="text-end">Rate</th>
                    <th>Top device</th>
                    <th class="text-end" title="% of that second's packets attributable to the top device">share</th>
                </tr>
            </thead>
            <tbody>`;
    peaks.forEach(p => {
        const dev = state.deviceCache?.[p.dominant_ieee] || {};
        const name = dev.friendly_name || dev.name
                  || (p.dominant_ieee
                      ? p.dominant_ieee.substring(p.dominant_ieee.length - 8)
                      : '—');
        const ageTxt = _formatAge(p.age_sec);
        const time   = _formatTime(p.ts);
        const pct    = p.dominant_pct != null ? `${p.dominant_pct}%` : '—';
        // Highlight if one device dominates the burst
        const pctCls = (p.dominant_pct != null && p.dominant_pct >= 70)
                       ? 'text-warning fw-bold' : 'text-muted';
        html += `<tr>
            <td>
                <span title="${time}">${ageTxt} ago</span>
            </td>
            <td class="text-end fw-bold">${p.rate}</td>
            <td class="text-truncate" style="max-width:180px;"
                title="${_esc(p.dominant_ieee || '—')}">
                ${_esc(name)}
            </td>
            <td class="text-end ${pctCls}">${pct}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
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
            <span>-60s</span>
            <span>peak ${max}/s</span>
            <span>now</span>
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