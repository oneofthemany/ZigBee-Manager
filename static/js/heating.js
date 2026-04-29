/**
 * heating.js
 * Frontend for the Heating Advisor dashboard, config and zone management.
 *
 * Consumes:
 *   GET  /api/heating/dashboard
 *   GET  /api/heating/history
 *   GET  /api/heating/preheat
 *   GET  /api/heating/config         POST to save
 *   GET  /api/heating/zones          POST to replace
 *   POST /api/heating/zones/{id}     DELETE to remove
 *   GET  /api/heating/thermostats
 *
 * Integration:
 *   - `initHeating()` called from main.js on DOMContentLoaded
 *   - Renders into <div id="heatingDashboard">
 *   - Auto-refreshes every 60s while the #heating tab is visible
 *   - Settings modal is injected into <body> once on init
 *   - Active controller panel is loaded by heating-controller.js when the
 *     dashboard renders and a #heatingControllerPanel div is present
 */

import {
    initHeatingController,
    loadControllerStatus,
} from './heating-controller.js';


// Chart palette tuned for both light and dark mode visibility.
// Each colour has ~4:1 contrast against both #f0f2f5 (light body) and
// #0f1117 (dark body). Verified with WebAIM contrast checker.
function getChartPalette() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return isDark
        ? ['#60a5fa',  // light blue
           '#f87171',  // light red
           '#4ade80',  // light green
           '#fbbf24',  // amber
           '#a78bfa',  // lavender
           '#fb923c',  // orange
           '#2dd4bf',  // teal
           '#f472b6',  // pink
           '#c084fc',  // purple
           '#38bdf8']  // sky
        : ['#1d4ed8',  // darker blue for white bg
           '#b91c1c',  // darker red
           '#15803d',  // darker green
           '#b45309',  // amber
           '#6d28d9',  // purple
           '#c2410c',  // orange
           '#0e7490',  // teal
           '#be185d',  // pink
           '#7e22ce',  // violet
           '#0369a1']; // deep sky
}

// ============================================================================
// STATE
// ============================================================================
let heatingRefreshTimer = null;
let heatingTabActive = false;
let lastDashboard = null;

let configCache = null;
let schemaCache = null;
let thermostatsCache = [];
let workingZones = [];   // zones being edited in the modal

const REFRESH_MS = 20_000;

const EPC_COLOURS = {
    A: '#008054', B: '#19b459', C: '#8dce46',
    D: '#ffd500', E: '#fcaa65', F: '#ef8023', G: '#e9153b',
};

const DAYS = [
    { key: 'mon', label: 'Mo' }, { key: 'tue', label: 'Tu' },
    { key: 'wed', label: 'We' }, { key: 'thu', label: 'Th' },
    { key: 'fri', label: 'Fr' }, { key: 'sat', label: 'Sa' },
    { key: 'sun', label: 'Su' },
];

// Re-render the chart immediately when the user toggles theme so colours
// and tooltip styling update without waiting for the next 20s tick.
document.addEventListener('themechange', () => {
    if (lastDashboard) {
        loadHeatingHistory();  // refetch + redraw
    }
});
// ============================================================================
// INITIALIZATION
// ============================================================================
export function initHeating() {
    console.log("Initializing Heating Module…");

    ensureSettingsModal();
    initHeatingController();

    const heatingTabBtn = document.querySelector('button[data-bs-target="#heating"]');
    if (!heatingTabBtn) {
        console.warn("Heating tab button not found");
        return;
    }

    heatingTabBtn.addEventListener('shown.bs.tab', () => {
        heatingTabActive = true;
        loadHeatingDashboard();
        startHeatingAutoRefresh();
    });
    heatingTabBtn.addEventListener('hidden.bs.tab', () => {
        heatingTabActive = false;
        stopHeatingAutoRefresh();
    });

    if (heatingTabBtn.classList.contains('active')) {
        heatingTabActive = true;
        loadHeatingDashboard();
        startHeatingAutoRefresh();
    }
}

function startHeatingAutoRefresh() {
    stopHeatingAutoRefresh();
    heatingRefreshTimer = setInterval(() => {
        if (heatingTabActive) loadHeatingDashboard({ silent: true });
    }, REFRESH_MS);
}

function stopHeatingAutoRefresh() {
    if (heatingRefreshTimer) clearInterval(heatingRefreshTimer);
    heatingRefreshTimer = null;
}

// ============================================================================
// DASHBOARD FETCH + RENDER
// ============================================================================
export async function loadHeatingDashboard({ silent = false } = {}) {
    const container = document.getElementById('heatingDashboard');
    if (!container) return;

    if (!silent && !lastDashboard) {
        container.innerHTML = spinnerBlock('Loading heating intelligence…');
    }

    try {
        // Force=1 on the user-initiated refresh, silent tick uses cache.
        const res = await fetch(`/api/heating/dashboard?force=${silent ? 0 : 1}`);
        const json = await res.json();

        if (!json.success) {
            container.innerHTML = renderDisabled(json.error || 'Heating advisor not enabled');
            bindTopBarControls();
            return;
        }

        lastDashboard = json.data;
        // Refresh runtime + controller overlay BEFORE rendering so the devices
        // table uses the controller's live truth for receivers and up-to-date
        // 24h on-time percentages.
        await Promise.all([
            loadHeatingRuntime(),
            loadControllerOverlay(),
            loadHeatingAnomalies(),
        ]);
        // Pull the controller config to expose the per-room list to the
        // Efficiency card (per-room thermal profile buttons).
        try {
            const cfgRes = await fetch('/api/heating/controller/config');
            const cfgJson = await cfgRes.json();
            if (cfgJson.success) {
                json.data._heating_config_circuits = cfgJson.config?.circuits || [];
            }
        } catch (e) { /* ignore */ }
        // Inject sensor-only rooms as pseudo-devices in the heating devices
        // table so bathrooms etc. are visible alongside TRV-equipped rooms.
        try {
            const ctrlRes = await fetch('/api/heating/controller/state');
            const ctrlJson = await ctrlRes.json();
            if (ctrlJson.success && ctrlJson.state) {
                const extraDevices = [];
                const known = new Set((json.data.heating?.devices || []).map(d => d.ieee));
                for (const c of (ctrlJson.state.circuits || [])) {
                    for (const r of (c.rooms || [])) {
                        if ((r.trvs || []).length > 0) continue;      // has TRVs already
                        if (!r.sensor_ieee) continue;                 // nothing to represent
                        if (known.has(r.sensor_ieee)) continue;       // already in table
                        // The device cache has the sensor's friendly name
                        const cachedDev = (window.state?.deviceCache || {})[r.sensor_ieee];
                        extraDevices.push({
                            ieee: r.sensor_ieee,
                            name: cachedDev?.friendly_name || `${r.name} (sensor)`,
                            temperature: r.current_temp,
                            setpoint: r.target_temp,
                            mode: 'sensor',
                            action: r.status === 'cold' ? 'calling' : 'idle',
                            effective_action: r.status === 'cold' ? 'calling' : 'idle',
                            running: false,
                            demand: 0,
                            _sensor_only: true,
                        });
                    }
                }
                if (extraDevices.length) {
                    json.data.heating.devices = (json.data.heating.devices || []).concat(extraDevices);
                }
            }
        } catch (e) { console.debug('Sensor-only injection skipped:', e); }
        // Preserve heatingControllerPanel contents across re-renders when
        // possible so the user doesn't see a spinner-flash every 20s.
        const prevPanel = document.getElementById('heatingControllerPanel');
        const prevHTML = prevPanel ? prevPanel.innerHTML : null;
        container.innerHTML = renderDashboard(json.data);
        if (prevHTML) {
            const newPanel = document.getElementById('heatingControllerPanel');
            if (newPanel) newPanel.innerHTML = prevHTML;
        }
        bindDashboardControls(json.data);
        bindTopBarControls();
        loadHeatingHistory();
        // Single-shot refresh: controller panel is fetched in-line with the
        // dashboard, not on its own interval. Matches the 20s cadence above.
        await loadControllerStatus();
    } catch (err) {
        console.error('Heating dashboard fetch failed:', err);
        if (!silent) {
            container.innerHTML = `<div class="alert alert-danger m-3">
                Failed to load heating dashboard: ${escapeHtml(err.message || String(err))}
            </div>`;
            bindTopBarControls();
        }
    }
}

async function loadHeatingHistory(hours = 24) {
    try {
        const res = await fetch(`/api/heating/history?hours=${hours}`);
        const json = await res.json();
        if (json.success) renderHistoryChart(json.data);
    } catch (err) {
        console.warn('Heating history fetch failed:', err);
    }
}

async function fetchPreheatRecommendation(targetTemp) {
    try {
        const url = targetTemp
            ? `/api/heating/preheat?target_temp=${encodeURIComponent(targetTemp)}`
            : '/api/heating/preheat';
        const res = await fetch(url);
        const json = await res.json();
        return json.success ? json.data : null;
    } catch (err) {
        console.error('Preheat fetch failed:', err);
        return null;
    }
}

// ============================================================================
// RENDER: dashboard
// ============================================================================
function topBar(subtitle) {
    return `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h5 class="mb-0"><i class="fas fa-temperature-high me-2"></i>Heating Intelligence</h5>
                <small class="text-muted">${subtitle}</small>
            </div>
            <div class="btn-group btn-group-sm">
                <button id="btn-heating-refresh" class="btn btn-outline-secondary" title="Refresh">
                    <i class="fas fa-sync-alt"></i>
                </button>
                <button id="btn-heating-settings" class="btn btn-outline-primary" title="Heating settings">
                    <i class="fas fa-cog"></i> Settings
                </button>
            </div>
        </div>`;
}

