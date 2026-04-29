/**
 * Device Settings tab — state-driven layout.
 *
 * The page layout depends on the current interview state:
 *
 *   INTERVIEWING — calm "in progress" view. Shows a progress strip and the
 *   advice text. No big call to action because the system is waiting for
 *   the device, not the user.
 *
 *   STALLED — wizard-style call to action. The dominant element is a
 *   "Wake your device" instruction with a live activity dot that lights
 *   up green when traffic from the device is detected. One big primary
 *   button: "Retry now". Diagnostic details collapsed below.
 *
 *   FAILED — a clear "this device needs re-pairing" message with one
 *   big primary button: "Delete & Pair Again". Diagnostic details
 *   collapsed below.
 *
 *   INTERVIEWED — minimal "Ready" banner plus the maintenance button row
 *   (Poll, Reconfigure, Re-Interview). Most users only ever see this.
 *
 *   UNKNOWN — fallback for missing data.
 *
 * Live updates from the backend arrive via the WebSocket
 * interview_status_update event (handled by websocket.js, which calls
 * applyInterviewStatusUpdate here). A separate device_activity event
 * gives us a "device just sent something" pulse for the activity dot.
 */

import { state } from '../state.js';
import { addLogEntry } from '../logging.js';
import { getTimestamp } from '../utils.js';

// ---------------------------------------------------------------------------
// Action result panel — updated by Poll / Reconfigure / Re-Interview
// ---------------------------------------------------------------------------

/**
 * Show or update the action result panel for a device.
 * Finds the [data-settings-section="action-result"] node inside the
 * [data-settings-ieee] root and writes html into it.
 */
function _setActionResult(ieee, html) {
    const root = document.querySelector(`[data-settings-ieee="${cssEscape(ieee)}"]`);
    if (!root) return;
    let panel = root.querySelector('[data-settings-section="action-result"]');
    if (!panel) return;
    panel.innerHTML = html;
}

function _actionSpinner(label) {
    return `
        <div class="d-flex align-items-center gap-2 text-muted small py-1">
            <div class="spinner-border spinner-border-sm" role="status"></div>
            ${_escape(label)}
        </div>
    `;
}

function _actionSuccess(label, detail) {
    return `
        <div class="alert alert-success py-2 mb-0 small d-flex align-items-start gap-2">
            <i class="fas fa-check-circle mt-1"></i>
            <div>
                <strong>${_escape(label)}</strong>
                ${detail ? `<div class="text-muted">${detail}</div>` : ''}
            </div>
        </div>
    `;
}

