/**
 * HVAC Schedule Manager *
 * Calendar-based weekly schedule editor for thermostat devices.
 */

// ============================================================================
// STORAGE HELPERS
// ============================================================================

const STORAGE_KEY_PREFIX = 'hvac_schedule_';

function loadSchedule(ieee) {
    try {
        const raw = localStorage.getItem(STORAGE_KEY_PREFIX + ieee);
        if (raw) return JSON.parse(raw);
    } catch (e) {
        console.warn('Failed to load schedule:', e);
    }
    return getDefaultSchedule();
}

function saveSchedule(ieee, schedule) {
    try {
        localStorage.setItem(STORAGE_KEY_PREFIX + ieee, JSON.stringify(schedule));
    } catch (e) {
        console.warn('Failed to save schedule:', e);
    }
}

function getDefaultSchedule() {
    // Default: reasonable comfort schedule for all days
    const defaultDay = [
        { time: 360, heat: 20.0, label: '06:00' },  // Wake
        { time: 540, heat: 18.0, label: '09:00' },  // Away
        { time: 1020, heat: 21.0, label: '17:00' }, // Home
        { time: 1320, heat: 16.0, label: '22:00' }  // Sleep
    ];
    return {
        enabled: false,
        days: {
            0: JSON.parse(JSON.stringify(defaultDay)), // Mon
            1: JSON.parse(JSON.stringify(defaultDay)), // Tue
            2: JSON.parse(JSON.stringify(defaultDay)), // Wed
            3: JSON.parse(JSON.stringify(defaultDay)), // Thu
            4: JSON.parse(JSON.stringify(defaultDay)), // Fri
            5: JSON.parse(JSON.stringify(defaultDay)), // Sat
            6: JSON.parse(JSON.stringify(defaultDay)), // Sun
        }
    };
}

// ============================================================================
// DAY / TIME HELPERS
// ============================================================================

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_FULL  = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

// ZCL day bitmask: 0x01=Sun, 0x02=Mon, 0x04=Tue, 0x08=Wed, 0x10=Thu, 0x20=Fri, 0x40=Sat
// Our internal index: 0=Mon ... 6=Sun
// Mapping our index -> ZCL bitmask bit
const DAY_TO_ZCL_BIT = {
    0: 0x02, // Mon
    1: 0x04, // Tue
    2: 0x08, // Wed
    3: 0x10, // Thu
    4: 0x20, // Fri
    5: 0x40, // Sat
    6: 0x01, // Sun
};

function minutesToTime(mins) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function timeToMinutes(timeStr) {
    const [h, m] = timeStr.split(':').map(Number);
    return h * 60 + m;
}

// Color for temperature (cold blue -> warm red)
function tempToColor(temp) {
    // Range: 5°C (cold blue) to 30°C (hot red)
    const ratio = Math.max(0, Math.min(1, (temp - 5) / 25));
    if (ratio < 0.5) {
        // Blue to Yellow
        const r = Math.round(ratio * 2 * 255);
        const g = Math.round(ratio * 2 * 200);
        const b = Math.round((1 - ratio * 2) * 220);
        return `rgb(${r},${g},${b})`;
    } else {
        // Yellow to Red
        const t = (ratio - 0.5) * 2;
        const r = 255;
        const g = Math.round((1 - t) * 200);
        const b = Math.round((1 - t) * 40);
        return `rgb(${r},${g},${b})`;
    }
}

// ============================================================================
// RENDER: Schedule HTML for thermostat card
// ============================================================================

