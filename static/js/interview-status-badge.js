/**
 * Device list interview-status badge.
 *
 * Renders nothing for devices in INTERVIEWED state. For interviewing,
 * stalled, or failed states, adds a small badge next to the friendly
 * name with the same advice text the Settings tab shows.
 *
 * Data flow:
 *   1. On startup, loadInterviewStatusPending() fetches all non-
 *      interviewed devices and populates the cache.
 *   2. WebSocket interview_status_update events call updateInterviewBadge
 *      which updates the cache and re-renders that one row.
 *   3. Full table re-renders call applyAllBadges() to restore badges on
 *      the freshly-rendered DOM.
 */

import { state } from './state.js';

// In-memory cache of the latest snapshot per ieee. Only contains devices
// in non-interviewed state — interviewed devices are removed.
const _pending = new Map();

export async function loadInterviewStatusPending() {
    try {
        const res = await fetch('/api/devices/interview_status_pending');
        const data = await res.json();
        if (data.success && Array.isArray(data.pending)) {
            _pending.clear();
            data.pending.forEach(snap => _pending.set(snap.ieee, snap));
            applyAllBadges();
        }
    } catch (e) {
        console.debug('loadInterviewStatusPending failed', e);
    }
}

export function updateInterviewBadge(snap) {
    if (!snap || !snap.ieee) return;
    if (snap.state === 'interviewed') {
        _pending.delete(snap.ieee);
    } else {
        _pending.set(snap.ieee, snap);
    }
    applyBadgeForRow(snap.ieee);
}

export function applyAllBadges() {
    if (!state || !Array.isArray(state.devices)) return;
    state.devices.forEach(d => applyBadgeForRow(d.ieee));
}

function applyBadgeForRow(ieee) {
    if (!ieee) return;
    const tr = document.querySelector(`tr[data-ieee="${cssEscape(ieee)}"]`);
    if (!tr) return;
    // Friendly-name cell is the second column in renderDeviceTable
    const nameCell = tr.querySelector('td:nth-child(2)');
    if (!nameCell) return;

    const existing = nameCell.querySelector('[data-interview-badge]');
    const snap = _pending.get(ieee);

    if (!snap || snap.state === 'interviewed') {
        if (existing) existing.remove();
        return;
    }

    const [bgClass, label, title] = labelFor(snap);
    const html = `<span class="badge ${bgClass} ms-2"
                       data-interview-badge
                       title="${escapeAttr(title)}"
                       style="font-size: 0.65rem; cursor: help;">
                    ${label}
                  </span>`;
    if (existing) {
        existing.outerHTML = html;
    } else {
        const nameDiv = nameCell.querySelector('.fw-bold');
        if (nameDiv) {
            nameDiv.insertAdjacentHTML('beforeend', html);
        }
    }
}

function labelFor(snap) {
    switch (snap.state) {
        case 'interviewing':
            return [
                'bg-info text-dark',
                'Interviewing',
                snap.advice || 'Interview in progress',
            ];
        case 'stalled':
            return [
                'bg-warning text-dark',
                'Stalled',
                snap.advice || 'Interview stalled — open Settings tab',
            ];
        case 'failed':
            return [
                'bg-danger',
                'Failed',
                snap.advice || 'Interview failed — needs re-pairing',
            ];
        default:
            return [
                'bg-secondary',
                snap.state || 'Unknown',
                snap.advice || '',
            ];
    }
}

function escapeAttr(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function cssEscape(s) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
    return String(s).replace(/"/g, '\\"');
}