function _actionError(label, error) {
    return `
        <div class="alert alert-danger py-2 mb-0 small d-flex align-items-start gap-2">
            <i class="fas fa-times-circle mt-1"></i>
            <div>
                <strong>${_escape(label)} failed</strong>
                <div class="text-muted">${_escape(error || 'Unknown error')}</div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Sequence counter per ieee — incremented on each Poll click.
// applyPollResult only renders if the sequence matches, discarding
// responses from previous polls that arrive late.
const _pollSeq = new Map();

// Poll result — called from websocket.js on poll_result events
// ---------------------------------------------------------------------------

/**
 * Called by websocket.js when a poll_result event arrives.
 * Updates the action-result panel of the currently open Settings tab (if any).
 */
export function applyPollResult(payload) {
    if (!payload || !payload.ieee) return;
    const { ieee, success, message, attributes, seq } = payload;

    // Discard if a newer poll has already been fired for this device
    if (seq !== undefined && _pollSeq.get(ieee) !== seq) return;

    const root = document.querySelector(`[data-settings-ieee="${cssEscape(ieee)}"]`);
    if (!root) return;

    if (success) {
        let detail = '';
        // Use only the fresh attributes from the backend — never fall back to
        // the cache, which may still hold pre-poll values at this point.
        if (attributes && Object.keys(attributes).length > 0) {
            const SKIP = new Set(['last_seen', 'last_seen_ts', 'lqi', 'rssi', 'ieee', 'node_id']);
            const entries = Object.entries(attributes)
                .filter(([k]) => !SKIP.has(k) && !k.startsWith('_'))
                .sort(([a], [b]) => a.localeCompare(b));

            if (entries.length > 0) {
                const ts = new Date().toLocaleTimeString();
                const rows = entries.map(([k, v]) => `
                    <tr>
                        <th class="small text-muted text-nowrap" style="width:40%">${_humaniseKey(k)}</th>
                        <td class="small font-monospace">${_escape(JSON.stringify(v))}</td>
                    </tr>
                `).join('');
                detail = `
                    <details class="mt-2" open>
                        <summary class="small text-muted">Polled values <span class="fw-normal text-muted" style="font-size:0.75em">(as of ${ts})</span></summary>
                        <table class="table table-sm table-borderless mb-0 mt-1">
                            <tbody>${rows}</tbody>
                        </table>
                    </details>
                `;
            }
        }

        _setActionResult(ieee, _actionSuccess('Poll complete', message) + detail);
    } else {
        _setActionResult(ieee, _actionError('Poll', message));
    }
}

// ---------------------------------------------------------------------------
// Initial render — gives the modal something to show while we fetch real data
// ---------------------------------------------------------------------------

export function renderSettingsTab(device) {
    if (!device) return '';
    const isZigbee = !device.protocol || device.protocol === 'zigbee';
    if (!isZigbee) {
        return `
            <div class="alert alert-info">
                Settings actions are only available for Zigbee devices.
            </div>
        `;
    }
    return `
        <div data-settings-ieee="${device.ieee}">
            <div data-settings-section="action-result"></div>
            <div data-settings-section="main">
                <div class="text-center text-muted py-4">
                    <div class="spinner-border spinner-border-sm me-2" role="status"></div>
                    Loading interview status...
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab activation — fetch initial status
// ---------------------------------------------------------------------------

export async function initSettingsTab(ieee) {
    if (!ieee) return;
    try {
        const res = await fetch(`/api/device/${encodeURIComponent(ieee)}/interview_status`);
        const data = await res.json();
        if (data.success && data.status) {
            applyInterviewStatusUpdate(data.status);
        } else {
            applyInterviewStatusUpdate({
                ieee,
                state: 'unknown',
                advice: data.error || 'Could not load interview status.',
                facts: {},
                missing: {},
                can_retry: false,
                can_repair: false,
            });
        }
    } catch (e) {
        console.error('initSettingsTab failed', e);
    }
}

// ---------------------------------------------------------------------------
// Live update entry point — called from websocket.js
// ---------------------------------------------------------------------------

// Most-recent snapshot per ieee, used by the live activity pulse so we
// know the current state when a device_activity event arrives.
const _lastSnapshot = new Map();

export function applyInterviewStatusUpdate(snap) {
    if (!snap || !snap.ieee) return;
    _lastSnapshot.set(snap.ieee, snap);

    const root = document.querySelector(`[data-settings-ieee="${cssEscape(snap.ieee)}"]`);
    if (!root) return;

    const main = root.querySelector('[data-settings-section="main"]');
    if (!main) return;

    main.innerHTML = _renderForState(snap);
    _bindActions(main, snap);
}

/**
 * Live activity pulse — called by websocket.js on device_activity events.
 * Only meaningful while the device is in a non-interviewed state, where
 * we're showing the activity dot in the wizard view.
 */
export function pulseDeviceActivity(ieee) {
    if (!ieee) return;
    const root = document.querySelector(`[data-settings-ieee="${cssEscape(ieee)}"]`);
    if (!root) return;
    const dot = root.querySelector('[data-activity-dot]');
    if (!dot) return;

    dot.classList.remove('text-muted');
    dot.classList.add('text-success');
    dot.querySelector('.activity-label').textContent = 'Device active just now';

    clearTimeout(dot._fadeTimer);
    dot._fadeTimer = setTimeout(() => {
        dot.classList.remove('text-success');
        dot.classList.add('text-muted');
        dot.querySelector('.activity-label').textContent = 'Waiting for device...';
    }, 4000);
}

// ---------------------------------------------------------------------------
// State-driven layouts
// ---------------------------------------------------------------------------

function _renderForState(snap) {
    switch (snap.state) {
        case 'interviewed':  return _layoutInterviewed(snap);
        case 'stalled':      return _layoutStalled(snap);
        case 'failed':       return _layoutFailed(snap);
        case 'interviewing': return _layoutInterviewing(snap);
        case 'unknown':
        default:             return _layoutUnknown(snap);
    }
}

// --- Layout: INTERVIEWED ----------------------------------------------------

function _layoutInterviewed(snap) {
    return `
        <div class="alert alert-success py-2 mb-3 d-flex align-items-center">
            <i class="fas fa-check-circle me-2 fs-5"></i>
            <div>
                <strong>Ready</strong>
                <div class="small">${_escape(snap.advice)}</div>
            </div>
        </div>

        <h6 class="text-primary border-bottom pb-1 mb-2">Maintenance</h6>
        ${_buttonRow(snap)}

        ${_collapsibleDetails(snap)}
    `;
}

// --- Layout: STALLED — the important one ------------------------------------

function _layoutStalled(snap) {
    const wakeInstruction = _wakeInstructionFor(snap);
    return `
        <div class="card border-warning mb-3">
            <div class="card-header bg-warning bg-opacity-25 d-flex align-items-center">
                <i class="fas fa-exclamation-triangle text-warning me-2 fs-5"></i>
                <strong>This device needs your help</strong>
            </div>
            <div class="card-body">
                <p class="mb-3">${_escape(snap.advice)}</p>

                <div class="bg-light p-3 rounded mb-3">
                    <div class="fw-bold mb-2">
                        <i class="fas fa-hand-pointer me-1"></i> Step 1: Wake the device
                    </div>
                    <div class="small mb-3">${wakeInstruction}</div>

                    <div data-activity-dot class="text-muted small d-flex align-items-center mb-3">
                        <i class="fas fa-circle me-2" style="font-size: 0.6rem;"></i>
                        <span class="activity-label">Waiting for device...</span>
                    </div>

                    <div class="fw-bold mb-2">
                        <i class="fas fa-redo me-1"></i> Step 2: Retry the interview
                    </div>
                    <div class="small text-muted mb-2">
                        Click below WHILE the device is awake (within a few seconds
                        of pressing its button or waking it).
                    </div>
                    <button type="button" class="btn btn-primary btn-lg w-100"
                            data-action="retry-interview">
                        <i class="fas fa-fingerprint me-1"></i> Retry Interview Now
                    </button>
                </div>

                <details class="small">
                    <summary class="text-muted">If retrying repeatedly doesn't work...</summary>
                    <div class="mt-2">
                        Some devices won't recover from a stalled interview without
                        being re-paired. After 3-5 failed retries with the device
                        clearly awake, the best path is to delete it and pair it
                        fresh:
                        <button type="button" class="btn btn-sm btn-outline-danger mt-2 d-block"
                                data-action="delete-repair">
                            <i class="fas fa-trash-restore me-1"></i> Delete &amp; Pair Again
                        </button>
                    </div>
                </details>
            </div>
        </div>

        ${_collapsibleDetails(snap)}
    `;
}

// --- Layout: FAILED ---------------------------------------------------------

function _layoutFailed(snap) {
    return `
        <div class="card border-danger mb-3">
            <div class="card-header bg-danger bg-opacity-25 d-flex align-items-center">
                <i class="fas fa-times-circle text-danger me-2 fs-5"></i>
                <strong>Interview failed — this device needs re-pairing</strong>
            </div>
            <div class="card-body">
                <p class="mb-3">${_escape(snap.advice)}</p>

                <div class="bg-light p-3 rounded mb-3">
                    <div class="fw-bold mb-2">What to do next</div>
                    <ol class="small mb-3">
                        <li>Click "Delete &amp; Pair Again" below</li>
                        <li>Put the device into pairing mode (reset / button-press as per its manual)</li>
                        <li>Open Permit Join from the main menu</li>
                        <li>Wait for the device to appear</li>
                    </ol>
                    <button type="button" class="btn btn-danger btn-lg w-100"
                            data-action="delete-repair">
                        <i class="fas fa-trash-restore me-1"></i> Delete &amp; Pair Again
                    </button>
                </div>

                <details class="small">
                    <summary class="text-muted">Try one more time before deleting</summary>
                    <div class="mt-2">
                        If you'd like to attempt one more interview with the
                        device awake, you can:
                        <button type="button" class="btn btn-sm btn-outline-primary mt-2 d-block"
                                data-action="retry-interview">
                            <i class="fas fa-fingerprint me-1"></i> Retry Interview
                        </button>
                    </div>
                </details>
            </div>
        </div>

        ${_collapsibleDetails(snap)}
    `;
}

// --- Layout: INTERVIEWING ---------------------------------------------------

function _layoutInterviewing(snap) {
    const stepLine = snap.current_step
        ? `<div class="small text-muted">${_humaniseStep(snap.current_step)}...</div>`
        : '';
    const elapsed = snap.elapsed_s != null
        ? `<span class="badge bg-secondary ms-2">${_formatDuration(snap.elapsed_s)}</span>`
        : '';

    return `
        <div class="card border-info mb-3">
            <div class="card-body">
                <div class="d-flex align-items-center mb-2">
                    <div class="spinner-border spinner-border-sm text-info me-2" role="status"></div>
                    <strong>Interviewing</strong>
                    ${elapsed}
                </div>
                <p class="small mb-2">${_escape(snap.advice)}</p>
                ${stepLine}
            </div>
        </div>

        <div class="alert alert-light border small mb-3">
            <i class="fas fa-info-circle me-1"></i>
            The system is waiting for the device. No action needed yet.
            If this state persists, the page will switch to a guided
            recovery view automatically.
        </div>

        ${_collapsibleDetails(snap)}
    `;
}

// --- Layout: UNKNOWN --------------------------------------------------------

function _layoutUnknown(snap) {
    return `
        <div class="alert alert-secondary py-2 mb-3">
            ${_escape(snap.advice || 'Status unknown.')}
        </div>
        ${_collapsibleDetails(snap)}
    `;
}

// ---------------------------------------------------------------------------
// Common bits
// ---------------------------------------------------------------------------

function _buttonRow(snap) {
    const ieee = snap.ieee;
    const retryDisabled = snap.can_retry ? '' : 'disabled';
    const repairDisabled = snap.can_repair ? '' : 'disabled';
    return `
        <div class="btn-group btn-group-sm flex-wrap mb-3" role="group">
            <button type="button" class="btn btn-outline-secondary"
                    onclick="window._settingsPoll('${ieee}')">
                <i class="fas fa-sync"></i> Poll
            </button>
            <button type="button" class="btn btn-outline-info"
                    onclick="window._settingsReconfigure('${ieee}', undefined)"
                    title="Standard Bindings &amp; Reporting">
                <i class="fas fa-wrench"></i> Reconfigure
            </button>
            <button type="button" class="btn btn-outline-warning"
                    onclick="window._settingsReconfigure('${ieee}', true)">
                <i class="fas fa-bolt"></i> Aggressive LQI
            </button>
            <button type="button" class="btn btn-outline-secondary"
                    onclick="window._settingsReconfigure('${ieee}', false)">
                <i class="fas fa-undo"></i> Restore Baseline
            </button>
            <button type="button" class="btn btn-outline-primary"
                    data-action="retry-interview" ${retryDisabled}>
                <i class="fas fa-fingerprint"></i> Re-Interview
            </button>
            <button type="button" class="btn btn-outline-danger"
                    data-action="delete-repair" ${repairDisabled}>
                <i class="fas fa-trash-restore"></i> Delete &amp; Re-pair
            </button>
        </div>
    `;
}

function _collapsibleDetails(snap) {
    return `
        <details class="mt-3">
            <summary class="text-muted small">Show technical details</summary>
            <div class="mt-2">
                ${_renderProgressIfAny(snap)}
                <h6 class="text-primary border-bottom pb-1 mb-2 mt-3">What we know</h6>
                ${_renderFacts(snap.facts || {})}
                <h6 class="text-primary border-bottom pb-1 mb-2 mt-3">What's missing</h6>
                ${_renderMissing(snap.missing || {})}
            </div>
        </details>
    `;
}

function _renderProgressIfAny(snap) {
    if (!snap.current_step) return '';
    return `
        <h6 class="text-primary border-bottom pb-1 mb-2 mt-3">Current step</h6>
        <div class="d-flex align-items-center small">
            <div class="spinner-border spinner-border-sm me-2" role="status"></div>
            ${_humaniseStep(snap.current_step)}
        </div>
    `;
}

function _renderFacts(facts) {
    const keys = Object.keys(facts);
    if (keys.length === 0) {
        return `
            <div class="text-muted small">
                Nothing known yet. Once the device replies to the Node Descriptor
                request, this will populate.
            </div>
        `;
    }
    const rows = keys.map(k => {
        const f = facts[k];
        const raw = f && f.raw != null ? f.raw : '';
        const name = f && f.name != null ? f.name : '';
        const showRaw = raw !== '' && String(raw) !== String(name);
        return `
            <tr>
                <th class="small text-muted" style="width: 40%;">${_humaniseKey(k)}</th>
                <td class="small">${_escape(name)}</td>
                <td class="small text-muted font-monospace">${showRaw ? _escape(String(raw)) : ''}</td>
            </tr>
        `;
    }).join('');
    return `
        <table class="table table-sm table-borderless mb-0">
            <tbody>${rows}</tbody>
        </table>
    `;
}

function _renderMissing(missing) {
    const keys = Object.keys(missing);
    if (keys.length === 0) {
        return `
            <div class="alert alert-success py-2 mb-0 small">
                <i class="fas fa-check"></i>
                Nothing missing — everything zigpy needs has been discovered.
            </div>
        `;
    }
    const items = keys.map(k => {
        const v = missing[k];
        if (k === 'incomplete_endpoints' && Array.isArray(v)) {
            const eps = v.map(ep => `
                <li>
                    EP${ep.endpoint_id}: ${ep.in_clusters} in / ${ep.out_clusters} out
                    (status: <code>${_escape(ep.status)}</code>)
                    — ${_escape(ep.issue || '')}
                </li>
            `).join('');
            return `
                <div class="mb-2 small">
                    <strong>Incomplete endpoints</strong>
                    <ul class="mb-0">${eps}</ul>
                </div>
            `;
        }
        return `
            <div class="mb-2 small">
                <strong>${_humaniseKey(k)}</strong>
                <div class="text-muted">${_escape(String(v))}</div>
            </div>
        `;
    }).join('');
    return `<div class="alert alert-warning py-2 mb-0">${items}</div>`;
}

// ---------------------------------------------------------------------------
// Action binding
// ---------------------------------------------------------------------------

function _bindActions(root, snap) {
    root.querySelectorAll('[data-action="retry-interview"]').forEach(btn => {
        btn.addEventListener('click', () => startRetryInterview(snap.ieee));
    });
    root.querySelectorAll('[data-action="delete-repair"]').forEach(btn => {
        btn.addEventListener('click', () => deleteAndRepair(snap.ieee));
    });
}

// ---------------------------------------------------------------------------
// Window-scoped handlers for poll / reconfigure — called from onclick attrs
// ---------------------------------------------------------------------------

window._settingsPoll = async function(ieee) {
    // Increment sequence so any in-flight response from a previous poll is ignored
    const seq = (_pollSeq.get(ieee) ?? 0) + 1;
    _pollSeq.set(ieee, seq);

    _setActionResult(ieee, _actionSpinner('Polling device…'));
    addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: 'Poll sent.' });
    try {
        const res = await fetch('/api/device/poll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, seq }),
        });
        const data = await res.json();
        if (!data.success) {
            _setActionResult(ieee, _actionError('Poll', data.error));
        }
        // On success: leave spinner — applyPollResult() from WS event replaces it
    } catch (e) {
        _setActionResult(ieee, _actionError('Poll', e.message));
    }
};

