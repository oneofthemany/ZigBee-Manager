/**
 * notifications.js
 * --------------------------------------------------------------------------
 * Settings → Notifications sub-tab.
 *
 * Lets the user create rules that fire browser / in-app notifications when
 * device events occur. Rules are stored in localStorage so they survive
 * across reloads. The actual delivery uses window.zbmSendNotification (set
 * up in pwa.js) so we get the same service-worker / native / in-app
 * fallback behaviour for free.
 *
 * Architecture mirrors the rest of the SPA:
 *   - initNotifications()  is called once at boot (from main.js)
 *   - on Settings sub-tab "shown.bs.tab" we render the rules list
 *   - a 5-second poll over window.state.deviceCache evaluates rules
 *
 * The rule engine is independent from pwa.js' four hard-coded toggles —
 * those continue to work via the navbar bell. This module adds *per-device*
 * + *per-event* rules with cooldowns, time windows, and condition logic.
 * --------------------------------------------------------------------------
 */

import { state } from './state.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const RULES_KEY = 'zbm-notification-rules';
const POLL_INTERVAL_MS = 5000;

/**
 * Trigger catalogue. Each entry describes one kind of event we can detect
 * on a device. The `match(prev, curr, rule)` function returns true when the
 * trigger has just fired (i.e. the transition happened between prev → curr).
 *
 * Keep this list in sync with the icons used in the UI builder below.
 */