export function renderScheduleSection(ieee) {
    const schedule = loadSchedule(ieee);
    const isEnabled = schedule.enabled;

    // Build the calendar grid
    let calendarRows = '';
    for (let d = 0; d < 7; d++) {
        const dayTransitions = schedule.days[d] || [];
        const isToday = new Date().getDay() === (d === 6 ? 0 : d + 1); // JS: 0=Sun

        // Build the timeline bar (24h visual)
        let timelineSegments = '';
        for (let i = 0; i < dayTransitions.length; i++) {
            const t = dayTransitions[i];
            const nextTime = i < dayTransitions.length - 1 ? dayTransitions[i + 1].time : 1440;
            const startPct = (t.time / 1440) * 100;
            const widthPct = ((nextTime - t.time) / 1440) * 100;
            const color = tempToColor(t.heat);

            timelineSegments += `<div class="sched-seg" 
                style="left:${startPct}%;width:${widthPct}%;background:${color};"
                title="${minutesToTime(t.time)} — ${t.heat}°C"
                data-day="${d}" data-idx="${i}"></div>`;
        }

        // Build transition pills
        let pills = '';
        dayTransitions.forEach((t, idx) => {
            pills += `<span class="sched-pill" data-day="${d}" data-idx="${idx}"
                style="border-left: 3px solid ${tempToColor(t.heat)}">
                <span class="sched-pill-time">${minutesToTime(t.time)}</span>
                <span class="sched-pill-temp">${t.heat}°C</span>
                <button class="sched-pill-del" data-day="${d}" data-idx="${idx}" 
                    title="Remove">&times;</button>
            </span>`;
        });

        calendarRows += `
        <div class="sched-day-row ${isToday ? 'sched-today' : ''}" data-day="${d}">
            <div class="sched-day-label">
                <span class="sched-day-name">${DAY_NAMES[d]}</span>
                ${isToday ? '<span class="sched-today-dot"></span>' : ''}
            </div>
            <div class="sched-day-body">
                <div class="sched-timeline">${timelineSegments}</div>
                <div class="sched-pills">${pills}</div>
            </div>
            <div class="sched-day-actions">
                <button class="btn btn-sm btn-outline-secondary sched-add-btn" 
                    data-day="${d}" title="Add transition">
                    <i class="fas fa-plus"></i>
                </button>
            </div>
        </div>`;
    }

    // Hour markers for the timeline header
    let hourMarkers = '';
    for (let h = 0; h <= 24; h += 6) {
        hourMarkers += `<span class="sched-hour-mark" style="left:${(h / 24) * 100}%">${String(h).padStart(2, '0')}</span>`;
    }

    return `
    <div class="col-12">
        <div class="card" id="schedule-card-${ieee}">
            <div class="card-header bg-light d-flex justify-content-between align-items-center">
                <strong><i class="fas fa-calendar-alt"></i> Weekly Schedule</strong>
                <div class="d-flex align-items-center gap-2">
                    <div class="form-check form-switch mb-0">
                        <input class="form-check-input" type="checkbox" role="switch" 
                            id="sched-toggle-${ieee}" ${isEnabled ? 'checked' : ''}
                            onchange="window.toggleScheduleEnabled('${ieee}', this.checked)">
                        <label class="form-check-label small ${isEnabled ? 'text-success fw-bold' : 'text-muted'}" 
                            id="sched-toggle-label-${ieee}" for="sched-toggle-${ieee}">
                            ${isEnabled ? 'Enabled' : 'Disabled'}
                        </label>
                    </div>
                </div>
            </div>
            <div class="card-body p-2" id="sched-body-${ieee}" 
                style="${!isEnabled ? 'opacity:0.45;pointer-events:none;' : ''}">
                
                <!-- Timeline header -->
                <div class="sched-timeline-header">
                    <div class="sched-day-label"></div>
                    <div class="sched-hour-markers">${hourMarkers}</div>
                    <div class="sched-day-actions"></div>
                </div>

                <!-- Day rows -->
                ${calendarRows}

                <!-- Actions bar -->
                <div class="d-flex justify-content-between align-items-center mt-3 pt-2 border-top">
                    <div class="btn-group btn-group-sm">
                        <button class="btn btn-outline-secondary" 
                            onclick="window.copyScheduleDay('${ieee}')" title="Copy Monday to all days">
                            <i class="fas fa-copy"></i> Copy Mon → All
                        </button>
                        <button class="btn btn-outline-secondary" 
                            onclick="window.resetSchedule('${ieee}')" title="Reset to defaults">
                            <i class="fas fa-undo"></i> Reset
                        </button>
                    </div>
                    <button class="btn btn-primary btn-sm" 
                        onclick="window.uploadFullSchedule('${ieee}')">
                        <i class="fas fa-upload"></i> Upload to Device
                    </button>
                </div>
            </div>
        </div>
    </div>

    <style>
        /* ── Schedule Calendar Styles ── */
        .sched-timeline-header {
            display: flex;
            align-items: flex-end;
            padding-bottom: 2px;
            margin-bottom: 4px;
        }
        .sched-hour-markers {
            flex: 1;
            position: relative;
            height: 16px;
            font-size: 10px;
            color: #999;
        }
        .sched-hour-mark {
            position: absolute;
            transform: translateX(-50%);
            bottom: 0;
        }
        .sched-day-row {
            display: flex;
            align-items: center;
            padding: 3px 0;
            border-bottom: 1px solid #f0f0f0;
            transition: background 0.15s;
        }
        .sched-day-row:hover {
            background: #f8f9fa;
        }
        .sched-today {
            background: #f0f7ff !important;
        }
        .sched-today-dot {
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #0d6efd;
            margin-left: 4px;
            vertical-align: middle;
        }
        .sched-day-label {
            width: 44px;
            flex-shrink: 0;
            font-size: 11px;
            font-weight: 600;
            color: #555;
            text-align: right;
            padding-right: 8px;
        }
        .sched-day-name {
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .sched-day-body {
            flex: 1;
            min-width: 0;
        }
        .sched-timeline {
            position: relative;
            height: 14px;
            background: #e9ecef;
            border-radius: 3px;
            overflow: hidden;
            margin-bottom: 3px;
        }
        .sched-seg {
            position: absolute;
            top: 0;
            height: 100%;
            opacity: 0.75;
            cursor: pointer;
            transition: opacity 0.15s;
        }
        .sched-seg:hover {
            opacity: 1;
        }
        .sched-pills {
            display: flex;
            flex-wrap: wrap;
            gap: 3px;
        }
        .sched-pill {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: #fff;
            border: 1px solid #dee2e6;
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 11px;
            cursor: default;
            transition: box-shadow 0.15s;
        }
        .sched-pill:hover {
            box-shadow: 0 1px 4px rgba(0,0,0,0.12);
        }
        .sched-pill-time {
            font-weight: 600;
            color: #333;
        }
        .sched-pill-temp {
            color: #666;
        }
        .sched-pill-del {
            border: none;
            background: none;
            color: #ccc;
            cursor: pointer;
            padding: 0 2px;
            font-size: 13px;
            line-height: 1;
        }
        .sched-pill-del:hover {
            color: #dc3545;
        }
        .sched-day-actions {
            width: 34px;
            flex-shrink: 0;
            text-align: center;
        }
        .sched-add-btn {
            padding: 1px 5px;
            font-size: 10px;
            line-height: 1.2;
            border-radius: 3px;
        }

        /* ── Add Transition Modal (inline popover) ── */
        .sched-add-popover {
            position: fixed;
            z-index: 1060;
            background: #fff;
            border: 1px solid #dee2e6;
            border-radius: 8px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.15);
            padding: 12px;
            width: 220px;
        }
        .sched-add-popover .form-label {
            font-size: 11px;
            font-weight: 600;
            margin-bottom: 2px;
        }
        .sched-add-popover input {
            font-size: 13px;
        }
    </style>
    `;
}

