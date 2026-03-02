/**
 * settings.js
 * Rich Settings Panel - Config / Security / Spectrum tabs
 * Replaces the raw YAML textarea with a structured form UI.
 */

 // ============================================================================
 // IMPORTS
 // ============================================================================

 import { loadSSLStatus } from './system.js';

// ============================================================================
// STATE
// ============================================================================

let _spectrumChart = null;
let _spectrumData = {};
let _currentConfig = {};

// ============================================================================
// INIT
// ============================================================================

export function initSettings() {
    const tab = document.querySelector('[data-bs-target="#settings"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadSettingsPanel());
    }
}

export async function loadSettingsPanel() {
    await loadStructuredConfig();
    await loadSSLStatus();
}

// ============================================================================
// STRUCTURED CONFIG LOAD / SAVE
// ============================================================================

async function loadStructuredConfig() {
    try {
        const res = await fetch('/api/config/structured');
        const data = await res.json();
        if (!data.success) {
            showSettingsAlert('danger', 'Failed to load config: ' + data.error);
            return;
        }
        _currentConfig = data.config;
        renderConfigTab(data.config);
        renderSecurityTab(data.config);
    } catch (e) {
        showSettingsAlert('danger', 'Error loading config: ' + e.message);
    }
}

async function saveStructuredConfig() {
    const config = collectFormValues();
    try {
        const res = await fetch('/api/config/structured', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });
        const data = await res.json();
        if (data.success) {
            showSettingsAlert('success', 'Configuration saved. Restart the service to apply.');
        } else {
            showSettingsAlert('danger', 'Save failed: ' + data.error);
        }
    } catch (e) {
        showSettingsAlert('danger', 'Error saving: ' + e.message);
    }
}

// ============================================================================
// CONFIG TAB RENDER
// ============================================================================

function renderConfigTab(config) {
    const z = config.zigbee || {};
    const m = config.mqtt || {};
    const w = config.web || {};
    const l = config.logging || {};

    const el = document.getElementById('configFormBody');
    if (!el) return;

    el.innerHTML = `
    <!-- ZIGBEE SECTION -->
    <h6 class="text-uppercase text-muted fw-bold mb-3 mt-2 small">
      <i class="fas fa-broadcast-tower me-1"></i> Zigbee Radio
    </h6>
    <div class="row g-3 mb-4">
      <div class="col-md-5">
        <label class="form-label small fw-semibold">Serial Port</label>
        <input type="text" class="form-control" id="cfg_port" value="${z.port || ''}" placeholder="/dev/ttyACM0">
        <div class="form-text">Serial path or socket URI (e.g. <code>socket://127.0.0.1:9999</code> for MultiPAN)</div>
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Radio Type</label>
        <select class="form-select" id="cfg_radio_type">
          ${['auto','ezsp','znp','deconz'].map(t =>
            `<option value="${t}" ${z.radio_type === t ? 'selected' : ''}>${t}</option>`
          ).join('')}
        </select>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Channel</label>
        <select class="form-select" id="cfg_channel">
          ${Array.from({length: 16}, (_, i) => i + 11).map(ch =>
            `<option value="${ch}" ${z.channel == ch ? 'selected' : ''}>${ch}</option>`
          ).join('')}
        </select>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Topology Scan (s)</label>
        <input type="number" class="form-control" id="cfg_topology_scan_interval"
               value="${z.topology_scan_interval || 120}" min="0">
      </div>
    </div>

    <!-- MQTT SECTION -->
    <h6 class="text-uppercase text-muted fw-bold mb-3 mt-2 small">
      <i class="fas fa-network-wired me-1"></i> MQTT Broker
    </h6>
    <div class="row g-3 mb-4">
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Broker Host</label>
        <input type="text" class="form-control" id="cfg_mqtt_host" value="${m.broker_host || ''}">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Port</label>
        <input type="number" class="form-control" id="cfg_mqtt_port" value="${m.broker_port || 1883}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Username</label>
        <input type="text" class="form-control" id="cfg_mqtt_username" value="${m.username || ''}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Password</label>
        <input type="password" class="form-control" id="cfg_mqtt_password" value="${m.password || ''}"
               placeholder="(unchanged)">
      </div>
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Base Topic</label>
        <input type="text" class="form-control" id="cfg_mqtt_base_topic" value="${m.base_topic || 'zigbee_manager'}">
      </div>
    </div>

    <!-- WEB SECTION -->
    <h6 class="text-uppercase text-muted fw-bold mb-3 mt-2 small">
      <i class="fas fa-globe me-1"></i> Web Interface
    </h6>
    <div class="row g-3 mb-4">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Host</label>
        <input type="text" class="form-control" id="cfg_web_host" value="${w.host || '0.0.0.0'}">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Port</label>
        <input type="number" class="form-control" id="cfg_web_port" value="${w.port || 8000}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Log Level</label>
        <select class="form-select" id="cfg_log_level">
          ${['DEBUG','INFO','WARNING','ERROR'].map(lv =>
            `<option value="${lv}" ${l.level === lv ? 'selected' : ''}>${lv}</option>`
          ).join('')}
        </select>
      </div>
    </div>
    `;
}