function renderDisabled(message) {
    return `
        <div class="container-fluid p-3">
            ${topBar('Configure your property, tariff and zones to enable smart heating')}
            <div class="card">
                <div class="card-body text-center py-5">
                    <i class="fas fa-temperature-high fa-3x text-muted mb-3"></i>
                    <h5>Heating Advisor Disabled</h5>
                    <p class="text-muted">${escapeHtml(message)}</p>
                    <button class="btn btn-primary" id="btn-heating-settings-alt">
                        <i class="fas fa-cog me-1"></i> Open Settings
                    </button>
                </div>
            </div>
        </div>`;
}

function renderDashboard(d) {
    const subtitle = `${escapeHtml(d.property?.type || 'property')} · ${escapeHtml(d.property?.insulation || '')} insulation · ${d.property?.floor_area_m2 || '?'}m² · ${escapeHtml(d.property?.boiler || 'boiler')} ${d.property?.boiler_kw || '?'}kW`;

    return `
        <div class="container-fluid p-3">
            ${topBar(subtitle)}
            <div class="text-muted small mb-3">Updated ${formatTs(d.ts)}</div>

            <div class="row g-3 mb-3">
                <div class="col-md-4">${renderEpcCard(d.epc, d.property)}</div>
                <div class="col-md-4">${renderTemperatureCard(d.outdoor, d.indoor, d.heating)}</div>
                <div class="col-md-4">${renderCostCard(d.cost, d.tariff, d.epc)}</div>
            </div>

            <div class="row g-3 mb-3">
                <div class="col-md-6">${renderPreheatCard(d.preheat)}</div>
                <div class="col-md-6">${renderForecastCard(d.outdoor)}</div>
            </div>

            <div id="heatingAnomaliesPanel" class="mb-3"></div>
            <div id="heatingControllerPanel"></div>

            ${renderZonesDashboard(d.zones || [])}

            <div class="card mb-3">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span><i class="fas fa-thermometer-half me-2"></i>Heating Devices</span>
                    <span class="badge bg-secondary">${(d.heating?.devices || []).length}</span>
                </div>
                ${renderDevicesTable(d.heating?.devices || [])}
            </div>

            <div class="card mb-3">
                <div class="card-header"><i class="fas fa-chart-line me-2"></i>24h History</div>
                <div class="card-body">
                    <div id="heatingHistoryChart" style="min-height:220px;">
                        ${spinnerBlock('Loading history…')}
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <i class="fas fa-lightbulb me-2"></i>Energy-Saving Tips
                    <span class="badge bg-secondary ms-2">${(d.tips || []).length}</span>
                </div>
                ${renderTips(d.tips || [])}
            </div>
        </div>`;
}

async function loadHeatingAnomalies() {
    const container = document.getElementById('heatingAnomaliesPanel');
    if (!container) return;
    try {
        const res = await fetch('/api/heating/anomalies');
        const json = await res.json();
        if (!json.success) { container.innerHTML = ''; return; }
        const data = json.data || {};
        const active = data.active || [];
        const recent = (data.recently_resolved || []).slice(0, 3);

        if (!active.length && !recent.length) {
            container.innerHTML = '';   // hide the section entirely when quiet
            return;
        }

        const severityBadge = s => s === 'critical'
            ? '<span class="badge bg-danger ms-1">critical</span>'
            : s === 'warning'
            ? '<span class="badge bg-warning text-dark ms-1">warning</span>'
            : '<span class="badge bg-info text-dark ms-1">info</span>';

        const kindIcon = k => k === 'fast_cool'
            ? '<i class="fas fa-snowflake text-info me-1"></i>'
            : '<i class="fas fa-hourglass-half text-warning me-1"></i>';

        const activeHtml = active.map(a => `
            <div class="alert alert-${a.severity === 'critical' ? 'danger' : 'warning'} d-flex justify-content-between align-items-start py-2 px-3 mb-2">
                <div>
                    ${kindIcon(a.kind)}
                    <strong>${escapeHtml(a.circuit_name)} › ${escapeHtml(a.room_name)}</strong>
                    ${severityBadge(a.severity)}
                    <div class="small mt-1">${escapeHtml(a.message)}</div>
                </div>
                <button class="btn btn-sm btn-outline-secondary btn-room-thermal-link"
                        data-circuit-id="${escapeAttr(a.circuit_id)}"
                        data-room-id="${escapeAttr(a.room_id)}"
                        data-circuit-name="${escapeAttr(a.circuit_name || '')}"
                        data-room-name="${escapeAttr(a.room_name || '')}">
                    <i class="fas fa-thermometer-half"></i> Profile
                </button>
            </div>`).join('');

        const recentHtml = recent.map(r => `
            <div class="small text-muted mb-1">
                ${kindIcon(r.kind)}
                <em>Resolved:</em> ${escapeHtml(r.circuit_name)} › ${escapeHtml(r.room_name)}
                <span class="text-muted">— ${escapeHtml(r.message)}</span>
            </div>`).join('');

        container.innerHTML = `
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span><i class="fas fa-exclamation-triangle me-2"></i>Anomalies
                        <span class="badge bg-${active.length ? 'danger' : 'secondary'} ms-1">${active.length}</span>
                    </span>
                    <small class="text-muted">
                        ${data.last_scan_age_seconds != null
                            ? `scanned ${Math.round(data.last_scan_age_seconds)}s ago`
                            : 'idle'}
                    </small>
                </div>
                <div class="card-body">
                    ${activeHtml || '<div class="small text-muted mb-2">No active anomalies.</div>'}
                    ${recent.length ? `<hr class="my-2"><strong class="small">Recently resolved</strong>${recentHtml}` : ''}
                </div>
            </div>`;

        // Rebind the "Profile" buttons so they open the detail modal
        document.querySelectorAll('#heatingAnomaliesPanel .btn-room-thermal-link')
            .forEach(btn => btn.addEventListener('click', () =>
                openRoomThermalModal(
                    btn.dataset.circuitId, btn.dataset.roomId,
                    btn.dataset.circuitName, btn.dataset.roomName
                )
            ));
    } catch (e) {
        container.innerHTML = '';
    }
}

function renderEpcCard(epc, prop) {
    if (!epc) return emptyCard('EPC Rating', 'No EPC data');
    const colour = EPC_COLOURS[epc.band] || '#888';

    // Collect every room from the configured circuits for the "Room thermals" list
    const cfg = (lastDashboard && lastDashboard._heating_config_circuits) || [];
    const rooms = [];
    for (const c of cfg) {
        for (const r of (c.rooms || [])) {
            rooms.push({
                circuit_id: c.id, circuit_name: c.name,
                room_id: r.id, room_name: r.name,
                has_dimensions: !!(r.dimensions && r.dimensions.floor_area_m2),
            });
        }
    }

    const roomListHtml = rooms.length ? `
        <hr class="my-2">
        <div class="small mb-1"><strong>Per-room thermal profiles</strong></div>
        <div class="small d-flex flex-wrap gap-1">
            ${rooms.map(r => `
                <button class="btn btn-sm btn-outline-${r.has_dimensions ? 'primary' : 'secondary'}
                               py-0 px-2 btn-room-thermal-link"
                        data-circuit-id="${escapeAttr(r.circuit_id)}"
                        data-room-id="${escapeAttr(r.room_id)}"
                        data-room-name="${escapeAttr(r.room_name)}"
                        data-circuit-name="${escapeAttr(r.circuit_name)}"
                        title="${r.has_dimensions ? 'View thermal profile' : 'No dimensions set — click to view anyway'}">
                    <i class="fas fa-thermometer-half me-1"></i>${escapeHtml(r.room_name)}
                </button>`).join('')}
        </div>` : '';

    return `
        <div class="card h-100">
            <div class="card-header"><i class="fas fa-home me-2"></i>Efficiency Rating</div>
            <div class="card-body">
                <div class="d-flex align-items-center">
                    <div style="background:${colour};color:#fff;width:64px;height:64px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:2rem;font-weight:700;flex:0 0 auto;">${escapeHtml(epc.band)}</div>
                    <div class="ms-3">
                        <div class="fs-4 fw-bold">${epc.score}<small class="text-muted fs-6">/100</small></div>
                        <small class="text-muted">${epc.kwh_per_m2_year} kWh/m²/year</small>
                    </div>
                </div>
                <hr class="my-2">
                <div class="small">
                    ${kvRow('Annual demand', `${formatNumber(epc.annual_kwh)} kWh`)}
                    ${kvRow('Annual cost', `£${formatNumber(epc.annual_cost_gbp)}`)}
                    ${kvRow('Glazing', escapeHtml(prop?.glazing || '—'))}
                </div>
                ${roomListHtml}
            </div>
        </div>`;
}

function renderTemperatureCard(outdoor, indoor, heating) {
    const outTemp = outdoor?.temperature;
    const inTemp = indoor?.average;
    const delta = (outTemp != null && inTemp != null) ? (inTemp - outTemp).toFixed(1) : '—';
    const heatingBadge = heating?.active
        ? `<span class="badge bg-danger"><i class="fas fa-fire me-1"></i>Heating</span>`
        : `<span class="badge bg-secondary">Idle</span>`;
    return `
        <div class="card h-100">
            <div class="card-header d-flex justify-content-between">
                <span><i class="fas fa-thermometer-half me-2"></i>Temperatures</span>
                ${heatingBadge}
            </div>
            <div class="card-body">
                <div class="row text-center g-2">
                    <div class="col-6"><div class="text-muted small">Indoor avg</div><div class="fs-3 fw-bold">${formatTemp(inTemp)}</div></div>
                    <div class="col-6"><div class="text-muted small">Outdoor</div><div class="fs-3 fw-bold">${formatTemp(outTemp)}</div></div>
                </div>
                <hr class="my-2">
                <div class="small">
                    ${kvRow('Δ (in − out)', `${delta}°C`)}
                    ${kvRow('Avg demand', `${heating?.avg_demand_percent ?? 0}%`)}
                    ${kvRow('Conditions', escapeHtml(outdoor?.weather?.description || outdoor?.weather?.condition || '—'))}
                </div>
            </div>
        </div>`;
}

