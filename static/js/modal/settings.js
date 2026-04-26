/**
 * Device Settings tab.
 *
 * Hosts the maintenance buttons (Poll, Reconfigure, Re-Interview, Re-pair)
 * and the rich interview status analysis. The analysis section shows what
 * we know, what's missing, and what the user should do — driven by the
 * backend interview_status module.
 *
 * Live updates arrive via the `interview_status_update` WebSocket event
 * (handled in websocket.js, which calls `applyInterviewStatusUpdate` from
 * here).
 */

import { state } from '../state.js';
import { addLogEntry, getTimestamp } from '../logging.js';

// ---------------------------------------------------------------------------
// Static rendering — initial HTML before live data arrives
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
    const ieee = device.ieee;

    return `
        <div data-settings-ieee="${ieee}">
            <h6 class="text-primary border-bottom pb-1 mb-3">Maintenance</h6>
            <div class="btn-group btn-group-sm mb-3 flex-wrap" role="group">
                <button type="button" class="btn btn-outline-secondary"
                        onclick="window.doAction('poll', '${ieee}')">
                    <i class="fas fa-sync"></i> Poll
                </button>
                <button type="button" class="btn btn-outline-info"
                        onclick="window.doAction('reconfigure', '${ieee}')"
                        title="Standard Bindings & Reporting">
                    <i class="fas fa-wrench"></i> Reconfigure
                </button>
                <button type="button" class="btn btn-outline-warning"
                        onclick="window.doAction('reconfigure_aggressive', '${ieee}')"
                        title="Apply aggressive LQI reporting">
                    <i class="fas fa-bolt"></i> Aggressive LQI
                </button>
                <button type="button" class="btn btn-outline-secondary"
                        onclick="window.doAction('reconfigure_baseline', '${ieee}')"
                        title="Restore baseline reporting">
                    <i class="fas fa-undo"></i> Restore Baseline
                </button>
                <button type="button" class="btn btn-outline-primary"
                        onclick="window.startRetryInterview('${ieee}')"
                        data-action="retry-interview">
                    <i class="fas fa-fingerprint"></i> Re-Interview
                </button>
                <button type="button" class="btn btn-outline-danger"
                        onclick="window.deleteAndRepair('${ieee}')"
                        data-action="delete-repair" disabled
                        title="Available when interview has failed">
                    <i class="fas fa-trash-restore"></i> Delete &amp; Re-pair
                </button>
            </div>

            <h6 class="text-primary border-bottom pb-1 mb-2 mt-4">Interview Status</h6>
            <div data-settings-section="status">
                <div class="text-muted small">Loading status…</div>
            </div>

            <h6 class="text-primary border-bottom pb-1 mb-2 mt-4">What we know</h6>
            <div data-settings-section="facts">
                <div class="text-muted small">Loading…</div>
            </div>

            <h6 class="text-primary border-bottom pb-1 mb-2 mt-4">What's missing</h6>
            <div data-settings-section="missing">
                <div class="text-muted small">Loading…</div>
            </div>

            <h6 class="text-primary border-bottom pb-1 mb-2 mt-4 d-none"
                data-settings-section="progress-heading">Interview progress</h6>
            <div data-settings-section="progress" class="d-none"></div>
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
                ieee, state: 'unknown',
                advice: data.error || 'Could not load interview status.',
                facts: {}, missing: {},
                can_retry: false, can_repair: false,
            });
        }
    } catch (e) {
        console.error('initSettingsTab failed', e);
    }
}

// ---------------------------------------------------------------------------
// Live update handler — called from websocket.js
// ---------------------------------------------------------------------------

export function applyInterviewStatusUpdate(snap) {
    if (!snap || !snap.ieee) return;
    const root = document.querySelector(`[data-settings-ieee="${snap.ieee}"]`);
    if (!root) return;

    _renderStatus(root, snap);
    _renderFacts(root, snap.facts || {});
    _renderMissing(root, snap.missing || {});
    _renderProgress(root, snap);
    _updateActionButtons(root, snap);
}

// ---------------------------------------------------------------------------
// Section renderers
// ---------------------------------------------------------------------------

function _renderStatus(root, snap) {
    const slot = root.querySelector('[data-settings-section="status"]');
    if (!slot) return;

    const stateBadge = _stateBadge(snap.state);
    const elapsed = snap.elapsed_s != null
        ? `<span class="text-muted small">${_formatDuration(snap.elapsed_s)} since join</span>`
        : '';
    const lastSeen = snap.last_seen_s_ago != null
        ? `<span class="text-muted small">last seen ${_formatDuration(snap.last_seen_s_ago)} ago</span>`
        : '';
    const power = snap.is_battery == null
        ? ''
        : `<span class="badge bg-secondary">${snap.is_battery ? 'Battery' : 'Mains'}</span>`;

    slot.innerHTML = `
        <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
            ${stateBadge}
            ${power}
            ${elapsed}
            ${lastSeen}
        </div>
        <div class="alert ${_adviceAlertClass(snap.state)} py-2 mb-0">
            ${_escape(snap.advice || '')}
        </div>
    `;
}

function _renderFacts(root, facts) {
    const slot = root.querySelector('[data-settings-section="facts"]');
    if (!slot) return;

    const keys = Object.keys(facts);
    if (keys.length === 0) {
        slot.innerHTML = `
            <div class="text-muted small">
                Nothing known yet. Once the device replies to the Node Descriptor
                request, this section will populate.
            </div>
        `;
        return;
    }

    const rows = keys.map(k => {
        const f = facts[k];
        const raw = f && f.raw != null ? f.raw : '';
        const name = f && f.name != null ? f.name : '';
        const showRaw = raw !== '' && String(raw) !== String(name);
        return `
            <tr>
                <th class="small text-muted" style="width: 40%;">${_humaniseKey(k)}</th>
                <td>${_escape(name)}</td>
                <td class="small text-muted">${showRaw ? _escape(String(raw)) : ''}</td>
            </tr>
        `;
    }).join('');

    slot.innerHTML = `
        <table class="table table-sm table-borderless mb-0">
            <thead>
                <tr>
                    <th class="small text-muted">Field</th>
                    <th class="small text-muted">Value</th>
                    <th class="small text-muted">Raw</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