// ============================================================================
// SECURITY TAB RENDER
// ============================================================================

function renderSecurityTab(config) {
    const z = config.zigbee || {};
    const el = document.getElementById('securityFormBody');
    if (!el) return;

    el.innerHTML = `
    <div class="alert alert-warning small mb-4">
      <i class="fas fa-exclamation-triangle me-1"></i>
      <strong>Warning:</strong> Changing PAN ID or Network Key will disconnect all paired devices.
      They will need to be re-paired.
    </div>

    <!-- PAN ID -->
    <div class="mb-4">
      <label class="form-label fw-semibold">PAN ID (16-bit hex)</label>
      <div class="input-group">
        <span class="input-group-text text-muted">0x</span>
        <input type="text" class="form-control font-monospace" id="cfg_pan_id"
               value="${z.pan_id || ''}" maxlength="4" placeholder="A1B2"
               pattern="[0-9A-Fa-f]{4}">
        <button class="btn btn-outline-secondary btn-sm" onclick="regenCredential('pan_id')"
                title="Generate random PAN ID">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">4-character hex. Must be unique on your 2.4GHz network.</div>
    </div>

    <!-- EXTENDED PAN ID -->
    <div class="mb-4">
      <label class="form-label fw-semibold">Extended PAN ID (64-bit)</label>
      <div class="input-group">
        <input type="text" class="form-control font-monospace" id="cfg_extended_pan_id"
               value="${z.extended_pan_id_hex || ''}" maxlength="16" placeholder="16 hex characters">
        <button class="btn btn-outline-secondary btn-sm" onclick="regenCredential('extended_pan_id')"
                title="Generate random Extended PAN ID">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">16-character hex (8 bytes). Stored as byte array in config.</div>
    </div>

    <!-- NETWORK KEY -->
    <div class="mb-4">
      <label class="form-label fw-semibold">Network Key (128-bit)</label>
      <div class="input-group">
        <input type="password" class="form-control font-monospace" id="cfg_network_key"
               value="${z.network_key_hex || ''}" maxlength="32"
               placeholder="32 hex characters (leave blank to keep current)">
        <button class="btn btn-outline-secondary btn-sm"
                onclick="document.getElementById('cfg_network_key').type === 'password'
                  ? (document.getElementById('cfg_network_key').type='text', this.innerHTML='<i class=\\'fas fa-eye-slash\\'></i>')
                  : (document.getElementById('cfg_network_key').type='password', this.innerHTML='<i class=\\'fas fa-eye\\'></i>')"
                title="Toggle visibility">
          <i class="fas fa-eye"></i>
        </button>
        <button class="btn btn-outline-danger btn-sm" onclick="regenCredential('network_key')"
                title="Generate new random network key">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">32-character hex (16 bytes). Changing this requires re-pairing ALL devices.</div>
    </div>

    <div id="regenResult" class="mt-2"></div>
    `;
}