const TRIGGERS = {
    motion_detected: {
        label: 'Motion detected',
        icon: 'fa-running',
        category: 'motion',
        match: (prev, curr) => {
            const was = !!(prev.occupancy || prev.motion || prev.presence);
            const now = !!(curr.occupancy || curr.motion || curr.presence);
            return !was && now;
        },
        defaultBody: (name) => `Motion detected — ${name}`,
    },
    motion_cleared: {
        label: 'Motion cleared',
        icon: 'fa-shield-alt',
        category: 'motion',
        match: (prev, curr) => {
            const was = !!(prev.occupancy || prev.motion || prev.presence);
            const now = !!(curr.occupancy || curr.motion || curr.presence);
            return was && !now;
        },
        defaultBody: (name) => `Motion cleared — ${name}`,
    },
    contact_opened: {
        label: 'Door / window opened',
        icon: 'fa-door-open',
        category: 'contact',
        match: (prev, curr) => {
            // contact === false means open in the ZCL convention used here
            const wasOpen = prev.contact === false || prev.is_open === true;
            const nowOpen = curr.contact === false || curr.is_open === true;
            return !wasOpen && nowOpen;
        },
        defaultBody: (name) => `${name} opened`,
    },
    contact_closed: {
        label: 'Door / window closed',
        icon: 'fa-door-closed',
        category: 'contact',
        match: (prev, curr) => {
            const wasOpen = prev.contact === false || prev.is_open === true;
            const nowOpen = curr.contact === false || curr.is_open === true;
            return wasOpen && !nowOpen;
        },
        defaultBody: (name) => `${name} closed`,
    },
    water_leak: {
        label: 'Water leak detected',
        icon: 'fa-tint',
        category: 'safety',
        match: (prev, curr) => !prev.water_leak && !!curr.water_leak,
        defaultBody: (name) => `🚨 Water leak — ${name}`,
        persistent: true,
    },
    smoke: {
        label: 'Smoke detected',
        icon: 'fa-fire',
        category: 'safety',
        match: (prev, curr) => !prev.smoke && !!curr.smoke,
        defaultBody: (name) => `🚨 Smoke detected — ${name}`,
        persistent: true,
    },
    vibration: {
        label: 'Vibration / tamper',
        icon: 'fa-bolt',
        category: 'safety',
        match: (prev, curr) => !prev.vibration && !!curr.vibration,
        defaultBody: (name) => `Vibration — ${name}`,
    },
    button_pressed: {
        label: 'Button pressed',
        icon: 'fa-hand-pointer',
        category: 'control',
        match: (prev, curr) => {
            // Edge-trigger any change in `action` that is not empty
            if (!curr.action || curr.action === '') return false;
            return prev.action !== curr.action;
        },
        defaultBody: (name, curr) => `${name}: ${curr.action}`,
    },
    low_battery: {
        label: 'Low battery (< 15%)',
        icon: 'fa-battery-quarter',
        category: 'maintenance',
        match: (prev, curr) => {
            const b = curr.battery ?? curr.battery_percentage;
            const pb = prev.battery ?? prev.battery_percentage;
            if (b === undefined) return false;
            // Edge trigger when crossing the threshold downward
            return (pb === undefined || pb > 15) && b <= 15;
        },
        defaultBody: (name, curr) => {
            const b = curr.battery ?? curr.battery_percentage;
            return `${name} battery at ${b}%`;
        },
        persistent: true,
    },
    offline: {
        label: 'Device went offline',
        icon: 'fa-plug',
        category: 'maintenance',
        // Note: `available` is merged in from device.available (not state)
        match: (prev, curr) => prev.available === true && curr.available === false,
        defaultBody: (name) => `${name} is offline`,
    },
    online: {
        label: 'Device came online',
        icon: 'fa-plug-circle-bolt',
        category: 'maintenance',
        match: (prev, curr) => prev.available === false && curr.available === true,
        defaultBody: (name) => `${name} is online`,
    },
    temp_target_reached: {
        label: 'Heating target reached',
        icon: 'fa-thermometer-half',
        category: 'heating',
        match: (prev, curr) => {
            const target = curr.occupied_heating_setpoint ?? curr.heating_setpoint;
            const cnow = curr.internal_temperature ?? curr.temperature ?? curr.local_temperature;
            const cprev = prev.internal_temperature ?? prev.temperature ?? prev.local_temperature;
            if (!target || cnow === undefined || cprev === undefined) return false;
            return cprev < (target - 0.3) && cnow >= (target - 0.3);
        },
        defaultBody: (name, curr) => {
            const target = curr.occupied_heating_setpoint ?? curr.heating_setpoint;
            const cnow = curr.internal_temperature ?? curr.temperature ?? curr.local_temperature;
            return `${name} reached ${Number(cnow).toFixed(1)}°C (target ${Number(target).toFixed(1)}°C)`;
        },
    },
    temp_above: {
        label: 'Temperature rises above threshold',
        icon: 'fa-temperature-high',
        category: 'heating',
        needsThreshold: true,
        match: (prev, curr, rule) => {
            const t = curr.temperature ?? curr.local_temperature ?? curr.internal_temperature;
            const pt = prev.temperature ?? prev.local_temperature ?? prev.internal_temperature;
            if (t === undefined || pt === undefined) return false;
            const thr = Number(rule.threshold);
            return pt <= thr && t > thr;
        },
        defaultBody: (name, curr, rule) => `${name} now ${Number(curr.temperature ?? curr.local_temperature).toFixed(1)}°C (above ${rule.threshold}°C)`,
    },
    temp_below: {
        label: 'Temperature drops below threshold',
        icon: 'fa-temperature-low',
        category: 'heating',
        needsThreshold: true,
        match: (prev, curr, rule) => {
            const t = curr.temperature ?? curr.local_temperature ?? curr.internal_temperature;
            const pt = prev.temperature ?? prev.local_temperature ?? prev.internal_temperature;
            if (t === undefined || pt === undefined) return false;
            const thr = Number(rule.threshold);
            return pt >= thr && t < thr;
        },
        defaultBody: (name, curr, rule) => `${name} now ${Number(curr.temperature ?? curr.local_temperature).toFixed(1)}°C (below ${rule.threshold}°C)`,
    },
    valve_alarm: {
        label: 'Valve alarm (TRV)',
        icon: 'fa-exclamation-triangle',
        category: 'heating',
        match: (prev, curr) => !prev.valve_alarm && !!curr.valve_alarm,
        defaultBody: (name) => `Valve alarm — ${name}`,
        persistent: true,
    },
    window_open_trv: {
        label: 'Window-open detected (TRV)',
        icon: 'fa-window-maximize',
        category: 'heating',
        match: (prev, curr) => !prev.window_open && !!curr.window_open,
        defaultBody: (name) => `Window-open detected — ${name}`,
    },
};