// ============================================================================
// WINDOW-LEVEL HANDLERS
// ============================================================================

/**
 * Toggle schedule enabled/disabled
 */
window.toggleScheduleEnabled = function(ieee, enabled) {
    const schedule = loadSchedule(ieee);
    schedule.enabled = enabled;
    saveSchedule(ieee, schedule);

    const body = document.getElementById(`sched-body-${ieee}`);
    const label = document.getElementById(`sched-toggle-label-${ieee}`);
    if (body) {
        body.style.opacity = enabled ? '1' : '0.45';
        body.style.pointerEvents = enabled ? 'auto' : 'none';
    }
    if (label) {
        label.textContent = enabled ? 'Enabled' : 'Disabled';
        label.className = `form-check-label small ${enabled ? 'text-success fw-bold' : 'text-muted'}`;
    }
};

/**
 * Copy Monday schedule to all other days
 */
window.copyScheduleDay = function(ieee) {
    if (!confirm('Copy Monday\'s schedule to all other days?')) return;
    const schedule = loadSchedule(ieee);
    const mondayTransitions = JSON.parse(JSON.stringify(schedule.days[0] || []));
    for (let d = 1; d < 7; d++) {
        schedule.days[d] = JSON.parse(JSON.stringify(mondayTransitions));
    }
    saveSchedule(ieee, schedule);
    rerenderSchedule(ieee);
};