// ============================================================================
// CREDENTIAL REGENERATION
// ============================================================================

window.regenCredential = async function(type) {
    const labels = {
        pan_id: 'PAN ID',
        extended_pan_id: 'Extended PAN ID',
        network_key: 'Network Key'
    };
    const warn = type === 'network_key'
        ? '\n\nWARNING: All devices will need to be re-paired!'
        : '';

    if (!confirm(`Regenerate ${labels[type]}?${warn}`)) return;

    try {
        const res = await fetch('/api/zigbee/credentials/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [type]: true })
        });
        const data = await res.json();
        if (data.success) {
            const r = data.regenerated;
            if (type === 'pan_id' && r.pan_id) {
                document.getElementById('cfg_pan_id').value = r.pan_id;
            }
            if (type === 'extended_pan_id' && r.extended_pan_id_hex) {
                document.getElementById('cfg_extended_pan_id').value = r.extended_pan_id_hex;
            }
            if (type === 'network_key' && r.network_key_hex) {
                document.getElementById('cfg_network_key').value = r.network_key_hex;
                document.getElementById('cfg_network_key').type = 'text';
            }
            document.getElementById('regenResult').innerHTML =
                `<div class="alert alert-success small">${labels[type]} regenerated and saved. Restart to apply.</div>`;
        } else {
            document.getElementById('regenResult').innerHTML =
                `<div class="alert alert-danger small">Error: ${data.error}</div>`;
        }
    } catch (e) {
        document.getElementById('regenResult').innerHTML =
            `<div class="alert alert-danger small">Request failed: ${e.message}</div>`;
    }
};

// ============================================================================
// SPECTRUM ANALYSIS
// ============================================================================

export async function runSpectrumScan() {
    const btn = document.getElementById('spectrumScanBtn');
    const statusEl = document.getElementById('spectrumStatus');
    const autoBtn = document.getElementById('autoChannelBtn');

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Scanning...'; }
    if (statusEl) statusEl.textContent = 'Running energy scan across channels 11-26...';
    if (autoBtn) autoBtn.disabled = true;

    try {
        const res = await fetch('/api/zigbee/spectrum');
        const data = await res.json();

        if (!data.success) {
            if (statusEl) statusEl.textContent = 'Scan failed: ' + data.error;
            return;
        }

        _spectrumData = data.spectrum;
        renderSpectrumChart(data);

        if (statusEl) {
            statusEl.textContent =
                `Scan complete. Best channel: ${data.best_channel}` +
                (data.current_channel ? ` (current: ${data.current_channel})` : '');
        }

        if (autoBtn) autoBtn.disabled = false;

    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-satellite-dish me-1"></i> Scan Spectrum'; }
    }
}