function renderCostCard(cost, tariff, epc) {
    const daily = cost?.daily_gbp, monthly = cost?.monthly_gbp;
    return `
        <div class="card h-100">
            <div class="card-header"><i class="fas fa-pound-sign me-2"></i>Running Cost</div>
            <div class="card-body">
                <div class="row text-center g-2">
                    <div class="col-6"><div class="text-muted small">Today</div><div class="fs-3 fw-bold">${daily != null ? '£' + daily.toFixed(2) : '—'}</div></div>
                    <div class="col-6"><div class="text-muted small">This month</div><div class="fs-3 fw-bold">${monthly != null ? '£' + formatNumber(monthly) : '—'}</div></div>
                </div>
                <hr class="my-2">
                <div class="small">
                    ${kvRow('Tariff', escapeHtml(tariff?.type || 'fixed'))}
                    ${kvRow('Unit rate', `${tariff?.unit_rate_p ?? '—'}p/kWh`)}
                    ${kvRow("Today's kWh", cost?.daily_kwh ?? '—')}
                    ${kvRow('Est. annual', `£${epc?.annual_cost_gbp != null ? formatNumber(epc.annual_cost_gbp) : '—'}`)}
                </div>
            </div>
        </div>`;
}

function renderPreheatCard(preheat) {
    const mins = preheat?.minutes_needed;
    return `
        <div class="card h-100">
            <div class="card-header"><i class="fas fa-clock me-2"></i>Pre-heat Recommendation</div>
            <div class="card-body">
                ${preheat ? `
                    <p class="mb-2">To warm from <strong>${preheat.from_temp}°C</strong> to <strong>${preheat.to_temp}°C</strong> (outdoor ${formatTemp(preheat.outdoor_temp)}):</p>
                    <div class="fs-2 fw-bold text-primary">${mins} <small class="fs-6 text-muted">min lead time</small></div>
                ` : `<p class="text-muted mb-2">Insufficient sensor data.</p>`}
                <hr class="my-2">
                <div class="input-group input-group-sm mb-2">
                    <span class="input-group-text">Target °C</span>
                    <input type="number" id="preheatTarget" class="form-control" step="0.5" min="10" max="28" value="21">
                    <button id="btnPreheatCalc" class="btn btn-outline-primary">Calculate</button>
                </div>
                <div id="preheatResult" class="small text-muted"></div>
            </div>
        </div>`;
}

function renderForecastCard(outdoor) {
    const forecast = outdoor?.forecast_3h || [];
    if (!forecast.length) return emptyCard('3-hour Forecast', 'No forecast available');
    const max = Math.max(...forecast, outdoor?.temperature ?? 0);
    const min = Math.min(...forecast, outdoor?.temperature ?? 0);
    const range = Math.max(1, max - min);
    const bars = forecast.map((t, i) => {
        const h = ((t - min) / range) * 80 + 10;
        return `
            <div class="text-center" style="flex:1;">
                <div style="height:100px;display:flex;align-items:flex-end;justify-content:center;">
                    <div style="width:28px;height:${h}px;background:linear-gradient(180deg,#ff7043,#ffa726);border-radius:4px 4px 0 0;" title="${t}°C in +${i + 1}h"></div>
                </div>
                <div class="small fw-bold">${t.toFixed(1)}°</div>
                <div class="text-muted" style="font-size:0.7rem;">+${i + 1}h</div>
            </div>`;
    }).join('');
    return `
        <div class="card h-100">
            <div class="card-header"><i class="fas fa-cloud-sun me-2"></i>Short-term Forecast</div>
            <div class="card-body">
                <div class="d-flex gap-2 align-items-end">${bars}</div>
                <div class="text-center small text-muted mt-2">Range ${min.toFixed(1)}°C — ${max.toFixed(1)}°C</div>
            </div>
        </div>`;
}

// Runtime is fetched per-dashboard-load and cached here so renderDevicesTable()
// can render synchronously. loadHeatingRuntime() populates this.
let runtimeCache = {};

// Cache of { ieee: { running: bool, calling: bool } } from
// /api/heating/controller/state — the authoritative source for receiver truth.
// Populated by loadControllerOverlay() before renderDevicesTable is called.
let controllerOverlay = {};

async function loadControllerOverlay() {
    try {
        const res = await fetch('/api/heating/controller/state');
        const json = await res.json();
        controllerOverlay = {};
        if (json.success && json.state && Array.isArray(json.state.circuits)) {
            for (const c of json.state.circuits) {
                // Receiver entry — running / calling truth
                if (c.receiver_ieee) {
                    controllerOverlay[c.receiver_ieee] = {
                        kind: 'receiver',
                        running: c.receiver_state?.running === true,
                        calling: !!c.calling_for_heat,
                        system_mode: c.receiver_state?.system_mode,
                    };
                }
                // Per-TRV entries — overlay the authoritative room temperature
                // (external sensor if configured, else TRV mean) so the Heating
                // Devices table shows what the controller actually decides on.
                for (const r of (c.rooms || [])) {
                    if (r.current_temp == null) continue;
                    for (const t of (r.trvs || [])) {
                        if (!t.ieee) continue;
                        controllerOverlay[t.ieee] = {
                            kind: 'trv',
                            room_name: r.name,
                            room_temp: r.current_temp,
                            room_temp_source: r.temp_source, // "external" | "trv_mean"
                            room_status: r.status,           // cold | ontarget | hot
                            target_temp: r.target_temp,
                            intended_setpoint: t.intended_setpoint,
                            trv_action: t.action,            // track_target | force_close
                        };
                    }
                }
            }
        }
    } catch (err) {
        console.warn('Controller overlay fetch failed:', err);
        controllerOverlay = {};
    }
}

