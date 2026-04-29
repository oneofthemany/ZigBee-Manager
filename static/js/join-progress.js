/**
 * join-progress.js — Live onboarding tracker for newly joined devices.
 *
 * Shows a persistent card in the bottom-right corner when a device joins,
 * walking through each stage of the onboarding journey:
 *
 *   Joined → Interviewing → Interviewed → Configuring → Polling → Ready ✓
 *
 * Data arrives via WebSocket events:
 *   device_joined          → create card, mark "Joined"
 *   interview_status_update → update interview stage (interviewing / stalled / failed / interviewed)
 *   join_progress          → configuring / polling / ready / error
 *
 * The card auto-dismisses 8 seconds after "ready", or can be closed manually.
 * Multiple devices joining simultaneously each get their own card.
 */

// ─── Stage definitions ──────────────────────────────────────────────────────

const STAGES = [
    { key: 'joined',       label: 'Joined',       icon: 'fa-plug' },
    { key: 'interviewing', label: 'Interviewing',  icon: 'fa-search' },
    { key: 'interviewed',  label: 'Interviewed',   icon: 'fa-check' },
    { key: 'configuring',  label: 'Configuring',   icon: 'fa-wrench' },
    { key: 'polling',      label: 'Initial Poll',  icon: 'fa-sync' },
    { key: 'ready',        label: 'Ready',         icon: 'fa-check-circle' },
];

// Special non-progress states
const PROBLEM_STATES = new Set(['stalled', 'failed', 'error']);

// ─── Per-device tracker state ────────────────────────────────────────────────

// ieee → { stage, label, cardEl, autoDismissTimer, stalled }
const _trackers = new Map();

// ─── Container ───────────────────────────────────────────────────────────────

function _ensureContainer() {
    let c = document.getElementById('join-progress-container');
    if (!c) {
        c = document.createElement('div');
        c.id = 'join-progress-container';
        Object.assign(c.style, {
            position: 'fixed',
            bottom: '1rem',
            right: '1rem',
            zIndex: '1090',
            display: 'flex',
            flexDirection: 'column',
            gap: '0.5rem',
            maxWidth: '340px',
            width: '100%',
        });
        document.body.appendChild(c);
    }
    return c;
}

// ─── Card creation ────────────────────────────────────────────────────────────

function _createCard(ieee, friendlyName) {
    const container = _ensureContainer();

    const card = document.createElement('div');
    card.className = 'card shadow-sm border-0';
    card.dataset.joinIeee = ieee;
    Object.assign(card.style, {
        fontSize: '0.82rem',
        borderLeft: '3px solid var(--bs-primary)',
        opacity: '0',
        transform: 'translateY(8px)',
        transition: 'opacity 0.25s ease, transform 0.25s ease',
    });

    const displayName = friendlyName && friendlyName !== ieee
        ? `<strong>${_esc(friendlyName)}</strong>`
        : `<span class="font-monospace" style="font-size:0.75rem">${_esc(ieee)}</span>`;

    card.innerHTML = `
        <div class="card-header d-flex justify-content-between align-items-center py-2 px-3"
             style="background:transparent; border-bottom: 1px solid rgba(0,0,0,0.07)">
            <div class="d-flex align-items-center gap-2">
                <i class="fas fa-satellite-dish text-primary" style="font-size:0.9rem"></i>
                <span>New device</span>
                ${displayName}
            </div>
            <button type="button" class="btn-close btn-close-sm" aria-label="Dismiss"
                    style="font-size:0.65rem" data-join-dismiss="${_esc(ieee)}"></button>
        </div>
        <div class="card-body py-2 px-3">
            <div data-join-stages class="d-flex flex-column gap-1">
                ${_renderStages('joined', null)}
            </div>
            <div data-join-note class="text-muted mt-2" style="min-height:1.2em; font-size:0.77rem"></div>
        </div>
    `;

    card.querySelector('[data-join-dismiss]').addEventListener('click', () => _dismiss(ieee));
    container.appendChild(card);

    // Animate in
    requestAnimationFrame(() => {
        card.style.opacity = '1';
        card.style.transform = 'translateY(0)';
    });

    return card;
}

// ─── Stage rendering ──────────────────────────────────────────────────────────

function _renderStages(currentKey, problemState) {
    const currentIdx = STAGES.findIndex(s => s.key === currentKey);

    return STAGES.map((s, i) => {
        let iconHtml, textClass;

        if (problemState && s.key === currentKey) {
            // Stalled or failed on this stage
            const isProblem = problemState === 'failed' || problemState === 'error';
            iconHtml = `<i class="fas ${isProblem ? 'fa-times-circle text-danger' : 'fa-exclamation-triangle text-warning'}"
                           style="width:14px; text-align:center"></i>`;
            textClass = isProblem ? 'text-danger' : 'text-warning';
        } else if (i < currentIdx) {
            // Done
            iconHtml = `<i class="fas fa-check-circle text-success" style="width:14px; text-align:center"></i>`;
            textClass = 'text-muted';
        } else if (i === currentIdx) {
            // Active
            if (currentKey === 'ready') {
                iconHtml = `<i class="fas fa-check-circle text-success" style="width:14px; text-align:center"></i>`;
                textClass = 'text-success fw-semibold';
            } else {
                iconHtml = `<div class="spinner-border spinner-border-sm text-primary"
                                 role="status" style="width:14px; height:14px; border-width:2px"></div>`;
                textClass = 'text-primary fw-semibold';
            }
        } else {
            // Pending
            iconHtml = `<i class="fas ${s.icon} text-muted" style="width:14px; text-align:center; opacity:0.35"></i>`;
            textClass = 'text-muted';
        }

        return `
            <div class="d-flex align-items-center gap-2">
                ${iconHtml}
                <span class="${textClass}">${s.label}</span>
            </div>
        `;
    }).join('');
}