function renderSpectrumChart(data) {
    const container = document.getElementById('spectrumChart');
    if (!container) return;

    const channels = Object.keys(data.spectrum).map(Number).sort((a,b) => a-b);
    const energies = channels.map(ch => data.spectrum[ch]);
    const best = data.best_channel;
    const current = data.current_channel;

    // Color: green=low interference, yellow=medium, red=high. Best=blue, current=purple
    const colors = channels.map(ch => {
        if (ch === current && ch === best) return '#6f42c1';
        if (ch === best) return '#0d6efd';
        if (ch === current) return '#6f42c1';
        const e = data.spectrum[ch];
        if (e < 80) return '#198754';
        if (e < 150) return '#ffc107';
        return '#dc3545';
    });

    // Wi-Fi overlap annotations (typical)
    const wifiOverlap = {
        11: 'WiFi-1', 12: 'WiFi-1', 13: 'WiFi-1',
        16: 'WiFi-6', 17: 'WiFi-6',
        21: 'WiFi-11', 22: 'WiFi-11'
    };

    // Simple SVG chart (no external deps beyond what's available)
    const W = container.clientWidth || 700;
    const H = 260;
    const padL = 40, padR = 20, padT = 20, padB = 60;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const barW = Math.floor(plotW / channels.length) - 4;

    const bars = channels.map((ch, i) => {
        const x = padL + i * (plotW / channels.length) + 2;
        const barH = Math.round((energies[i] / 255) * plotH);
        const y = padT + plotH - barH;
        const label = wifiOverlap[ch] ? `<text x="${x + barW/2}" y="${y - 4}"
          text-anchor="middle" font-size="8" fill="#888">${wifiOverlap[ch]}</text>` : '';
        const badge = ch === best ? '★' : (ch === current ? '●' : '');
        return `
          <rect x="${x}" y="${y}" width="${barW}" height="${barH}"
                fill="${colors[i]}" rx="2"
                data-channel="${ch}" data-energy="${energies[i]}"/>
          ${label}
          <text x="${x + barW/2}" y="${padT + plotH + 16}" text-anchor="middle"
                font-size="11" fill="${ch === best ? '#0d6efd' : (ch === current ? '#6f42c1' : '#555')}"
                font-weight="${(ch === best || ch === current) ? 'bold' : 'normal'}">
            ${ch}${badge}
          </text>
          <text x="${x + barW/2}" y="${y - 4}" text-anchor="middle" font-size="9" fill="${colors[i]}">
            ${badge === '★' || badge === '●' ? badge : ''}
          </text>
        `;
    }).join('');

    // Y axis ticks
    const yTicks = [0, 64, 128, 192, 255].map(v => {
        const y = padT + plotH - Math.round((v / 255) * plotH);
        return `<line x1="${padL - 4}" y1="${y}" x2="${padL + plotW}" y2="${y}"
                      stroke="#e0e0e0" stroke-width="1"/>
                <text x="${padL - 6}" y="${y + 4}" text-anchor="end" font-size="9" fill="#888">${v}</text>`;
    }).join('');

    container.innerHTML = `
      <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}">
        ${yTicks}
        ${bars}
        <!-- Axes -->
        <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + plotH}" stroke="#ccc" stroke-width="1"/>
        <line x1="${padL}" y1="${padT + plotH}" x2="${padL + plotW}" y2="${padT + plotH}" stroke="#ccc" stroke-width="1"/>
        <!-- Labels -->
        <text x="${padL + plotW/2}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#666">
          ZigBee Channel
        </text>
        <text x="12" y="${padT + plotH/2}" text-anchor="middle" font-size="10" fill="#666"
              transform="rotate(-90, 12, ${padT + plotH/2})">Energy</text>
      </svg>
      <div class="mt-2 d-flex gap-3 flex-wrap small">
        <span><span style="display:inline-block;width:12px;height:12px;background:#198754;border-radius:2px;"></span> Low interference</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#ffc107;border-radius:2px;"></span> Medium</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#dc3545;border-radius:2px;"></span> High interference</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#0d6efd;border-radius:2px;"></span> Best ★</span>
        ${current ? `<span><span style="display:inline-block;width:12px;height:12px;background:#6f42c1;border-radius:2px;"></span> Current ●</span>` : ''}
      </div>
    `;
}