/**
 * Reset schedule to defaults
 */
window.resetSchedule = function(ieee) {
    if (!confirm('Reset schedule to defaults?')) return;
    const defaultSched = getDefaultSchedule();
    // Preserve enabled state
    const current = loadSchedule(ieee);
    defaultSched.enabled = current.enabled;
    saveSchedule(ieee, defaultSched);
    rerenderSchedule(ieee);
};

/**
 * Upload full weekly schedule to device
 */
window.uploadFullSchedule = async function(ieee) {
    const schedule = loadSchedule(ieee);

    if (!schedule.enabled) {
        alert('Schedule is disabled. Enable it first before uploading.');
        return;
    }

    const hasTransitions = Object.values(schedule.days).some(d => d && d.length > 0);
    if (!hasTransitions) {
        alert('No transitions defined. Add some schedule entries first.');
        return;
    }

    if (!confirm('This will overwrite the device\'s internal schedule. Continue?')) return;

    const btn = document.querySelector(`#schedule-card-${ieee} .btn-primary`);
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Uploading...';
    }

    try {
        // Send one command per day (ZCL SetWeeklySchedule works per-day bitmask)
        for (let d = 0; d < 7; d++) {
            const transitions = schedule.days[d] || [];
            if (transitions.length === 0) continue;

            const payload = {
                command: "set_schedule",
                value: {
                    day_of_week: DAY_TO_ZCL_BIT[d],
                    transitions: transitions.map(t => ({
                        time: t.time,
                        heat: t.heat
                    }))
                }
            };

            await fetch(`/api/device/${ieee}/command`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            // Small delay between day commands to avoid overwhelming the device
            await new Promise(r => setTimeout(r, 300));
        }

        alert('Schedule uploaded successfully!');
    } catch (error) {
        console.error('Schedule upload failed:', error);
        alert('Failed to upload schedule: ' + error.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-upload"></i> Upload to Device';
        }
    }
};

/**
 * Show add-transition popover for a given day
 */