window._settingsReconfigure = async function(ieee, aggressive) {
    const labels = { undefined: 'Reconfigure', true: 'Aggressive LQI', false: 'Restore Baseline' };
    const label = labels[String(aggressive)] ?? 'Reconfigure';
    _setActionResult(ieee, _actionSpinner(`${label}…`));
    addLogEntry({ timestamp: getTimestamp(), level: 'INFO', message: `${label} sent.` });
    try {
        const body = { ieee };
        if (aggressive !== undefined) body.aggressive = aggressive;
        const res = await fetch('/api/device/reconfigure', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            _setActionResult(ieee, _actionSuccess(label, data.message));
        } else {
            _setActionResult(ieee, _actionError(label, data.error));
        }
    } catch (e) {
        _setActionResult(ieee, _actionError(label, e.message));
    }
};

// ---------------------------------------------------------------------------
// Re-Interview flow — confirmation + invocation
// ---------------------------------------------------------------------------

export async function startRetryInterview(ieee) {
    const message =
        'Wake the device NOW and keep it awake for the next 60 seconds.\n\n' +
        '• Battery sensors: press the device button or open the cover\n' +
        '• TRVs: press a button or rotate the dial\n' +
        '• Relays/switches: usually already awake — just continue\n\n' +
        'Click OK when the device is awake.';
    if (!confirm(message)) return;

    try {
        const res = await fetch('/api/device/retry_interview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, confirm_awake: true }),
        });
        const data = await res.json();
        addLogEntry({
            timestamp: getTimestamp(),
            level: data.success ? 'INFO' : 'WARNING',
            message: data.success
                ? `Re-Interview complete: ${data.steps_succeeded} succeeded, ${data.steps_failed} failed`
                : `Re-Interview: ${data.error || 'one or more steps failed'}`,
        });
    } catch (e) {
        console.error('retry_interview failed', e);
        alert('Re-Interview failed: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Delete-and-repair flow
// ---------------------------------------------------------------------------

export async function deleteAndRepair(ieee) {
    const ok = confirm(
        'This will permanently delete the device. After it is removed, you ' +
        'will need to put it back into pairing mode and re-pair it.\n\n' +
        'Continue?'
    );
    if (!ok) return;
    try {
        const res = await fetch('/api/device/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, force: false, ban: false }),
        });
        const data = await res.json();
        if (data.success) {
            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: 'Device removed. Open Permit Join when ready to re-pair.',
            });
            const closer = document.querySelector('#deviceModal [data-bs-dismiss="modal"]');
            if (closer) closer.click();
        } else {
            alert(`Error: ${data.error || 'unknown'}`);
        }
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _wakeInstructionFor(snap) {
    // Use the manufacturer name (if known) to give a more specific hint.
    // Falls back to a generic instruction.
    const mfr = snap.facts?.manufacturer_name?.name
        || snap.facts?.manufacturer_code?.name
        || '';
    const m = mfr.toLowerCase();

    if (m.includes('lumi') || m.includes('aqara') || m.includes('xiaomi')) {
        return `Press and hold the device's <strong>reset button</strong> for 5 seconds, ` +
               `or open and close the sensor (door/window/contact sensors). ` +
               `For Aqara TRVs, press the dial button.`;
    }
    if (m.includes('ikea')) {
        return `Press the device button briefly. For TRADFRI bulbs, toggle ` +
               `the wall switch. For remotes, press any button.`;
    }
    if (m.includes('hive')) {
        return `Press and hold the centre button on the SLT thermostat ` +
               `until the display lights up.`;
    }
    if (snap.is_battery === true) {
        return `Press the device's main button briefly to wake it. ` +
               `Battery devices sleep between transmissions.`;
    }
    if (snap.is_battery === false) {
        return `This appears to be a mains-powered device. ` +
               `Check it has power and is within range of the coordinator.`;
    }
    return `Press the device's main button to wake it. ` +
           `For battery devices this is usually a small button on the back or side.`;
}

function _humaniseKey(k) {
    return k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _humaniseStep(step) {
    if (!step) return '';
    if (step === 'node_descriptor') return 'Requesting Node Descriptor';
    if (step === 'active_endpoints') return 'Requesting Active Endpoints';
    const m = step.match(/^simple_descriptor_ep_(\d+)$/);
    if (m) return `Requesting Simple Descriptor for EP${m[1]}`;
    return step;
}

function _formatDuration(seconds) {
    if (seconds == null) return '';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function _escape(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function cssEscape(s) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
    return String(s).replace(/"/g, '\\"');
}