window.autoSelectChannel = async function() {
    if (!confirm('Run spectrum scan and automatically set the best channel in config?\nA service restart will be required to apply.')) return;

    const btn = document.getElementById('autoChannelBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Selecting...'; }

    try {
        const res = await fetch('/api/zigbee/channel/auto', { method: 'POST' });
        const data = await res.json();
        const statusEl = document.getElementById('spectrumStatus');

        if (data.success) {
            // Update the channel dropdown in config tab
            const sel = document.getElementById('cfg_channel');
            if (sel) sel.value = data.selected_channel;

            if (statusEl) statusEl.innerHTML =
                `<span class="text-success fw-semibold"><i class="fas fa-check me-1"></i>Channel ${data.selected_channel} selected and saved. ${data.message}</span>`;

            // Re-render chart with new data
            if (data.spectrum) {
                renderSpectrumChart({ spectrum: data.spectrum, best_channel: data.selected_channel });
            }
        } else {
            if (statusEl) statusEl.innerHTML = `<span class="text-danger">Error: ${data.error}</span>`;
        }
    } catch (e) {
        document.getElementById('spectrumStatus').textContent = 'Request failed: ' + e.message;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-magic me-1"></i> Auto Select Best Channel'; }
    }
};


// ============================================================================
// HISTORY CHART
// ============================================================================

export async function loadSpectrumHistory() {
    const hours = parseInt(document.getElementById('historyHours')?.value || 24);
    const container = document.getElementById('spectrumHistory');
    const meta = document.getElementById('spectrumHistoryMeta');
    if (!container) return;

    container.innerHTML = '<div class="text-center text-muted py-3 small"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        const [histRes, avgRes] = await Promise.all([
            fetch(`/api/zigbee/spectrum/history?hours=${hours}`).then(r => r.json()),
            fetch(`/api/zigbee/spectrum/averages?hours=${hours}`).then(r => r.json())
        ]);

        if (!histRes.success || !histRes.records.length) {
            container.innerHTML = '<div class="text-center text-muted py-4 small">No history yet — background scans run hourly.</div>';
            return;
        }

        renderHistoryChart(histRes.records, avgRes.averages || {}, hours, container);

        if (meta) {
            const count = histRes.records.length / 16; // 16 channels per scan
            meta.textContent = `${Math.round(count)} scans over the last ${hours}h`;
        }

    } catch (e) {
        container.innerHTML = `<div class="text-center text-danger py-3 small">Error: ${e.message}</div>`;
    }
}