window.showAddTransition = function(ieee, dayIndex, anchorEl) {
    // Remove any existing popover
    document.querySelectorAll('.sched-add-popover').forEach(p => p.remove());

    const rect = anchorEl.getBoundingClientRect();

    const popover = document.createElement('div');
    popover.className = 'sched-add-popover';
    popover.innerHTML = `
        <div class="mb-2">
            <label class="form-label">Time</label>
            <input type="time" class="form-control form-control-sm" id="sched-new-time" value="12:00">
        </div>
        <div class="mb-2">
            <label class="form-label">Temperature (°C)</label>
            <input type="number" class="form-control form-control-sm" id="sched-new-temp" 
                value="20" min="5" max="35" step="0.5">
        </div>
        <div class="d-flex justify-content-between">
            <button class="btn btn-sm btn-outline-secondary" onclick="this.closest('.sched-add-popover').remove()">Cancel</button>
            <button class="btn btn-sm btn-primary" onclick="window.addTransition('${ieee}', ${dayIndex})">Add</button>
        </div>
    `;

    popover.style.top = `${rect.bottom + 4}px`;
    popover.style.left = `${rect.left - 180}px`;
    document.body.appendChild(popover);

    // Close on outside click
    setTimeout(() => {
        const closeHandler = (e) => {
            if (!popover.contains(e.target) && e.target !== anchorEl) {
                popover.remove();
                document.removeEventListener('mousedown', closeHandler);
            }
        };
        document.addEventListener('mousedown', closeHandler);
    }, 50);
};

/**
 * Add a transition to a day
 */
window.addTransition = function(ieee, dayIndex) {
    const timeInput = document.getElementById('sched-new-time');
    const tempInput = document.getElementById('sched-new-temp');
    if (!timeInput || !tempInput) return;

    const time = timeToMinutes(timeInput.value);
    const heat = parseFloat(tempInput.value);

    if (isNaN(heat) || heat < 5 || heat > 35) {
        alert('Temperature must be between 5°C and 35°C');
        return;
    }

    const schedule = loadSchedule(ieee);
    if (!schedule.days[dayIndex]) schedule.days[dayIndex] = [];

    // Check for duplicate time
    const existing = schedule.days[dayIndex].find(t => t.time === time);
    if (existing) {
        existing.heat = heat; // Update existing
    } else {
        schedule.days[dayIndex].push({
            time: time,
            heat: heat,
            label: minutesToTime(time)
        });
    }

    // Sort by time
    schedule.days[dayIndex].sort((a, b) => a.time - b.time);

    saveSchedule(ieee, schedule);
    
    // Remove popover
    document.querySelectorAll('.sched-add-popover').forEach(p => p.remove());
    
    rerenderSchedule(ieee);
};

/**
 * Remove a transition
 */
window.removeTransition = function(ieee, dayIndex, transitionIndex) {
    const schedule = loadSchedule(ieee);
    if (schedule.days[dayIndex]) {
        schedule.days[dayIndex].splice(transitionIndex, 1);
        saveSchedule(ieee, schedule);
        rerenderSchedule(ieee);
    }
};

// ============================================================================
// RE-RENDER (updates just the schedule card body)
// ============================================================================

function rerenderSchedule(ieee) {
    const card = document.getElementById(`schedule-card-${ieee}`);
    if (!card) return;

    // Replace entire card content by re-calling renderScheduleSection
    // We create a temporary container to parse the new HTML
    const tmp = document.createElement('div');
    tmp.innerHTML = renderScheduleSection(ieee);
    const newCard = tmp.querySelector(`#schedule-card-${ieee}`);
    if (newCard) {
        card.innerHTML = newCard.innerHTML;
        // Rebind the add button click handlers
        bindScheduleEvents(ieee);
    }
}

// ============================================================================
// EVENT BINDING  (called after DOM insertion)
// ============================================================================

export function bindScheduleEvents(ieee) {
    const card = document.getElementById(`schedule-card-${ieee}`);
    if (!card) return;

    // Add transition buttons
    card.querySelectorAll('.sched-add-btn').forEach(btn => {
        btn.onclick = function(e) {
            e.stopPropagation();
            const day = parseInt(this.dataset.day);
            window.showAddTransition(ieee, day, this);
        };
    });

    // Delete transition buttons
    card.querySelectorAll('.sched-pill-del').forEach(btn => {
        btn.onclick = function(e) {
            e.stopPropagation();
            const day = parseInt(this.dataset.day);
            const idx = parseInt(this.dataset.idx);
            window.removeTransition(ieee, day, idx);
        };
    });
}