// ─── Update helpers ───────────────────────────────────────────────────────────

function _updateCard(ieee, stageKey, problemState, note) {
    const tracker = _trackers.get(ieee);
    if (!tracker) return;

    const card = tracker.cardEl;
    if (!card || !card.isConnected) return;

    const stagesDiv = card.querySelector('[data-join-stages]');
    if (stagesDiv) stagesDiv.innerHTML = _renderStages(stageKey, problemState);

    const noteDiv = card.querySelector('[data-join-note]');
    if (noteDiv) noteDiv.textContent = note || '';

    // Update border colour
    if (problemState === 'failed' || problemState === 'error') {
        card.style.borderLeftColor = 'var(--bs-danger)';
    } else if (problemState === 'stalled') {
        card.style.borderLeftColor = 'var(--bs-warning)';
    } else if (stageKey === 'ready') {
        card.style.borderLeftColor = 'var(--bs-success)';
    } else {
        card.style.borderLeftColor = 'var(--bs-primary)';
    }
}

function _dismiss(ieee) {
    const tracker = _trackers.get(ieee);
    if (!tracker) return;
    clearTimeout(tracker.autoDismissTimer);
    const card = tracker.cardEl;
    if (card && card.isConnected) {
        card.style.opacity = '0';
        card.style.transform = 'translateY(8px)';
        setTimeout(() => card.remove(), 260);
    }
    _trackers.delete(ieee);
}

function _scheduleDismiss(ieee, delayMs) {
    const tracker = _trackers.get(ieee);
    if (!tracker) return;
    clearTimeout(tracker.autoDismissTimer);
    tracker.autoDismissTimer = setTimeout(() => _dismiss(ieee), delayMs);
}

// ─── Public API — called from websocket.js ────────────────────────────────────

/**
 * A new device has joined the network.
 * payload: { ieee, friendly_name? }
 */
export function onDeviceJoined(payload) {
    const ieee = payload?.ieee;
    if (!ieee) return;

    // Ignore if we already have a tracker (duplicate event)
    if (_trackers.has(ieee)) return;

    const friendlyName = payload.friendly_name || payload.name || '';
    const card = _createCard(ieee, friendlyName);

    _trackers.set(ieee, {
        stage: 'joined',
        cardEl: card,
        autoDismissTimer: null,
    });
}

/**
 * Interview status update from InterviewStatusTracker.
 * payload: InterviewSnapshot.to_dict()
 */
export function onInterviewStatusUpdate(payload) {
    const ieee = payload?.ieee;
    if (!ieee || !_trackers.has(ieee)) return;

    const tracker = _trackers.get(ieee);
    const snapState = payload.state;

    if (snapState === 'interviewing') {
        tracker.stage = 'interviewing';
        _updateCard(ieee, 'interviewing', null,
            payload.current_step ? `Step: ${_humaniseStep(payload.current_step)}` : '');

    } else if (snapState === 'stalled') {
        _updateCard(ieee, 'interviewing', 'stalled',
            'Device not responding — wake it and retry from the Settings tab.');

    } else if (snapState === 'failed') {
        _updateCard(ieee, 'interviewing', 'failed',
            'Interview failed. Use Settings → Delete & Re-pair.');
        _scheduleDismiss(ieee, 30_000);

    } else if (snapState === 'interviewed') {
        tracker.stage = 'interviewed';
        _updateCard(ieee, 'interviewed', null, 'Device discovered — applying configuration…');
    }
}

/**
 * Backend join_progress events from _async_device_initialized.
 * payload: { ieee, stage: 'configuring' | 'polling' | 'ready' | 'error', error? }
 */
export function onJoinProgress(payload) {
    const ieee = payload?.ieee;
    if (!ieee || !_trackers.has(ieee)) return;

    const { stage, error } = payload;
    const tracker = _trackers.get(ieee);
    tracker.stage = stage;

    if (stage === 'configuring') {
        _updateCard(ieee, 'configuring', null, 'Binding clusters & setting up reporting…');

    } else if (stage === 'polling') {
        _updateCard(ieee, 'polling', null, 'Reading current attribute values…');

    } else if (stage === 'ready') {
        _updateCard(ieee, 'ready', null, 'Device is fully set up and reporting.');
        _scheduleDismiss(ieee, 8_000);

    } else if (stage === 'error') {
        _updateCard(ieee, tracker.stage, 'error', error || 'Configuration failed.');
        _scheduleDismiss(ieee, 20_000);
    }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function _esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _humaniseStep(step) {
    if (!step) return '';
    if (step === 'node_descriptor') return 'Node Descriptor';
    if (step === 'active_endpoints') return 'Active Endpoints';
    const m = step.match(/^simple_descriptor_ep_(\d+)$/);
    if (m) return `Simple Descriptor EP${m[1]}`;
    return step;
}