function renderHistoryChart(records, averages, hours, container) {
    // Group records by channel, build time series per channel
    const byChannel = {};
    for (const r of records) {
        if (!byChannel[r.channel]) byChannel[r.channel] = [];
        byChannel[r.channel].push({ ts: r.timestamp, energy: r.energy });
    }

    const channels = Object.keys(byChannel).map(Number).sort((a, b) => a - b);
    if (!channels.length) return;

    // Average energy per channel across the period
    const avgPerChannel = channels.map(ch => {
        const vals = byChannel[ch].map(p => p.energy);
        return { ch, avg: Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) };
    });

    // Find best (lowest avg) and worst (highest avg) channels
    const best = avgPerChannel.reduce((a, b) => a.avg < b.avg ? a : b);
    const worst = avgPerChannel.reduce((a, b) => a.avg > b.avg ? a : b);

    // Build SVG bar chart of averages with min/max range indicators
    const W = container.clientWidth || 680;
    const H = 200;
    const padL = 40, padR = 20, padT = 16, padB = 50;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const barW = Math.floor(plotW / channels.length) - 4;

    const bars = avgPerChannel.map(({ ch, avg }, i) => {
        const x = padL + i * (plotW / channels.length) + 2;
        const barH = Math.round((avg / 255) * plotH);
        const y = padT + plotH - barH;

        // Range: min/max as thin overlay
        const vals = byChannel[ch].map(p => p.energy);
        const minE = Math.min(...vals);
        const maxE = Math.max(...vals);
        const rangeTop = padT + plotH - Math.round((maxE / 255) * plotH);
        const rangeH = Math.round(((maxE - minE) / 255) * plotH);

        const color = ch === best.ch ? '#0d6efd'
                    : avg < 80 ? '#198754'
                    : avg < 150 ? '#ffc107'
                    : '#dc3545';

        return `
          <!-- range indicator -->
          <rect x="${x + barW/2 - 1}" y="${rangeTop}" width="2" height="${Math.max(rangeH, 2)}"
                fill="${color}" opacity="0.3"/>
          <!-- avg bar -->
          <rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${color}" rx="2" opacity="0.85"/>
          <!-- channel label -->
          <text x="${x + barW/2}" y="${padT + plotH + 14}" text-anchor="middle" font-size="10"
                fill="${ch === best.ch ? '#0d6efd' : '#555'}"
                font-weight="${ch === best.ch ? 'bold' : 'normal'}">${ch}${ch === best.ch ? '★' : ''}</text>
          <!-- avg value -->
          ${avg > 20 ? `<text x="${x + barW/2}" y="${y - 3}" text-anchor="middle" font-size="8" fill="${color}">${avg}</text>` : ''}
        `;
    }).join('');

    // Y ticks
    const yTicks = [0, 64, 128, 192, 255].map(v => {
        const y = padT + plotH - Math.round((v / 255) * plotH);
        return `<line x1="${padL - 4}" y1="${y}" x2="${padL + plotW}" y2="${y}" stroke="#e8e8e8" stroke-width="1"/>
                <text x="${padL - 6}" y="${y + 4}" text-anchor="end" font-size="9" fill="#aaa">${v}</text>`;
    }).join('');

    container.innerHTML = `
      <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}">
        ${yTicks}
        ${bars}
        <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + plotH}" stroke="#ccc" stroke-width="1"/>
        <line x1="${padL}" y1="${padT + plotH}" x2="${padL + plotW}" y2="${padT + plotH}" stroke="#ccc" stroke-width="1"/>
        <text x="${padL + plotW/2}" y="${H - 4}" text-anchor="middle" font-size="10" fill="#888">Channel (avg energy, bars show min/max range)</text>
      </svg>
      <div class="d-flex gap-3 mt-1 flex-wrap small">
        <span class="text-success"><i class="fas fa-check-circle me-1"></i>Best avg: ch ${best.ch} (${best.avg})</span>
        <span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>Most noisy: ch ${worst.ch} (${worst.avg})</span>
      </div>
    `;
}

// ============================================================================
// FORM VALUE COLLECTION
// ============================================================================

function collectFormValues() {
    const get = id => document.getElementById(id)?.value?.trim() ?? null;
    const getNum = id => { const v = get(id); return v !== null ? Number(v) : null; };

    return {
        zigbee: {
            port: get('cfg_port'),
            radio_type: get('cfg_radio_type'),
            channel: getNum('cfg_channel'),
            topology_scan_interval: getNum('cfg_topology_scan_interval'),
            pan_id: get('cfg_pan_id'),
            extended_pan_id_hex: get('cfg_extended_pan_id'),
            network_key_hex: get('cfg_network_key') || null,  // null = don't overwrite
        },
        mqtt: {
            broker_host: get('cfg_mqtt_host'),
            broker_port: getNum('cfg_mqtt_port'),
            username: get('cfg_mqtt_username'),
            password: get('cfg_mqtt_password') || undefined,
            base_topic: get('cfg_mqtt_base_topic'),
        },
        web: {
            host: get('cfg_web_host'),
            port: getNum('cfg_web_port'),
        },
        logging: {
            level: get('cfg_log_level'),
        }
    };
}

// ============================================================================
// EXPORT GLOBALS FOR HTML INLINE ONCLICK
// ============================================================================

export async function saveSettingsConfig() {
    await saveStructuredConfig();
}

window.runSpectrumScan = runSpectrumScan;
window.saveSettingsConfig = saveSettingsConfig;
window.loadSpectrumHistory = loadSpectrumHistory;

// ============================================================================
// UTILITIES
// ============================================================================

function showSettingsAlert(type, msg) {
    const el = document.getElementById('settingsAlert');
    if (!el) return;
    el.className = `alert alert-${type} small`;
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 6000);
}