function renderDevicesTable(devices) {
    if (!devices.length) return `<div class="card-body text-muted text-center py-4">No heating devices detected.</div>`;
    const rows = devices.map(d => {
        // Receivers known to the Heating Controller → trust its truth over
        // the advisor's (possibly-stale) hvac_action. This mirrors exactly
        // what the heatingControllerPanel shows.
        const overlay = controllerOverlay[d.ieee];
        let eff;
        if (overlay) {
            if (overlay.running)       eff = 'heating';
            else if (overlay.calling)  eff = 'calling';
            else if (String(d.mode || '').toLowerCase() === 'off') eff = 'off';
            else                       eff = 'idle';
        } else {
            // Non-receiver (TRV, standalone thermostat): fall back to advisor.
            eff = d.effective_action ??
                (d.running ? 'heating' : (d.action || 'idle'));
        }
        const actionBadge = (() => {
            switch (eff) {
                case 'heating':
                    return `<span class="badge bg-danger"><i class="fas fa-fire me-1"></i>heating</span>`;
                case 'calling':
                    return `<span class="badge bg-warning text-dark" title="Controller is calling for heat but the receiver is not firing yet"><i class="fas fa-hourglass-half me-1"></i>calling</span>`;
                case 'off':
                    return `<span class="badge bg-dark">off</span>`;
                default:
                    return `<span class="badge bg-secondary">idle</span>`;
            }
        })();

        // 24h on-time percentage (replaces instantaneous demand)
        const rt = runtimeCache[d.ieee];
        const pct = rt ? Number(rt.percent || 0) : 0;
        const onMin = rt ? Math.round((rt.on_seconds || 0) / 60) : 0;
        const pctBar = `
            <div class="progress" style="height:6px;width:80px;"><div class="progress-bar ${pct > 50 ? 'bg-danger' : 'bg-warning'}" style="width:${Math.min(100, pct)}%"></div></div>
            <small class="text-muted" title="${onMin} min on in the last 24h${rt?.source ? ` (source: ${rt.source})` : ''}">${pct.toFixed(1)}%</small>`;

        const cachedDev = (window.state?.deviceCache || {})[d.ieee];
        const displayName = cachedDev?.friendly_name || d.name || d.ieee || '—';
        // If the controller is managing this device, overlay room temp / target
        // so this row matches the controller panel exactly.
        let currentCell, setpointCell;
        if (overlay?.kind === 'trv' && overlay.room_temp != null) {
            const srcTag = overlay.room_temp_source === 'external'
                ? `<i class="fas fa-satellite-dish ms-1 text-info" title="From external sensor in ${escapeHtml(overlay.room_name)}"></i>`
                : `<i class="fas fa-thermometer-half ms-1 text-muted" title="TRV mean"></i>`;
            currentCell = `${formatTemp(overlay.room_temp)} ${srcTag}`;
            const intendedTag = (overlay.intended_setpoint != null && Math.abs((overlay.intended_setpoint - (d.setpoint ?? 0))) > 0.05)
                ? ` <small class="text-muted" title="Controller intended setpoint">(→${overlay.intended_setpoint}°)</small>`
                : '';
            setpointCell = `${formatTemp(d.setpoint)}${intendedTag}`;
        } else {
            currentCell = formatTemp(d.temperature);
            setpointCell = formatTemp(d.setpoint);
        }

        return `
            <tr>
                <td>${escapeHtml(displayName)}</td>
                <td>${currentCell}</td>
                <td>${setpointCell}</td>
                <td>${escapeHtml(d.mode || '—')}</td>
                <td>${actionBadge}</td>
                <td>${pctBar}</td>
                <td class="text-end">
                    <button class="btn btn-sm btn-outline-primary heating-manage-btn" data-ieee="${escapeAttr(d.ieee)}" title="Details & Control">
                        <i class="fas fa-sliders-h"></i> Manage
                    </button>
                </td>
            </tr>`;
    }).join('');
    return `
        <div class="table-responsive">
            <table class="table table-sm mb-0 align-middle">
                <thead><tr><th>Device</th><th>Current</th><th>Setpoint</th><th>Mode</th><th>Action</th><th>24h on-time</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

async function loadHeatingRuntime(hours = 24) {
    try {
        const res = await fetch(`/api/heating/runtime?hours=${hours}`);
        const json = await res.json();
        if (json.success) {
            runtimeCache = json.devices || {};
        }
    } catch (err) {
        console.warn('Heating runtime fetch failed:', err);
    }
}

// ─── Zones dashboard (per-zone summary cards) ──────────────────────
function renderZonesDashboard(zones) {
    if (!zones.length) return '';  // hidden entirely if no zones configured

    const cards = zones.map(z => {
        const statusMeta = {
            below:    { colour: '#fd7e14', icon: 'arrow-down', label: 'Below target' },
            ontarget: { colour: '#198754', icon: 'check',      label: 'On target' },
            above:    { colour: '#dc3545', icon: 'arrow-up',   label: 'Above target' },
            unknown:  { colour: '#6c757d', icon: 'question',   label: 'No data' },
        }[z.status] || { colour: '#6c757d', icon: 'question', label: 'Unknown' };

        const current = z.current_temp != null ? `${Number(z.current_temp).toFixed(1)}°C` : '—';
        const delta = (z.current_temp != null)
            ? (z.current_temp - z.target_temp).toFixed(1)
            : null;
        const deltaHtml = delta !== null
            ? `<small class="${delta >= 0 ? 'text-danger' : 'text-warning'}">${delta >= 0 ? '+' : ''}${delta}°C vs target</small>`
            : `<small class="text-muted">no reading</small>`;

        const sourceBadge = {
            schedule: `<span class="badge bg-info text-dark" title="From active schedule slot"><i class="fas fa-calendar-alt me-1"></i>scheduled</span>`,
            setback:  `<span class="badge bg-secondary" title="Night setback"><i class="fas fa-moon me-1"></i>setback</span>`,
            default:  `<span class="badge bg-light text-dark border" title="Zone default target">default</span>`,
        }[z.target_source] || '';

        const demand = Number(z.avg_demand_percent || 0);
        const demandBar = `
            <div class="progress" style="height:6px;">
                <div class="progress-bar ${demand > 50 ? 'bg-danger' : 'bg-warning'}" style="width:${Math.min(100, demand)}%"></div>
            </div>`;

        const heatingBadge = z.heating_active
            ? `<span class="badge bg-danger"><i class="fas fa-fire me-1"></i>heating</span>`
            : '';

        const preheatLine = (z.preheat_minutes != null && z.preheat_minutes > 0)
            ? `<div class="mt-1 small text-muted"><i class="fas fa-clock me-1"></i>Pre-heat: ${z.preheat_minutes} min</div>`
            : '';

        const deviceCount = z.device_count || 0;

        return `
            <div class="col-md-6 col-lg-4">
                <div class="card h-100 heating-zone-dash" style="border-left: 4px solid ${statusMeta.colour};">
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-start mb-2">
                            <div>
                                <h6 class="mb-0"><i class="fas fa-layer-group me-1"></i>${escapeHtml(z.name)}</h6>
                                <small class="text-muted">${deviceCount} device${deviceCount === 1 ? '' : 's'}</small>
                            </div>
                            ${heatingBadge}
                        </div>

                        <div class="d-flex justify-content-between align-items-baseline mb-1">
                            <div class="fs-3 fw-bold">${current}</div>
                            <div class="text-end">
                                <div class="small">Target: <strong>${Number(z.target_temp).toFixed(1)}°C</strong></div>
                                ${sourceBadge}
                            </div>
                        </div>
                        <div class="mb-2">${deltaHtml}</div>

                        <div class="d-flex justify-content-between small text-muted mb-1">
                            <span>Demand</span>
                            <span>${demand}%</span>
                        </div>
                        ${demandBar}

                        <div class="mt-2 small" style="color:${statusMeta.colour};">
                            <i class="fas fa-${statusMeta.icon} me-1"></i>${statusMeta.label}
                        </div>
                        ${preheatLine}
                    </div>
                </div>
            </div>`;
    }).join('');

    return `
        <div class="mb-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <h6 class="mb-0"><i class="fas fa-layer-group me-2"></i>Zones</h6>
                <small class="text-muted">${zones.length} zone${zones.length === 1 ? '' : 's'}, sorted by priority</small>
            </div>
            <div class="row g-3">${cards}</div>
        </div>`;
}

// Cached chart state so refreshes don't flash
let _historyChartSeries = null;
let _historyHoverHandler = null;

function renderHistoryChart(historyData) {
    const container = document.getElementById('heatingHistoryChart');
    if (!container) return;
    const devices = historyData?.devices || {};
    const names = Object.keys(devices);

    const noDataBlock = (title, detail) => `
        <div class="text-center text-muted py-4">
            <i class="fas fa-chart-line fa-2x mb-2 opacity-50"></i>
            <div><strong>${escapeHtml(title)}</strong></div>
            <div class="small mt-1">${escapeHtml(detail)}</div>
        </div>`;

    if (!names.length) {
        container.innerHTML = noDataBlock(
            'No history yet',
            'No heating devices have been detected. Once a thermostat or TRV starts reporting temperature, points will appear here within a few minutes.'
        );
        return;
    }

    // Build series — temperature only (we chart temp over time, not demand)
    const series = [];
    let totalPoints = 0;
    for (const name of names) {
        const temps = devices[name]?.temperature || [];
        totalPoints += temps.length;
        if (temps.length > 1) series.push({ name, points: temps });
    }

    if (!series.length) {
        const deviceList = names.slice(0, 6).map(n => `<li><code>${escapeHtml(n)}</code></li>`).join('');
        const extra = names.length > 6 ? `<li>…and ${names.length - 6} more</li>` : '';
        container.innerHTML = `
            <div class="text-center text-muted py-3">
                <i class="fas fa-chart-line fa-2x mb-2 opacity-50"></i>
                <div><strong>No temperature history recorded yet</strong></div>
                <div class="small mt-2">Found ${names.length} heating device${names.length === 1 ? '' : 's'} but only ${totalPoints} point${totalPoints === 1 ? '' : 's'} of temperature data in the last 24h.</div>
                <div class="small mt-1">Telemetry records a point each time a device reports a changed attribute. Points will accumulate as the day progresses.</div>
                <ul class="small text-muted list-unstyled mt-2 mb-0">${deviceList}${extra}</ul>
            </div>`;
        return;
    }

    _historyChartSeries = series;

    // === Geometry ===
    const W = 900;
    const H = 280;
    const PAD_L = 46;   // left for y-axis
    const PAD_R = 16;
    const PAD_T = 12;
    const PAD_B = 40;   // bottom for x-axis labels
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    // === Scale domains ===
    let tMin = Infinity, tMax = -Infinity, xMin = Infinity, xMax = -Infinity;
    for (const s of series) for (const p of s.points) {
        const [ts, val] = parsePoint(p);
        if (val == null || !isFinite(val) || !isFinite(ts)) continue;
        if (val < tMin) tMin = val;
        if (val > tMax) tMax = val;
        if (ts < xMin) xMin = ts;
        if (ts > xMax) xMax = ts;
    }
    if (!isFinite(tMin) || !isFinite(tMax)) {
        container.innerHTML = noDataBlock('No valid data points', 'Telemetry exists but no samples have numeric values.');
        return;
    }

    // Nice-round Y range at 1°C intervals with a bit of padding
    tMin = Math.floor(tMin - 0.5);
    tMax = Math.ceil(tMax + 0.5);
    if (tMax - tMin < 4) tMax = tMin + 4;   // minimum 4° visible

    // X range — default to "last 24h ending now" so the axis is stable
    const now = Date.now();
    xMax = Math.max(xMax, now);
    xMin = Math.min(xMin, now - 24 * 3600 * 1000);

    const xScale = t => PAD_L + ((t - xMin) / Math.max(1, xMax - xMin)) * plotW;
    const yScale = v => PAD_T + plotH - ((v - tMin) / Math.max(0.1, tMax - tMin)) * plotH;

    // === Y-axis ticks (every 1°C, labels every 2°C if range is large) ===
    const yTicks = [];
    const yLabelStep = (tMax - tMin > 10) ? 2 : 1;
    for (let v = tMin; v <= tMax + 0.001; v += 1) {
        yTicks.push({ v, isLabel: (Math.round(v) % yLabelStep === 0) });
    }

    // === X-axis ticks (every 2 hours) ===
    const xTicks = [];
    const start = new Date(xMin);
    start.setMinutes(0, 0, 0);
    start.setHours(start.getHours() + 1);
    for (let t = start.getTime(); t <= xMax; t += 2 * 3600 * 1000) {
        xTicks.push(t);
    }

    const fmtHour = ms => {
        const d = new Date(ms);
        const h = d.getHours().toString().padStart(2, '0');
        return `${h}:00`;
    };

    // === Palette ===
    const palette = getChartPalette();

    // === Paths ===
    const paths = series.map((s, i) => {
        const colour = palette[i % palette.length];
        const d = s.points.map(p => {
            const [ts, val] = parsePoint(p);
            if (val == null || !isFinite(val) || !isFinite(ts)) return '';
            return `${xScale(ts).toFixed(1)},${yScale(val).toFixed(1)}`;
        }).filter(Boolean).join(' L ');
        return d ? `<path d="M ${d}" fill="none" stroke="${colour}" stroke-width="1.8"
                         stroke-linejoin="round" stroke-linecap="round"
                         data-series-index="${i}" class="history-line"/>` : '';
    }).join('');

    // === Grid ===
    const yGrid = yTicks.map(t => `
        <line x1="${PAD_L}" x2="${W - PAD_R}" y1="${yScale(t.v)}" y2="${yScale(t.v)}"
              stroke="currentColor" stroke-opacity="${t.isLabel ? 0.15 : 0.05}"
              stroke-dasharray="${t.isLabel ? '' : '2,2'}"/>`).join('');

    const xGrid = xTicks.map(t => `
        <line x1="${xScale(t)}" x2="${xScale(t)}" y1="${PAD_T}" y2="${PAD_T + plotH}"
              stroke="currentColor" stroke-opacity="0.05"/>`).join('');

    // === Labels ===
    const yLabels = yTicks.filter(t => t.isLabel).map(t => `
        <text x="${PAD_L - 6}" y="${yScale(t.v) + 4}" text-anchor="end"
              font-size="11" fill="currentColor" opacity="0.7">${t.v}°C</text>`).join('');

    const xLabels = xTicks.map(t => `
        <text x="${xScale(t)}" y="${PAD_T + plotH + 16}" text-anchor="middle"
              font-size="11" fill="currentColor" opacity="0.7">${fmtHour(t)}</text>`).join('');

    // === Axes ===
    const xAxis = `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${PAD_T + plotH}" y2="${PAD_T + plotH}"
                         stroke="currentColor" stroke-opacity="0.3"/>`;
    const yAxis = `<line x1="${PAD_L}" x2="${PAD_L}" y1="${PAD_T}" y2="${PAD_T + plotH}"
                         stroke="currentColor" stroke-opacity="0.3"/>`;

    // === Legend ===
    const legend = series.map((s, i) => `
        <span class="me-3 small d-inline-flex align-items-center">
            <span style="display:inline-block;width:18px;height:3px;background:${palette[i % palette.length]};margin-right:6px;border-radius:2px;"></span>
            ${escapeHtml(s.name)}
        </span>`).join('');

    container.innerHTML = `
        <div class="mb-2">${legend}</div>
        <div class="position-relative" id="heatingHistorySvgWrap">
            <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
                 style="width:100%;height:auto;color:var(--bs-body-color);display:block;"
                 id="heatingHistorySvg">
                ${yGrid}${xGrid}${xAxis}${yAxis}${yLabels}${xLabels}${paths}
                <line id="hover-line" x1="0" x2="0" y1="${PAD_T}" y2="${PAD_T + plotH}"
                      stroke="currentColor" stroke-opacity="0.4" stroke-dasharray="3,3"
                      style="display:none;pointer-events:none;"/>
            </svg>
            <div id="heatingHistoryTooltip"
                 style="position:absolute;display:none;pointer-events:none;
                        padding:6px 10px;border-radius:6px;font-size:12px;
                        line-height:1.4;white-space:nowrap;z-index:10;
                        transform:translate(-50%, -100%);margin-top:-8px;"></div>
        </div>`;

    // === Hover handler (single listener; replaces previous on re-render) ===
    if (_historyHoverHandler) {
        const old = document.getElementById('heatingHistorySvg');
        if (old) old.removeEventListener('mousemove', _historyHoverHandler);
    }

    const svg = document.getElementById('heatingHistorySvg');
    const tooltip = document.getElementById('heatingHistoryTooltip');
    const hoverLine = document.getElementById('hover-line');

    _historyHoverHandler = (evt) => {
        const rect = svg.getBoundingClientRect();
        const svgX = (evt.clientX - rect.left) * (W / rect.width);
        const t = xMin + ((svgX - PAD_L) / plotW) * (xMax - xMin);
        if (svgX < PAD_L || svgX > W - PAD_R) {
            tooltip.style.display = 'none';
            hoverLine.style.display = 'none';
            return;
        }
        hoverLine.setAttribute('x1', svgX);
        hoverLine.setAttribute('x2', svgX);
        hoverLine.style.display = '';

        // Nearest point per series
        const readings = [];
        for (let i = 0; i < _historyChartSeries.length; i++) {
            const s = _historyChartSeries[i];
            let best = null, bestDiff = Infinity;
            for (const p of s.points) {
                const [ts, val] = parsePoint(p);
                if (val == null || !isFinite(val) || !isFinite(ts)) continue;
                const diff = Math.abs(ts - t);
                if (diff < bestDiff) { bestDiff = diff; best = { ts, val }; }
            }
            if (best && bestDiff < 30 * 60 * 1000) {  // within 30min
                readings.push({
                    name: s.name, val: best.val, ts: best.ts,
                    colour: palette[i % palette.length],
                });
            }
        }

        if (readings.length === 0) {
            tooltip.style.display = 'none';
            return;
        }

        const d = new Date(t);
        const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        const dayLabel = (() => {
            const today = new Date();
            today.setHours(0, 0, 0, 0);
            const cmp = new Date(d);
            cmp.setHours(0, 0, 0, 0);
            const diffDays = Math.round((today - cmp) / (24 * 3600 * 1000));
            if (diffDays === 0) return 'today';
            if (diffDays === 1) return 'yesterday';
            return d.toLocaleDateString(undefined, { weekday: 'short' });
        })();

        tooltip.innerHTML = `
            <div style="margin-bottom:4px;font-weight:600;">${hhmm} <span style="opacity:0.7;font-weight:400;">${dayLabel}</span></div>
            ${readings.map(r => `
                <div>
                    <span style="display:inline-block;width:8px;height:8px;background:${r.colour};border-radius:2px;margin-right:6px;"></span>
                    ${escapeHtml(r.name)}: <strong>${r.val.toFixed(1)}°C</strong>
                </div>`).join('')}`;

        tooltip.style.display = '';
        tooltip.style.left = ((svgX / W) * rect.width) + 'px';
        tooltip.style.top = (evt.clientY - rect.top) + 'px';
    };

    svg.addEventListener('mousemove', _historyHoverHandler);
    svg.addEventListener('mouseleave', () => {
        tooltip.style.display = 'none';
        hoverLine.style.display = 'none';
    });
}

function parsePoint(p) {
    if (Array.isArray(p)) return [Number(p[0]), Number(p[1])];
    if (p && typeof p === 'object') {
        // ts may be:
        //   - ISO string from DuckDB (e.g. "2026-04-17T12:34:56")
        //   - Number (epoch seconds or ms)
        //   - Date object
        const rawTs = p.ts ?? p.timestamp ?? p.time;
        let ts;
        if (rawTs instanceof Date) {
            ts = rawTs.getTime();
        } else if (typeof rawTs === 'string') {
            ts = Date.parse(rawTs);   // returns ms epoch or NaN
        } else if (typeof rawTs === 'number') {
            // If the number is suspiciously small (< 10^12), treat as seconds
            ts = rawTs < 1e12 ? rawTs * 1000 : rawTs;
        } else {
            ts = NaN;
        }

        // Prefer numeric_val (our server-side parsed float) over the raw value
        // string. Falls back to value for older/non-numeric attributes.
        let val;
        if (p.numeric_val != null) {
            val = Number(p.numeric_val);
        } else {
            const raw = p.value ?? p.v ?? p.val;
            val = raw == null ? null : Number(raw);
        }
        if (val != null && !isFinite(val)) val = null;

        return [ts, val];
    }
    return [NaN, null];
}

function renderTips(tips) {
    if (!tips.length) return `<div class="card-body text-muted text-center py-3">No tips right now — you're running efficiently.</div>`;
    const order = { high: 0, medium: 1, low: 2 };
    const sorted = [...tips].sort((a, b) => (order[a.priority] ?? 3) - (order[b.priority] ?? 3));
    const items = sorted.map(t => {
        const border = { high: 'border-danger', medium: 'border-warning', low: 'border-info' }[t.priority] || 'border-secondary';
        const badge = { high: 'bg-danger', medium: 'bg-warning text-dark', low: 'bg-info text-dark' }[t.priority] || 'bg-secondary';
        return `
            <div class="list-group-item border-start border-4 ${border}">
                <div class="d-flex w-100 justify-content-between align-items-start">
                    <div>
                        <h6 class="mb-1"><i class="fas fa-${escapeAttr(t.icon || 'lightbulb')} me-2"></i>${escapeHtml(t.title)}</h6>
                        <p class="mb-1 small">${escapeHtml(t.detail)}</p>
                        <small class="text-muted">${escapeHtml(t.category || '')}</small>
                    </div>
                    <span class="badge ${badge}">${escapeHtml(t.priority || '')}</span>
                </div>
            </div>`;
    }).join('');
    return `<div class="list-group list-group-flush">${items}</div>`;
}