function _renderMissing(root, missing) {
    const slot = root.querySelector('[data-settings-section="missing"]');
    if (!slot) return;

    const keys = Object.keys(missing);
    if (keys.length === 0) {
        slot.innerHTML = `
            <div class="alert alert-success py-2 mb-0">
                <i class="fas fa-check"></i>
                Nothing missing — everything zigpy needs has been discovered.
            </div>
        `;
        return;
    }

    const items = keys.map(k => {
        const v = missing[k];
        if (k === 'incomplete_endpoints' && Array.isArray(v)) {
            const eps = v.map(ep => `
                <li>
                    Endpoint ${ep.endpoint_id}: ${ep.in_clusters} input,
                    ${ep.out_clusters} output clusters discovered
                    (status: <code>${_escape(ep.status)}</code>)
                    — ${_escape(ep.issue || '')}
                </li>
            `).join('');
            return `
                <div class="mb-2">
                    <strong>Incomplete endpoints</strong>
                    <ul class="mb-0 small">${eps}</ul>
                </div>
            `;
        }
        return `
            <div class="mb-2">
                <strong>${_humaniseKey(k)}</strong>
                <div class="small text-muted">${_escape(String(v))}</div>
            </div>
        `;
    }).join('');

    slot.innerHTML = `
        <div class="alert alert-warning py-2 mb-0">
            ${items}
        </div>
    `;
}

function _renderProgress(root, snap) {
    const heading = root.querySelector('[data-settings-section="progress-heading"]');
    const slot = root.querySelector('[data-settings-section="progress"]');
    if (!heading || !slot) return;

    const live = !!snap.current_step;
    if (!live && !slot.dataset.hasHistory) {
        heading.classList.add('d-none');
        slot.classList.add('d-none');
        return;
    }
    heading.classList.remove('d-none');
    slot.classList.remove('d-none');
    slot.dataset.hasHistory = '1';

    if (live) {
        // Mark previous live row as completed and add a new in-flight row
        const oldLive = slot.querySelector('[data-progress-live="1"]');
        if (oldLive) {
            oldLive.removeAttribute('data-progress-live');
            const spinner = oldLive.querySelector('.spinner-border');
            if (spinner) spinner.remove();
            const ok = document.createElement('i');
            ok.className = 'fas fa-check text-success';
            oldLive.querySelector('[data-progress-icon]').appendChild(ok);
        }
        if (!slot.querySelector(`[data-progress-step="${snap.current_step}"]`)) {
            const row = document.createElement('div');
            row.className = 'd-flex align-items-center mb-1';
            row.dataset.progressStep = snap.current_step;
            row.dataset.progressLive = '1';
            row.innerHTML = `
                <span data-progress-icon class="me-2" style="width: 16px;">
                    <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                </span>
                <span class="small">${_humaniseStep(snap.current_step)}</span>
            `;
            slot.appendChild(row);
        }
    } else {
        // Interview finished — close out any live row
        const oldLive = slot.querySelector('[data-progress-live="1"]');
        if (oldLive) {
            oldLive.removeAttribute('data-progress-live');
            const spinner = oldLive.querySelector('.spinner-border');
            if (spinner) spinner.remove();
            const isOk = snap.state === 'interviewed';
            const icon = document.createElement('i');
            icon.className = isOk ? 'fas fa-check text-success' : 'fas fa-times text-danger';
            oldLive.querySelector('[data-progress-icon]').appendChild(icon);
        }
    }
}

function _updateActionButtons(root, snap) {
    const retryBtn = root.querySelector('[data-action="retry-interview"]');
    const repairBtn = root.querySelector('[data-action="delete-repair"]');
    if (retryBtn) {
        retryBtn.disabled = !snap.can_retry;
    }
    if (repairBtn) {
        repairBtn.disabled = !snap.can_repair;
        repairBtn.title = snap.can_repair
            ? 'Delete this device and re-pair it'
            : 'Available when interview has failed';
    }
}

// ---------------------------------------------------------------------------
// Re-interview flow — confirmation + invocation
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

function _stateBadge(s) {
    const map = {
        interviewed: ['bg-success', 'Interviewed'],
        interviewing: ['bg-info', 'Interviewing'],
        stalled: ['bg-warning text-dark', 'Stalled'],
        failed: ['bg-danger', 'Failed'],
        unknown: ['bg-secondary', 'Unknown'],
    };
    const [cls, label] = map[s] || map.unknown;
    return `<span class="badge ${cls}">${label}</span>`;
}

function _adviceAlertClass(s) {
    return ({
        interviewed: 'alert-success',
        interviewing: 'alert-info',
        stalled: 'alert-warning',
        failed: 'alert-danger',
        unknown: 'alert-secondary',
    })[s] || 'alert-secondary';
}

function _humaniseKey(k) {
    return k
        .replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
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