// Sensible defaults for rule cooldowns
const COOLDOWN_OPTIONS = [
    { value: 0,  label: 'No cooldown' },
    { value: 1,  label: '1 minute' },
    { value: 5,  label: '5 minutes' },
    { value: 15, label: '15 minutes' },
    { value: 60, label: '1 hour' },
];

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function loadRules() {
    try {
        const raw = localStorage.getItem(RULES_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
        console.warn('[notifications] Failed to load rules', e);
        return [];
    }
}

function saveRules(rules) {
    localStorage.setItem(RULES_KEY, JSON.stringify(rules));
}

function newRuleId() {
    return 'rule-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
}

// ---------------------------------------------------------------------------
// Rule engine
// ---------------------------------------------------------------------------

const previousStates = {};   // ieee → last seen merged state
const lastFiredAt   = {};    // ruleId+ieee → epoch ms

function withinTimeWindow(rule) {
    if (!rule.timeFrom || !rule.timeTo) return true;
    const now = new Date();
    const mins = now.getHours() * 60 + now.getMinutes();
    const [fH, fM] = rule.timeFrom.split(':').map(Number);
    const [tH, tM] = rule.timeTo.split(':').map(Number);
    const from = fH * 60 + fM;
    const to = tH * 60 + tM;
    if (from === to) return true;
    if (from < to)  return mins >= from && mins <= to;
    // wraps midnight (e.g. 22:00 → 06:00)
    return mins >= from || mins <= to;
}

function deviceMatchesRule(rule, ieee, device) {
    if (rule.scope === 'all') return true;
    if (rule.scope === 'devices') {
        return Array.isArray(rule.devices) && rule.devices.includes(ieee);
    }
    if (rule.scope === 'tab') {
        // Optional: filter by user-defined tab. The deviceTabs map lives in
        // tabs.js — accessed lazily via window.state if exposed.
        const tabs = (window.state && window.state.deviceTabs) || {};
        const list = tabs[rule.tab] || [];
        return list.includes(ieee);
    }
    return false;
}

function ruleCooldownPassed(rule, ieee) {
    const key = rule.id + '|' + ieee;
    const cd = (Number(rule.cooldownMinutes) || 0) * 60_000;
    if (!cd) return true;
    const last = lastFiredAt[key] || 0;
    return (Date.now() - last) >= cd;
}

function markFired(rule, ieee) {
    lastFiredAt[rule.id + '|' + ieee] = Date.now();
}

/**
 * Evaluate every enabled rule against every cached device. Called every
 * POLL_INTERVAL_MS from the main poller. Edge-triggered: a rule only fires
 * on transition (prev → curr), never on a steady state.
 */
function evaluateRules() {
    const cache = window.state && window.state.deviceCache;
    if (!cache) return;

    const rules = loadRules().filter(r => r.enabled !== false);
    if (rules.length === 0) {
        // Still need to update previousStates so we have a baseline if the
        // user enables a rule later.
        Object.keys(cache).forEach(ieee => {
            const d = cache[ieee];
            if (d && d.state) {
                previousStates[ieee] = { ...d.state, available: d.available };
            }
        });
        return;
    }

    Object.keys(cache).forEach(ieee => {
        const device = cache[ieee];
        if (!device || !device.state) return;
        const curr = { ...device.state, available: device.available };
        const prev = previousStates[ieee] || {};
        const name = device.friendly_name || ieee.slice(-8);

        rules.forEach(rule => {
            const trigger = TRIGGERS[rule.trigger];
            if (!trigger) return;
            if (!deviceMatchesRule(rule, ieee, device)) return;
            if (!withinTimeWindow(rule)) return;
            if (!ruleCooldownPassed(rule, ieee)) return;

            let matched = false;
            try {
                matched = !!trigger.match(prev, curr, rule);
            } catch (e) {
                console.warn('[notifications] match error', rule, e);
            }
            if (!matched) return;

            const title = rule.title || trigger.label;
            const body  = rule.message
                ? rule.message.replace(/\{device\}/g, name)
                : trigger.defaultBody(name, curr, rule);

            const send = window.zbmSendNotification;
            if (typeof send === 'function') {
                send(title, body, `${rule.id}-${ieee}`, {
                    persistent: !!trigger.persistent,
                });
            } else if (window.toast) {
                // Hard fallback if pwa.js isn't loaded
                window.toast.info(`${title}: ${body}`);
            }

            markFired(rule, ieee);
        });

        previousStates[ieee] = curr;
    });
}

// ---------------------------------------------------------------------------
// UI rendering
// ---------------------------------------------------------------------------

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function getAllDevices() {
    const cache = (window.state && window.state.deviceCache) || {};
    return Object.values(cache)
        .filter(d => d && d.ieee)
        .sort((a, b) => (a.friendly_name || '').localeCompare(b.friendly_name || ''));
}

function renderRulesList() {
    const container = document.getElementById('notifRulesList');
    if (!container) return;

    const rules = loadRules();
    if (rules.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted py-5">
                <i class="fas fa-bell-slash fa-2x mb-2 d-block"></i>
                <div>No notification rules yet.</div>
                <div class="small">Click <strong>Add Rule</strong> to get notified about motion, doors, leaks, heating events and more.</div>
            </div>
        `;
        return;
    }

    container.innerHTML = rules.map(rule => {
        const trigger = TRIGGERS[rule.trigger];
        const triggerLabel = trigger ? trigger.label : rule.trigger;
        const icon = trigger ? trigger.icon : 'fa-bell';
        const enabled = rule.enabled !== false;

        let scopeText = 'All devices';
        if (rule.scope === 'devices') {
            const n = (rule.devices || []).length;
            scopeText = `${n} device${n === 1 ? '' : 's'}`;
        } else if (rule.scope === 'tab') {
            scopeText = `Tab: ${rule.tab}`;
        }

        let extras = '';
        if (rule.threshold !== undefined && rule.threshold !== '') {
            extras += `<span class="badge bg-light text-dark border me-1">Threshold: ${escapeHtml(rule.threshold)}</span>`;
        }
        if (rule.timeFrom && rule.timeTo) {
            extras += `<span class="badge bg-light text-dark border me-1"><i class="far fa-clock me-1"></i>${escapeHtml(rule.timeFrom)}–${escapeHtml(rule.timeTo)}</span>`;
        }
        if (rule.cooldownMinutes) {
            extras += `<span class="badge bg-light text-dark border me-1">Cooldown ${rule.cooldownMinutes}m</span>`;
        }

        return `
            <div class="card notif-rule-card mb-2 ${enabled ? '' : 'opacity-50'}" data-rule-id="${rule.id}">
                <div class="card-body py-2 px-3">
                    <div class="d-flex align-items-center gap-2">
                        <i class="fas ${icon} fa-fw text-primary"></i>
                        <div class="flex-grow-1 min-w-0">
                            <div class="fw-semibold text-truncate">${escapeHtml(rule.title || triggerLabel)}</div>
                            <div class="small text-muted">
                                ${escapeHtml(triggerLabel)} · ${escapeHtml(scopeText)}
                            </div>
                            <div class="mt-1">${extras}</div>
                        </div>
                        <div class="form-check form-switch m-0">
                            <input class="form-check-input" type="checkbox" data-action="toggle" ${enabled ? 'checked' : ''}>
                        </div>
                        <button class="btn btn-sm btn-outline-secondary" data-action="edit" title="Edit">
                            <i class="fas fa-pen"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-danger" data-action="delete" title="Delete">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    // Wire up per-card actions
    container.querySelectorAll('[data-rule-id]').forEach(card => {
        const id = card.dataset.ruleId;
        card.querySelector('[data-action="toggle"]').addEventListener('change', (ev) => {
            const rules = loadRules();
            const r = rules.find(x => x.id === id);
            if (!r) return;
            r.enabled = ev.target.checked;
            saveRules(rules);
            renderRulesList();
        });
        card.querySelector('[data-action="edit"]').addEventListener('click', () => openRuleEditor(id));
        card.querySelector('[data-action="delete"]').addEventListener('click', () => {
            if (!confirm('Delete this notification rule?')) return;
            const rules = loadRules().filter(x => x.id !== id);
            saveRules(rules);
            renderRulesList();
        });
    });
}

// ---------------------------------------------------------------------------
// Rule editor modal
// ---------------------------------------------------------------------------

function openRuleEditor(ruleId) {
    const rules = loadRules();
    const rule = ruleId
        ? rules.find(r => r.id === ruleId)
        : {
            id: newRuleId(),
            enabled: true,
            trigger: 'motion_detected',
            scope: 'all',
            devices: [],
            cooldownMinutes: 5,
        };
    if (!rule) return;

    // Remove any existing editor
    document.getElementById('notifRuleEditorModal')?.remove();

    const triggersByCategory = {};
    Object.entries(TRIGGERS).forEach(([key, t]) => {
        if (!triggersByCategory[t.category]) triggersByCategory[t.category] = [];
        triggersByCategory[t.category].push({ key, ...t });
    });

    const categoryLabels = {
        motion:      'Motion',
        contact:     'Doors & Windows',
        safety:      'Safety',
        control:     'Buttons & Controls',
        maintenance: 'Maintenance',
        heating:     'Heating',
    };

    const triggerOptions = Object.entries(triggersByCategory)
        .map(([cat, items]) => `
            <optgroup label="${escapeHtml(categoryLabels[cat] || cat)}">
                ${items.map(t => `
                    <option value="${t.key}" ${t.key === rule.trigger ? 'selected' : ''}>${escapeHtml(t.label)}</option>
                `).join('')}
            </optgroup>
        `).join('');

    const devices = getAllDevices();
    const deviceCheckboxes = devices.map(d => {
        const checked = (rule.devices || []).includes(d.ieee) ? 'checked' : '';
        // Searchable haystack — friendly name + ieee suffix, lowercased once
        const haystack = `${d.friendly_name || ''} ${d.ieee || ''}`.toLowerCase();
        return `
            <label class="list-group-item d-flex align-items-center gap-2 notif-device-item"
                   data-haystack="${escapeHtml(haystack)}">
                <input class="form-check-input m-0" type="checkbox" value="${escapeHtml(d.ieee)}" ${checked}>
                <span class="flex-grow-1 text-truncate">${escapeHtml(d.friendly_name || d.ieee)}</span>
                <small class="text-muted">${escapeHtml(d.protocol || 'zigbee')}</small>
            </label>
        `;
    }).join('');

    const tabs = (window.state && window.state.deviceTabs) || {};
    const tabOptions = Object.keys(tabs).map(t =>
        `<option value="${escapeHtml(t)}" ${rule.tab === t ? 'selected' : ''}>${escapeHtml(t)}</option>`
    ).join('');

    const html = `
        <div class="modal fade" id="notifRuleEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="fas fa-bell me-2"></i>${ruleId ? 'Edit' : 'Add'} Notification Rule
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">

                        <div class="mb-3">
                            <label class="form-label fw-bold">When this happens</label>
                            <select class="form-select" id="notifRuleTrigger">${triggerOptions}</select>
                        </div>

                        <div class="mb-3" id="notifRuleThresholdWrap" style="display:none;">
                            <label class="form-label fw-bold">Threshold (°C)</label>
                            <input type="number" step="0.1" class="form-control" id="notifRuleThreshold"
                                   value="${escapeHtml(rule.threshold ?? '')}" placeholder="e.g. 5">
                            <small class="text-muted">The rule fires when the temperature crosses this value.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">For which devices</label>
                            <div class="btn-group w-100" role="group">
                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeAll" value="all" ${rule.scope === 'all' ? 'checked' : ''}>
                                <label class="btn btn-outline-primary" for="scopeAll">All devices</label>

                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeDevices" value="devices" ${rule.scope === 'devices' ? 'checked' : ''}>
                                <label class="btn btn-outline-primary" for="scopeDevices">Selected</label>

                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeTab" value="tab" ${rule.scope === 'tab' ? 'checked' : ''} ${Object.keys(tabs).length === 0 ? 'disabled' : ''}>
                                <label class="btn btn-outline-primary" for="scopeTab">Device tab</label>
                            </div>

                            <div id="notifRuleDevicesWrap" class="mt-2" style="display:${rule.scope === 'devices' ? 'block' : 'none'};">
                                <input type="search" class="form-control form-control-sm mb-2" id="notifRuleDeviceFilter" placeholder="Search devices...">
                                <div class="list-group notif-device-list" id="notifRuleDevices" style="max-height: 240px; overflow-y: auto;">
                                    ${deviceCheckboxes || '<div class="text-muted small p-2">No devices loaded yet.</div>'}
                                </div>
                            </div>

                            <div id="notifRuleTabWrap" class="mt-2" style="display:${rule.scope === 'tab' ? 'block' : 'none'};">
                                <select class="form-select" id="notifRuleTab">
                                    <option value="">-- Choose a tab --</option>
                                    ${tabOptions}
                                </select>
                            </div>
                        </div>

                        <div class="row g-2 mb-3">
                            <div class="col-6">
                                <label class="form-label fw-bold">Only between</label>
                                <input type="time" class="form-control" id="notifRuleTimeFrom" value="${escapeHtml(rule.timeFrom ?? '')}">
                            </div>
                            <div class="col-6">
                                <label class="form-label fw-bold">and</label>
                                <input type="time" class="form-control" id="notifRuleTimeTo" value="${escapeHtml(rule.timeTo ?? '')}">
                            </div>
                            <small class="text-muted ps-2">Leave blank to fire any time of day.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Cooldown</label>
                            <select class="form-select" id="notifRuleCooldown">
                                ${COOLDOWN_OPTIONS.map(o => `
                                    <option value="${o.value}" ${Number(rule.cooldownMinutes) === o.value ? 'selected' : ''}>${o.label}</option>
                                `).join('')}
                            </select>
                            <small class="text-muted">Don't re-notify the same device within this window.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Notification title <span class="text-muted small">(optional)</span></label>
                            <input type="text" class="form-control" id="notifRuleTitle" value="${escapeHtml(rule.title ?? '')}" placeholder="Default: trigger name">
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Notification message <span class="text-muted small">(optional)</span></label>
                            <input type="text" class="form-control" id="notifRuleMessage" value="${escapeHtml(rule.message ?? '')}" placeholder="Use {device} for the device name">
                        </div>

                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary" id="notifRuleSave">
                            <i class="fas fa-save me-1"></i>Save Rule
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', html);
    const modalEl = document.getElementById('notifRuleEditorModal');
    const modal = new bootstrap.Modal(modalEl);

    // Show/hide threshold field per trigger
    function syncThresholdVisibility() {
        const sel = document.getElementById('notifRuleTrigger').value;
        const t = TRIGGERS[sel];
        document.getElementById('notifRuleThresholdWrap').style.display =
            (t && t.needsThreshold) ? 'block' : 'none';
    }
    document.getElementById('notifRuleTrigger').addEventListener('change', syncThresholdVisibility);
    syncThresholdVisibility();

    // Scope radio handling
    modalEl.querySelectorAll('input[name="notifRuleScope"]').forEach(radio => {
        radio.addEventListener('change', () => {
            const v = modalEl.querySelector('input[name="notifRuleScope"]:checked').value;
            document.getElementById('notifRuleDevicesWrap').style.display = v === 'devices' ? 'block' : 'none';
            document.getElementById('notifRuleTabWrap').style.display     = v === 'tab'     ? 'block' : 'none';
        });
    });

    // Live device filter — matches against pre-built haystack (name + ieee).
    // Uses the `hidden` attribute rather than style.display so we don't fight
    // Bootstrap's flexbox display rules on .list-group-item.
    const filterInput = document.getElementById('notifRuleDeviceFilter');
    function applyDeviceFilter() {
        const q = (filterInput.value || '').trim().toLowerCase();
        modalEl.querySelectorAll('#notifRuleDevices .notif-device-item').forEach(lbl => {
            const hay = lbl.dataset.haystack || '';
            lbl.hidden = q && !hay.includes(q);
        });
    }
    if (filterInput) {
        filterInput.addEventListener('input', applyDeviceFilter);
        // `search` event fires on the × clear button in type=search inputs
        filterInput.addEventListener('search', applyDeviceFilter);
    }

    // Save handler
    document.getElementById('notifRuleSave').addEventListener('click', () => {
        const triggerKey = document.getElementById('notifRuleTrigger').value;
        const trigger = TRIGGERS[triggerKey];
        const scope = modalEl.querySelector('input[name="notifRuleScope"]:checked').value;

        const selectedDevices = Array.from(
            modalEl.querySelectorAll('#notifRuleDevices input[type="checkbox"]:checked')
        ).map(cb => cb.value);

        const updated = {
            ...rule,
            trigger: triggerKey,
            scope,
            devices: scope === 'devices' ? selectedDevices : [],
            tab: scope === 'tab' ? document.getElementById('notifRuleTab').value : null,
            timeFrom: document.getElementById('notifRuleTimeFrom').value || null,
            timeTo:   document.getElementById('notifRuleTimeTo').value   || null,
            cooldownMinutes: Number(document.getElementById('notifRuleCooldown').value) || 0,
            title:   document.getElementById('notifRuleTitle').value.trim()   || null,
            message: document.getElementById('notifRuleMessage').value.trim() || null,
            threshold: trigger?.needsThreshold
                ? document.getElementById('notifRuleThreshold').value
                : undefined,
        };

        // Validation
        if (scope === 'devices' && updated.devices.length === 0) {
            alert('Pick at least one device, or change the scope to "All devices".');
            return;
        }
        if (scope === 'tab' && !updated.tab) {
            alert('Pick a device tab.');
            return;
        }
        if (trigger?.needsThreshold && (updated.threshold === '' || updated.threshold === undefined)) {
            alert('This trigger requires a threshold value.');
            return;
        }

        const all = loadRules();
        const idx = all.findIndex(r => r.id === updated.id);
        if (idx >= 0) all[idx] = updated; else all.push(updated);
        saveRules(all);

        modal.hide();
        renderRulesList();

        if (window.toast) {
            window.toast.success(idx >= 0 ? 'Rule updated' : 'Rule added');
        }
    });

    modalEl.addEventListener('hidden.bs.modal', () => modalEl.remove());
    modal.show();
}

// ---------------------------------------------------------------------------
// Sub-tab init
// ---------------------------------------------------------------------------

function renderNotificationsPane() {
    const pane = document.getElementById('settingsNotifications');
    if (!pane || pane.dataset.rendered === '1') return;

    pane.innerHTML = `
        <div class="card shadow-sm">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <span class="fw-bold"><i class="fas fa-bell me-1"></i> Notification Rules</span>
                <button class="btn btn-sm btn-primary" id="notifAddRuleBtn">
                    <i class="fas fa-plus me-1"></i> Add Rule
                </button>
            </div>
            <div class="card-body">
                <div class="alert alert-info small mb-3" id="notifGlobalStatus">
                    <i class="fas fa-info-circle me-1"></i>
                    Rules fire browser notifications via the same channel as the bell icon in the navbar.
                    Make sure the master notification toggle there is <strong>enabled</strong> for delivery to work.
                </div>
                <div id="notifRulesList"></div>
            </div>
            <div class="card-footer bg-light small text-muted">
                Rules are stored locally in this browser. Clear your browser storage and they're gone.
            </div>
        </div>
    `;

    document.getElementById('notifAddRuleBtn').addEventListener('click', () => openRuleEditor(null));

    pane.dataset.rendered = '1';
    renderRulesList();
}

export function initNotifications() {
    // Hook the Settings → Notifications sub-tab
    const tabBtn = document.querySelector('[data-bs-target="#settingsNotifications"]');
    if (tabBtn) {
        tabBtn.addEventListener('shown.bs.tab', renderNotificationsPane);
    }

    // Re-render the rule list when the device cache changes (so new devices
    // appear in the picker without a reload).
    document.addEventListener('zbm:devices-updated', () => {
        // Cheap: only refresh the list view, not the editor modal
        if (document.getElementById('notifRulesList')) {
            renderRulesList();
        }
    });

    // Start the rule engine. Independent from pwa.js' own poller.
    setInterval(evaluateRules, POLL_INTERVAL_MS);

    // Expose for debugging
    window.zbmNotificationRules = {
        list:   loadRules,
        clear:  () => { saveRules([]); renderRulesList(); },
        evaluate: evaluateRules,
    };
}