// ============================================================================
// DASHBOARD BINDINGS
// ============================================================================
function bindTopBarControls() {
    document.getElementById('btn-heating-refresh')?.addEventListener('click', () => {
        lastDashboard = null;
        loadHeatingDashboard();
    });
    document.getElementById('btn-heating-settings')?.addEventListener('click', openSettingsModal);
    document.getElementById('btn-heating-settings-alt')?.addEventListener('click', openSettingsModal);
}

function bindDashboardControls(data) {
    document.getElementById('btnPreheatCalc')?.addEventListener('click', async () => {
        const input = document.getElementById('preheatTarget');
        const resultDiv = document.getElementById('preheatResult');
        if (!input || !resultDiv) return;
        const target = parseFloat(input.value);
        if (isNaN(target)) { resultDiv.innerHTML = `<span class="text-danger">Invalid</span>`; return; }
        resultDiv.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Calculating…`;
        const ph = await fetchPreheatRecommendation(target);
        if (!ph) { resultDiv.innerHTML = `<span class="text-danger">Request failed</span>`; return; }
        if (ph.error) { resultDiv.innerHTML = `<span class="text-muted">${escapeHtml(ph.error)}</span>`; return; }
        resultDiv.innerHTML = `From <strong>${formatTemp(ph.current_indoor)}</strong> to <strong>${target}°C</strong> (outdoor ${formatTemp(ph.current_outdoor)}): start <strong>${ph.preheat_minutes} min</strong> ahead.`;
    });

    // Manage buttons — same pattern as device list: pass the full cached device object
    document.querySelectorAll('.heating-manage-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const ieee = btn.dataset.ieee;
            const cachedDev = (window.state?.deviceCache || {})[ieee];
            if (cachedDev && typeof window.openDeviceModal === 'function') {
                window.openDeviceModal(cachedDev);
            } else if (typeof window.openDeviceModal === 'function') {
                // Fallback — try with a stub if cache miss (modal may handle gracefully)
                window.openDeviceModal({ ieee, friendly_name: ieee, state: {} });
            } else {
                console.warn('openDeviceModal not available on window');
            }
        });
    });
    document.querySelectorAll('.btn-room-thermal-link').forEach(btn => {
        btn.addEventListener('click', () => {
            openRoomThermalModal(
                btn.dataset.circuitId,
                btn.dataset.roomId,
                btn.dataset.circuitName,
                btn.dataset.roomName,
            );
        });
    });
}

// ============================================================================
// SETTINGS MODAL
// ============================================================================
function ensureSettingsModal() {
    if (document.getElementById('heatingSettingsModal')) return;
    const html = `
        <div class="modal fade" id="heatingSettingsModal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title"><i class="fas fa-cog me-2"></i>Heating Settings</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body" id="heatingSettingsBody">${spinnerBlock('Loading settings…')}</div>
                    <div class="modal-footer">
                        <div id="heatingSettingsStatus" class="me-auto small text-muted"></div>
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary" id="btnHeatingSettingsSave">
                            <i class="fas fa-save me-1"></i> Save
                        </button>
                    </div>
                </div>
            </div>
        </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
    document.getElementById('btnHeatingSettingsSave').addEventListener('click', saveSettings);
}

async function openSettingsModal() {
    const modalEl = document.getElementById('heatingSettingsModal');
    const bodyEl = document.getElementById('heatingSettingsBody');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    bodyEl.innerHTML = spinnerBlock('Loading settings…');
    modal.show();

    try {
        const [cfgRes, thermRes] = await Promise.all([
            fetch('/api/heating/config').then(r => r.json()),
            fetch('/api/heating/thermostats').then(r => r.json()).catch(() => ({ success: false })),
        ]);
        if (!cfgRes.success) throw new Error(cfgRes.error || 'Failed to load config');

        configCache = cfgRes.config;
        schemaCache = cfgRes.schema || {};
        thermostatsCache = thermRes.success ? (thermRes.thermostats || []) : [];
        workingZones = deepClone(configCache.zones || []);

        bodyEl.innerHTML = renderSettingsForm(configCache, schemaCache);
        bindSettingsTabs();
    } catch (err) {
        bodyEl.innerHTML = `<div class="alert alert-danger">Failed to load settings: ${escapeHtml(err.message)}</div>`;
    }
}

function renderSettingsForm(cfg, schema) {
    return `
        <ul class="nav nav-tabs mb-3" role="tablist">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#heat-set-property">Property</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#heat-set-tariff">Tariff & Boiler</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#heat-set-comfort">Comfort</button></li>
        </ul>

        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="heatEnabled" ${cfg.enabled ? 'checked' : ''}>
            <label class="form-check-label" for="heatEnabled"><strong>Enable Heating Advisor</strong></label>
        </div>

        <div class="tab-content">
            <div class="tab-pane fade show active" id="heat-set-property">${renderPropertyForm(cfg.property, schema)}</div>
            <div class="tab-pane fade" id="heat-set-tariff">${renderTariffBoilerForm(cfg.tariff, cfg.boiler, schema)}</div>
            <div class="tab-pane fade" id="heat-set-comfort">${renderComfortForm(cfg.comfort)}</div>
        </div>`;
}

function renderPropertyForm(p, schema) {
    return `
        <div class="row g-3">
            <div class="col-md-6">
                <label class="form-label">Property type</label>
                ${selectEl('propType', p.type, schema.property_types)}
            </div>
            <div class="col-md-6">
                <label class="form-label">Build year</label>
                <input type="number" class="form-control" id="propAge" value="${p.age}" min="1800" max="2100">
            </div>
            <div class="col-md-6">
                <label class="form-label">Insulation</label>
                ${selectEl('propInsulation', p.insulation, schema.insulation)}
            </div>
            <div class="col-md-6">
                <label class="form-label">Glazing</label>
                ${selectEl('propGlazing', p.glazing, schema.glazing)}
            </div>
            <div class="col-md-6">
                <label class="form-label">Floor area (m²)</label>
                <input type="number" class="form-control" id="propArea" value="${p.floor_area_m2}" min="10" max="1000">
            </div>
            <div class="col-md-6">
                <label class="form-label">Floors</label>
                <input type="number" class="form-control" id="propFloors" value="${p.floors}" min="1" max="5">
            </div>
        </div>`;
}

function renderTariffBoilerForm(t, b, schema) {
    return `
        <h6 class="text-muted">Electricity / Gas Tariff</h6>
        <div class="row g-3 mb-3">
            <div class="col-md-6">
                <label class="form-label">Tariff type</label>
                ${selectEl('tariffType', t.type, schema.tariff_types)}
            </div>
            <div class="col-md-3">
                <label class="form-label">Unit rate (p/kWh)</label>
                <input type="number" step="0.01" class="form-control" id="tariffUnit" value="${t.unit_rate_p}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Standing charge (p/day)</label>
                <input type="number" step="0.01" class="form-control" id="tariffStanding" value="${t.standing_charge_p}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Off-peak start</label>
                <input type="time" class="form-control" id="tariffOpStart" value="${t.off_peak_start}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Off-peak end</label>
                <input type="time" class="form-control" id="tariffOpEnd" value="${t.off_peak_end}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Off-peak rate (p/kWh)</label>
                <input type="number" step="0.01" class="form-control" id="tariffOpRate" value="${t.off_peak_rate_p}">
            </div>
        </div>
        <hr>
        <h6 class="text-muted">Boiler / Heat Source</h6>
        <div class="row g-3">
            <div class="col-md-6">
                <label class="form-label">Type</label>
                ${selectEl('boilerType', b.type, schema.boiler_types)}
            </div>
            <div class="col-md-3">
                <label class="form-label">Output (kW)</label>
                <input type="number" step="0.5" class="form-control" id="boilerKw" value="${b.output_kw}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Efficiency (%)</label>
                <input type="number" class="form-control" id="boilerEff" value="${b.efficiency_percent}">
                <small class="text-muted">SEDBUK % (use COP×100 for heat pumps — e.g. 300)</small>
            </div>
        </div>`;
}

function renderComfortForm(c) {
    return `
        <div class="row g-3">
            <div class="col-md-3">
                <label class="form-label">Target temp (°C)</label>
                <input type="number" step="0.5" class="form-control" id="comfortTarget" value="${c.target_temp}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Night setback (°C)</label>
                <input type="number" step="0.5" class="form-control" id="comfortSetback" value="${c.night_setback}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Minimum (°C)</label>
                <input type="number" step="0.5" class="form-control" id="comfortMin" value="${c.min_temp}">
            </div>
            <div class="col-md-3">
                <label class="form-label">Max pre-heat (min)</label>
                <input type="number" class="form-control" id="comfortPreheat" value="${c.preheat_max_minutes}">
            </div>
        </div>
        <small class="text-muted">These act as defaults when a zone doesn't specify its own.</small>`;
}

function renderZonesSection() {
    return `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <strong>Heating Zones</strong>
                <small class="text-muted ms-2">Assign thermostats and TRVs to rooms or floors.</small>
            </div>
            <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-secondary" id="btnZonePresets"><i class="fas fa-magic me-1"></i> Quick setup</button>
                <button class="btn btn-primary" id="btnAddZone"><i class="fas fa-plus me-1"></i> Add zone</button>
            </div>
        </div>
        ${thermostatsCache.length === 0
            ? `<div class="alert alert-info small"><i class="fas fa-info-circle me-1"></i> No heating devices detected yet. You can still define zones — devices will appear once paired.</div>`
            : `<div class="small text-muted mb-2">${thermostatsCache.length} heating device(s) available.</div>`}
        <div id="zonesList"></div>`;
}

function renderZoneCard(zone, index) {
    const assigned = zone.devices || [];
    const unassignedForThis = thermostatsCache.filter(t =>
        !workingZones.some((z, zi) => zi !== index && (z.devices || []).includes(t.ieee))
    );
    const deviceCheckboxes = unassignedForThis.length
        ? unassignedForThis.map(t => {
            const checked = assigned.includes(t.ieee) ? 'checked' : '';
            const temp = t.temperature != null ? ` <small class="text-muted">(${Number(t.temperature).toFixed(1)}°C)</small>` : '';
            return `
                <label class="list-group-item small d-flex align-items-center">
                    <input class="form-check-input me-2 zone-device-cb" type="checkbox" data-zone-idx="${index}" data-ieee="${escapeAttr(t.ieee)}" ${checked}>
                    <div class="flex-grow-1">
                        <div>${escapeHtml(t.name)}${temp}</div>
                        <small class="text-muted">${escapeHtml(t.ieee)}</small>
                    </div>
                </label>`;
        }).join('')
        : `<div class="list-group-item small text-muted">No available devices.</div>`;

    return `
        <div class="card mb-2 heating-zone-card" data-zone-idx="${index}">
            <div class="card-header d-flex justify-content-between align-items-center">
                <div class="d-flex align-items-center gap-2 flex-grow-1">
                    <i class="fas fa-layer-group text-primary"></i>
                    <input type="text" class="form-control form-control-sm zone-name" data-zone-idx="${index}"
                           value="${escapeAttr(zone.name)}" placeholder="Zone name" style="max-width:220px;">
                    <small class="text-muted">id: ${escapeHtml(zone.id)}</small>
                </div>
                <button class="btn btn-sm btn-outline-danger btn-delete-zone" data-zone-idx="${index}" title="Delete zone">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
            <div class="card-body">
                <div class="row g-2 mb-3">
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Target °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm zone-target" data-zone-idx="${index}" value="${zone.target_temp}">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Setback °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm zone-setback" data-zone-idx="${index}" value="${zone.night_setback}">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Min °C</label>
                        <input type="number" step="0.5" class="form-control form-control-sm zone-min" data-zone-idx="${index}" value="${zone.min_temp}">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-1">Priority (1–10)</label>
                        <input type="number" min="1" max="10" class="form-control form-control-sm zone-priority" data-zone-idx="${index}" value="${zone.priority || 5}">
                    </div>
                </div>

                <div class="mb-3">
                    <label class="form-label small mb-1">Devices in this zone <span class="badge bg-secondary">${assigned.length}</span></label>
                    <div class="list-group" style="max-height:220px; overflow-y:auto;">${deviceCheckboxes}</div>
                </div>

                <details>
                    <summary class="small text-muted" style="cursor:pointer;">
                        <i class="fas fa-calendar-alt me-1"></i> Schedule (${(zone.schedule || []).length} slot${(zone.schedule || []).length === 1 ? '' : 's'})
                    </summary>
                    <div class="mt-2">${renderZoneSchedule(zone, index)}</div>
                </details>
            </div>
        </div>`;
}

function renderZoneSchedule(zone, zoneIdx) {
    const slots = zone.schedule || [];
    const slotsHtml = slots.map((slot, si) => {
        const dayBtns = DAYS.map(d => {
            const active = (slot.days || []).includes(d.key);
            return `<button type="button" class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-secondary'} zone-sched-day"
                    data-zone-idx="${zoneIdx}" data-slot-idx="${si}" data-day="${d.key}" style="padding:2px 8px;">${d.label}</button>`;
        }).join(' ');
        return `
            <div class="border rounded p-2 mb-2">
                <div class="d-flex gap-2 align-items-center flex-wrap">
                    <div class="btn-group">${dayBtns}</div>
                    <input type="time" class="form-control form-control-sm zone-sched-start" data-zone-idx="${zoneIdx}" data-slot-idx="${si}" value="${slot.start || '07:00'}" style="width:120px;">
                    <span>→</span>
                    <input type="time" class="form-control form-control-sm zone-sched-end" data-zone-idx="${zoneIdx}" data-slot-idx="${si}" value="${slot.end || '22:00'}" style="width:120px;">
                    <input type="number" step="0.5" class="form-control form-control-sm zone-sched-temp" data-zone-idx="${zoneIdx}" data-slot-idx="${si}" value="${slot.temp ?? 20}" style="width:90px;">
                    <span class="small text-muted">°C</span>
                    <button class="btn btn-sm btn-outline-danger ms-auto btn-delete-slot" data-zone-idx="${zoneIdx}" data-slot-idx="${si}"><i class="fas fa-times"></i></button>
                </div>
            </div>`;
    }).join('');
    return `
        ${slotsHtml || '<div class="text-muted small mb-2">No schedule slots. Uses target temp by default.</div>'}
        <button class="btn btn-sm btn-outline-primary btn-add-slot" data-zone-idx="${zoneIdx}"><i class="fas fa-plus"></i> Add time slot</button>`;
}

// ─── Zone presets ──────────────────────────────────────────────────
const ZONE_PRESETS = {
    single: [{ name: 'Whole Home' }],
    two: [{ name: 'Upstairs' }, { name: 'Downstairs' }],
    three: [{ name: 'Living Areas' }, { name: 'Bedrooms' }, { name: 'Bathroom' }],
    four: [{ name: 'Living Room' }, { name: 'Kitchen' }, { name: 'Master Bedroom' }, { name: 'Other Bedrooms' }],
};

function showQuickSetupPicker() {
    const html = `
        <div class="modal fade" id="zonePresetModal" tabindex="-1">
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">Quick Zone Setup</h5>
                        <button class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <p class="small text-muted">Pick a starting layout. You can customise and assign devices afterwards. <strong>This replaces your existing zones.</strong></p>
                        <div class="list-group">
                            <button class="list-group-item list-group-item-action" data-preset="single"><strong>Single Zone</strong><br><small class="text-muted">Whole-home control (1 zone)</small></button>
                            <button class="list-group-item list-group-item-action" data-preset="two"><strong>Upstairs / Downstairs</strong><br><small class="text-muted">Classic 2-zone layout</small></button>
                            <button class="list-group-item list-group-item-action" data-preset="three"><strong>Living / Bedrooms / Bathroom</strong><br><small class="text-muted">Typical 3-zone layout</small></button>
                            <button class="list-group-item list-group-item-action" data-preset="four"><strong>Room-by-Room (4 zones)</strong><br><small class="text-muted">Living, Kitchen, Master, Other Bedrooms</small></button>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    document.querySelector('#zonePresetModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', html);
    const modalEl = document.getElementById('zonePresetModal');
    const modal = new bootstrap.Modal(modalEl);
    modal.show();
    modalEl.addEventListener('hidden.bs.modal', () => modalEl.remove());
    modalEl.querySelectorAll('[data-preset]').forEach(btn => {
        btn.addEventListener('click', () => {
            const preset = ZONE_PRESETS[btn.dataset.preset];
            if (preset) {
                workingZones = preset.map(z => ({
                    id: z.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
                    name: z.name,
                    target_temp: 21, night_setback: 17, min_temp: 16, priority: 5,
                    devices: [], schedule: [],
                }));
                renderZonesList();
            }
            modal.hide();
        });
    });
}

// ─── Zone list rendering + bindings ────────────────────────────────
function renderZonesList() {
    const container = document.getElementById('zonesList');
    if (!container) return;
    if (!workingZones.length) {
        container.innerHTML = `<div class="text-center text-muted py-4 border rounded">
            No zones defined. Click <strong>Add zone</strong> or <strong>Quick setup</strong> to start.
        </div>`;
    } else {
        container.innerHTML = workingZones.map((z, i) => renderZoneCard(z, i)).join('');
    }
    const badge = document.getElementById('zoneCountBadge');
    if (badge) badge.textContent = workingZones.length;
    bindZoneControls();
}

function bindZoneControls() {
    document.querySelectorAll('.zone-name').forEach(el => {
        el.addEventListener('input', e => {
            workingZones[+e.target.dataset.zoneIdx].name = e.target.value;
        });
    });
    const numericBindings = [
        ['.zone-target', 'target_temp', parseFloat],
        ['.zone-setback', 'night_setback', parseFloat],
        ['.zone-min', 'min_temp', parseFloat],
        ['.zone-priority', 'priority', v => parseInt(v, 10)],
    ];
    for (const [sel, key, coerce] of numericBindings) {
        document.querySelectorAll(sel).forEach(el => {
            el.addEventListener('change', e => {
                workingZones[+e.target.dataset.zoneIdx][key] = coerce(e.target.value);
            });
        });
    }
    document.querySelectorAll('.zone-device-cb').forEach(cb => {
        cb.addEventListener('change', e => {
            const idx = +e.target.dataset.zoneIdx;
            const ieee = e.target.dataset.ieee;
            const zone = workingZones[idx];
            zone.devices = zone.devices || [];
            if (e.target.checked) {
                if (!zone.devices.includes(ieee)) zone.devices.push(ieee);
            } else {
                zone.devices = zone.devices.filter(d => d !== ieee);
            }
            renderZonesList();
        });
    });
    document.querySelectorAll('.btn-delete-zone').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = +btn.dataset.zoneIdx;
            if (confirm(`Delete zone "${workingZones[idx]?.name}"?`)) {
                workingZones.splice(idx, 1);
                renderZonesList();
            }
        });
    });
    document.querySelectorAll('.zone-sched-day').forEach(btn => {
        btn.addEventListener('click', () => {
            const zi = +btn.dataset.zoneIdx, si = +btn.dataset.slotIdx, day = btn.dataset.day;
            const slot = workingZones[zi].schedule[si];
            slot.days = slot.days || [];
            const i = slot.days.indexOf(day);
            if (i >= 0) slot.days.splice(i, 1); else slot.days.push(day);
            renderZonesList();
        });
    });
    const slotBindings = [
        ['.zone-sched-start', 'start', v => v],
        ['.zone-sched-end', 'end', v => v],
        ['.zone-sched-temp', 'temp', v => parseFloat(v)],
    ];
    for (const [sel, key, coerce] of slotBindings) {
        document.querySelectorAll(sel).forEach(el => {
            el.addEventListener('change', e => {
                const zi = +e.target.dataset.zoneIdx, si = +e.target.dataset.slotIdx;
                workingZones[zi].schedule[si][key] = coerce(e.target.value);
            });
        });
    }
    document.querySelectorAll('.btn-delete-slot').forEach(btn => {
        btn.addEventListener('click', () => {
            const zi = +btn.dataset.zoneIdx, si = +btn.dataset.slotIdx;
            workingZones[zi].schedule.splice(si, 1);
            renderZonesList();
        });
    });
    document.querySelectorAll('.btn-add-slot').forEach(btn => {
        btn.addEventListener('click', () => {
            const zi = +btn.dataset.zoneIdx;
            workingZones[zi].schedule = workingZones[zi].schedule || [];
            workingZones[zi].schedule.push({
                days: ['mon', 'tue', 'wed', 'thu', 'fri'],
                start: '07:00', end: '22:00', temp: 21,
            });
            renderZonesList();
        });
    });
}

function bindSettingsTabs() {
    renderZonesList();

    document.getElementById('btnAddZone')?.addEventListener('click', () => {
        const n = workingZones.length + 1;
        workingZones.push({
            id: `zone_${n}`, name: `Zone ${n}`,
            target_temp: 21, night_setback: 17, min_temp: 16, priority: 5,
            devices: [], schedule: [],
        });
        renderZonesList();
    });
    document.getElementById('btnZonePresets')?.addEventListener('click', showQuickSetupPicker);
}

// ─── Gather + save ─────────────────────────────────────────────────
function gatherFormValues() {
    const val = (id, coerce = (v) => v) => {
        const el = document.getElementById(id);
        return el ? coerce(el.value) : undefined;
    };
    return {
        enabled: document.getElementById('heatEnabled')?.checked ?? false,
        property: {
            type: val('propType'),
            age: val('propAge', v => parseInt(v, 10)),
            insulation: val('propInsulation'),
            glazing: val('propGlazing'),
            floor_area_m2: val('propArea', v => parseInt(v, 10)),
            floors: val('propFloors', v => parseInt(v, 10)),
        },
        tariff: {
            type: val('tariffType'),
            unit_rate_p: val('tariffUnit', parseFloat),
            standing_charge_p: val('tariffStanding', parseFloat),
            off_peak_start: val('tariffOpStart'),
            off_peak_end: val('tariffOpEnd'),
            off_peak_rate_p: val('tariffOpRate', parseFloat),
        },
        boiler: {
            type: val('boilerType'),
            output_kw: val('boilerKw', parseFloat),
            efficiency_percent: val('boilerEff', v => parseInt(v, 10)),
        },
        comfort: {
            target_temp: val('comfortTarget', parseFloat),
            night_setback: val('comfortSetback', parseFloat),
            min_temp: val('comfortMin', parseFloat),
            preheat_max_minutes: val('comfortPreheat', v => parseInt(v, 10)),
        },
        zones: workingZones,
    };
}

async function saveSettings() {
    const saveBtn = document.getElementById('btnHeatingSettingsSave');
    const statusEl = document.getElementById('heatingSettingsStatus');
    const payload = gatherFormValues();

    for (const z of payload.zones) {
        if (!z.name || !z.name.trim()) {
            statusEl.innerHTML = `<span class="text-danger">All zones need a name.</span>`;
            return;
        }
    }

    saveBtn.disabled = true;
    saveBtn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Saving…`;
    statusEl.innerHTML = '';

    try {
        const res = await fetch('/api/heating/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: payload }),
        });
        const json = await res.json();
        if (!json.success) throw new Error(json.error || 'Save failed');

        statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved. ${escapeHtml(json.message || '')}</span>`;
        if (typeof window.showToast === 'function') {
            window.showToast('Heating settings saved — restart to apply', 'success');
        }

        setTimeout(() => {
            const modalEl = document.getElementById('heatingSettingsModal');
            bootstrap.Modal.getInstance(modalEl)?.hide();
            lastDashboard = null;
            loadHeatingDashboard();
        }, 900);
    } catch (err) {
        statusEl.innerHTML = `<span class="text-danger">${escapeHtml(err.message)}</span>`;
    } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = `<i class="fas fa-save me-1"></i> Save`;
    }
}

// ============================================================================
// ROOM THERMAL / SIZING / PREHEAT DETAIL MODAL
// ============================================================================
function ensureRoomThermalModal() {
    if (document.getElementById('roomThermalModal')) return;
    const html = `
        <div class="modal fade" id="roomThermalModal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title"><i class="fas fa-thermometer-half me-2"></i>
                            <span id="roomThermalTitle">Room thermal profile</span>
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body" id="roomThermalBody">
                        <div class="text-center text-muted py-4">
                            <div class="spinner-border spinner-border-sm me-2"></div>Loading…
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
}

async function openRoomThermalModal(circuitId, roomId, circuitName, roomName) {
    ensureRoomThermalModal();
    const modalEl = document.getElementById('roomThermalModal');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    document.getElementById('roomThermalTitle').textContent =
        `${circuitName} › ${roomName}`;
    const body = document.getElementById('roomThermalBody');
    body.innerHTML = `<div class="text-center text-muted py-4">
        <div class="spinner-border spinner-border-sm me-2"></div>Loading…</div>`;
    modal.show();

    try {
        const cid = encodeURIComponent(circuitId);
        const rid = encodeURIComponent(roomId);
        const [thermal, sizing, preheat] = await Promise.all([
            fetch(`/api/heating/circuits/${cid}/rooms/${rid}/thermal`).then(r => r.json()),
            fetch(`/api/heating/circuits/${cid}/rooms/${rid}/sizing`).then(r => r.json()),
            fetch(`/api/heating/circuits/${cid}/rooms/${rid}/preheat`).then(r => r.json()),
        ]);

        let html = '';
        if (thermal.success) {
            html += `<h6 class="mt-2"><i class="fas fa-calculator me-1"></i>Thermal profile</h6>
                     <div class="mb-3">${_renderThermalCard(thermal.thermal, thermal.meta)}</div>`;
        }
        if (sizing.success) {
            html += `<h6 class="mt-3"><i class="fas fa-ruler-horizontal me-1"></i>Radiator sizing</h6>
                     <div class="mb-3">${_renderSizingCard(sizing.sizing, sizing.meta)}</div>`;
        }
        if (preheat.success) {
            html += `<h6 class="mt-3"><i class="fas fa-hourglass-half me-1"></i>Pre-heat time</h6>
                     <div class="mb-3">${_renderPreheatCard(preheat.preheat, preheat.meta)}</div>`;
        }
        if (!html) {
            html = `<div class="alert alert-warning">Could not load thermal data for this room.</div>`;
        }
        body.innerHTML = html;
    } catch (e) {
        body.innerHTML = `<div class="alert alert-danger">Load failed: ${escapeHtml(e.message)}</div>`;
    }
}

// Thin wrappers that delegate to the controller modal's renderers. We can't
// import them, so duplicate the minimal renderer inline here. To avoid drift,
// keep these simple — if you want richer displays, extract shared functions
// into a separate module later.
function _renderThermalCard(t, meta) {
    if (!t) return '<div class="text-muted">No data</div>';
    const fmt = v => v == null ? '—' : `${Number(v).toFixed(1)} W/K`;
    return `<div class="card card-body bg-light p-2 small">
        <div><strong>Blended heat loss:</strong>
             <span class="fs-6 text-primary">${fmt(t.blended_w_per_k)}</span></div>
        <div>Static: ${fmt(t.static_w_per_k)} · Measured: ${fmt(t.measured_w_per_k)}</div>
        <div>Samples: ${t.measured_sample_count || 0} · τ: ${t.tau_seconds ? Math.round(t.tau_seconds/60)+' min' : '—'}</div>
        <div class="text-muted small mt-1">Insulation: ${escapeHtml(meta?.insulation || '—')}</div>
    </div>`;
}

function _renderSizingCard(s, meta) {
    if (!s) return '';
    const w = v => v == null ? '—' : `${Math.round(v)} W`;
    const btu = v => v == null ? '—' : `${Math.round(v)} BTU/hr`;
    const statusBadge = s.status === 'adequate'
        ? '<span class="badge bg-success ms-1">Adequate</span>'
        : s.status === 'undersized'
        ? `<span class="badge bg-danger ms-1">Undersized by ${Math.round(s.deficit_watts)}W</span>`
        : s.status === 'oversized'
        ? `<span class="badge bg-warning text-dark ms-1">Oversized by ${Math.round(s.surplus_watts)}W</span>`
        : '';
    return `<div class="card card-body bg-light p-2 small">
        <div><strong>Required:</strong> ${w(s.required_watts_with_margin)} (${btu(s.required_btu_hr)}) ${statusBadge}</div>
        <div>ΔT: ${s.delta_t}°C · Target ${s.target_temp_c}°C, design outdoor ${s.design_outdoor_c}°C</div>
        ${s.installed_watts_at_dt50 != null ?
            `<div>Installed: ${w(s.installed_watts_at_dt50)} (ΔT50)${s.installed_watts_at_flow_temp != null ?
                ` → ${w(s.installed_watts_at_flow_temp)} at flow ${s.flow_temperature_c}°C` : ''}</div>` :
            `<div class="text-muted">No installed capacity recorded</div>`}
    </div>`;
}

function _renderPreheatCard(p, meta) {
    if (!p) return '';
    if (!p.reachable) {
        return `<div class="alert alert-warning small mb-0">
            <i class="fas fa-exclamation-triangle me-1"></i>
            Cannot reach target at current flow temp (${p.steady_state_temp_c}°C ceiling).
        </div>`;
    }
    if (!p.minutes_needed) {
        return `<div class="alert alert-success small mb-0">
            <i class="fas fa-check me-1"></i>Already at target.</div>`;
    }
    const mins = Math.round(p.minutes_needed);
    const hrs = Math.floor(mins / 60);
    const timeStr = hrs ? `${hrs}h ${mins % 60}m` : `${mins}m`;
    return `<div class="card card-body bg-light p-2 small">
        <div><strong>Pre-heat needed:</strong> <span class="fs-6 text-info">${timeStr}</span>
             <span class="badge bg-${p.confidence === 'high' ? 'success' : p.confidence === 'medium' ? 'warning text-dark' : 'secondary'} ms-1">${p.confidence} confidence</span></div>
        <div>${p.from_temp_c?.toFixed(1)}°C → ${p.to_temp_c?.toFixed(1)}°C at outdoor ${p.outdoor_temp_c?.toFixed(1)}°C</div>
    </div>`;
}

// ============================================================================
// HELPERS
// ============================================================================
function selectEl(id, value, options) {
    const opts = (options || []).map(o =>
        `<option value="${escapeAttr(o)}" ${o === value ? 'selected' : ''}>${escapeHtml(o)}</option>`
    ).join('');
    return `<select class="form-select" id="${id}">${opts}</select>`;
}
function kvRow(k, v) {
    return `<div class="d-flex justify-content-between"><span class="text-muted">${k}</span><span>${v}</span></div>`;
}
function spinnerBlock(msg) {
    return `<div class="text-center text-muted py-5"><div class="spinner-border spinner-border-sm me-2"></div>${escapeHtml(msg)}</div>`;
}
function emptyCard(title, message) {
    return `<div class="card h-100"><div class="card-header">${escapeHtml(title)}</div><div class="card-body text-muted text-center py-4">${escapeHtml(message)}</div></div>`;
}
function deepClone(o) { return JSON.parse(JSON.stringify(o)); }
function formatTemp(t) { return (t == null || isNaN(t)) ? '—' : `${Number(t).toFixed(1)}°C`; }
function formatNumber(n) { return (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString(); }
function formatTs(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : ''; }
function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function escapeAttr(s) { return escapeHtml(s); }