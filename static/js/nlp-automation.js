/**
 * NLP Automation — Context-Aware Fuzzy Autocomplete
 * Location: static/js/nlp-automation.js
 *
 * Works like an IDE — fuzzy-searches ALL pools simultaneously,
 * ranks by context (what came before), and builds the rule from
 * what the user explicitly selected.
 *
 * NO eager parsing. Devices only "lock in" when clicked/tab'd.
 * LLM fallback for anything the grammar can't handle.
 */

// ============================================================================
// DATA (loaded once from API)
// ============================================================================

let _allDevices = [];    // All devices + groups
let _actuators = [];     // Only controllable devices/groups
let _deviceAttrs = {};   // ieee → attributes array
let _loaded = false;
let _selections = [];    // Track what user has selected: [{type, text, data}]

export async function loadRegistry() {
    if (_loaded) return;
    try {
        const [devRes, actRes] = await Promise.all([
            fetch('/api/automations/devices').then(r => r.json()),
            fetch('/api/automations/actuators').then(r => r.json()),
        ]);
        _allDevices = devRes || [];
        _actuators = actRes || [];

        // Pre-fetch attributes for devices (first 80)
        const promises = _allDevices.slice(0, 80).map(d =>
            fetch(`/api/automations/device/${encodeURIComponent(d.ieee)}/attributes`)
                .then(r => r.json())
                .then(attrs => { _deviceAttrs[d.ieee] = attrs; })
                .catch(() => {})
        );
        await Promise.all(promises);
        _loaded = true;
    } catch (e) {
        console.warn('NLP registry load failed:', e);
    }
}

// ============================================================================
// VOCABULARY
// ============================================================================

const KEYWORDS = [
    { text: 'when',     type: 'trigger',   hint: 'Start a trigger condition' },
    { text: 'if',       type: 'trigger',   hint: 'Start a trigger condition' },
    { text: 'whenever', type: 'trigger',   hint: 'Start a trigger condition' },
    { text: 'then',     type: 'connector', hint: 'Define the action to take' },
    { text: 'else',     type: 'connector', hint: 'Action when condition is false' },
    { text: 'otherwise',type: 'connector', hint: 'Action when condition is false' },
    { text: 'and',      type: 'connector', hint: 'Add another condition' },
    { text: 'at',       type: 'time',      hint: 'Specific time (e.g. 9am, 21:00)' },
    { text: 'between',  type: 'time',      hint: 'Time range (e.g. between 9am and 5pm)' },
    { text: 'after',    type: 'delay',     hint: 'Delay (e.g. after 5 minutes)' },
    { text: 'wait',     type: 'delay',     hint: 'Delay (e.g. wait 10 seconds)' },
    { text: 'only if',  type: 'prereq',    hint: 'Add a prerequisite condition' },
    { text: 'but only if', type: 'prereq', hint: 'Add a prerequisite condition' },
    { text: 'unless',   type: 'prereq',    hint: 'Negative prerequisite (NOT)' },
];

const ACTIONS = [
    { text: 'turn on',    type: 'action', command: 'on',     hint: 'Switch on a device' },
    { text: 'turn off',   type: 'action', command: 'off',    hint: 'Switch off a device' },
    { text: 'switch on',  type: 'action', command: 'on',     hint: 'Switch on a device' },
    { text: 'switch off', type: 'action', command: 'off',    hint: 'Switch off a device' },
    { text: 'toggle',     type: 'action', command: 'toggle', hint: 'Toggle on/off' },
    { text: 'open',       type: 'action', command: 'open',   hint: 'Open a cover/lock' },
    { text: 'close',      type: 'action', command: 'close',  hint: 'Close a cover/lock' },
    { text: 'stop',       type: 'action', command: 'stop',   hint: 'Stop a cover' },
    { text: 'lock',       type: 'action', command: 'lock',   hint: 'Lock a door' },
    { text: 'unlock',     type: 'action', command: 'unlock', hint: 'Unlock a door' },
    { text: 'set brightness', type: 'action', command: 'brightness', hint: 'Set brightness 0-254' },
    { text: 'set temperature', type: 'action', command: 'temperature', hint: 'Set thermostat target' },
];

const CONDITIONS = [
    { text: 'is on',           type: 'condition', attr: 'state', op: 'eq', val: 'ON' },
    { text: 'is off',          type: 'condition', attr: 'state', op: 'eq', val: 'OFF' },
    { text: 'turns on',        type: 'condition', attr: 'state', op: 'eq', val: 'ON' },
    { text: 'turns off',       type: 'condition', attr: 'state', op: 'eq', val: 'OFF' },
    { text: 'is open',         type: 'condition', attr: 'contact', op: 'eq', val: false },
    { text: 'is closed',       type: 'condition', attr: 'contact', op: 'eq', val: true },
    { text: 'opens',           type: 'condition', attr: 'contact', op: 'eq', val: false },
    { text: 'closes',          type: 'condition', attr: 'contact', op: 'eq', val: true },
    { text: 'detects motion',  type: 'condition', attr: 'occupancy', op: 'eq', val: true },
    { text: 'motion detected', type: 'condition', attr: 'occupancy', op: 'eq', val: true },
    { text: 'no motion',       type: 'condition', attr: 'occupancy', op: 'eq', val: false },
    { text: 'is above',        type: 'condition', op: 'gt', needsValue: true },
    { text: 'is below',        type: 'condition', op: 'lt', needsValue: true },
    { text: 'is greater than', type: 'condition', op: 'gt', needsValue: true },
    { text: 'is less than',    type: 'condition', op: 'lt', needsValue: true },
    { text: 'equals',          type: 'condition', op: 'eq', needsValue: true },
    { text: 'reaches',         type: 'condition', op: 'gte', needsValue: true },
    { text: 'drops below',     type: 'condition', op: 'lt', needsValue: true },
    { text: 'exceeds',         type: 'condition', op: 'gt', needsValue: true },
];

const TIMES = [
    { text: '6am',      type: 'time_val', time: '06:00' },
    { text: '7am',      type: 'time_val', time: '07:00' },
    { text: '8am',      type: 'time_val', time: '08:00' },
    { text: '9am',      type: 'time_val', time: '09:00' },
    { text: '10am',     type: 'time_val', time: '10:00' },
    { text: '12pm',     type: 'time_val', time: '12:00' },
    { text: '3pm',      type: 'time_val', time: '15:00' },
    { text: '6pm',      type: 'time_val', time: '18:00' },
    { text: '9pm',      type: 'time_val', time: '21:00' },
    { text: '10pm',     type: 'time_val', time: '22:00' },
    { text: 'midnight', type: 'time_val', time: '00:00' },
    { text: 'sunrise',  type: 'time_val', time: '06:30' },
    { text: 'sunset',   type: 'time_val', time: '18:00' },
    { text: 'noon',     type: 'time_val', time: '12:00' },
];

const DURATIONS = [
    { text: '5 seconds',  type: 'duration', seconds: 5 },
    { text: '10 seconds', type: 'duration', seconds: 10 },
    { text: '30 seconds', type: 'duration', seconds: 30 },
    { text: '1 minute',   type: 'duration', seconds: 60 },
    { text: '2 minutes',  type: 'duration', seconds: 120 },
    { text: '5 minutes',  type: 'duration', seconds: 300 },
    { text: '10 minutes', type: 'duration', seconds: 600 },
    { text: '15 minutes', type: 'duration', seconds: 900 },
    { text: '30 minutes', type: 'duration', seconds: 1800 },
    { text: '1 hour',     type: 'duration', seconds: 3600 },
];

// ============================================================================
// CONTEXT DETECTION — selections are the PRIMARY signal
// ============================================================================

function _getContext(text) {
    const lower = text.toLowerCase().trim();
    if (!lower) return 'start';

    // ── SELECTIONS are the most reliable signal ──
    // What the user explicitly clicked/tabbed tells us where they are
    if (_selections.length) {
        const last = _selections[_selections.length - 1];

        // After selecting a device → show conditions/attributes for THAT device
        if (last.type === 'device') return 'condition';

        // After selecting a condition → show connectors (then/at/else/and)
        if (last.type === 'condition' || last.type === 'attribute') return 'connector';

        // After selecting an action → show target devices (actuators)
        if (last.type === 'action') return 'target_device';

        // After selecting a time → show connectors
        if (last.type === 'time_val') return 'connector';

        // After selecting a duration → show connectors or actions
        if (last.type === 'duration') return 'connector';

        // After selecting a connector keyword → depends which one
        if (last.type === 'connector') {
            const kw = last.text.toLowerCase();
            if (kw === 'then' || kw === 'else' || kw === 'otherwise') return 'action';
            if (kw === 'and') return 'device';   // another trigger device
            return 'action';
        }

        // After selecting a trigger keyword → device
        if (last.type === 'trigger') return 'device';

        // After selecting a time keyword → time values
        if (last.type === 'time') return 'time';

        // After selecting a delay keyword → duration values
        if (last.type === 'delay') return 'duration';

        // After selecting a prereq keyword → device
        if (last.type === 'prereq') return 'device';
    }

    // ── NO selections yet — everything typed is just a search query ──
    // Don't interpret typed keywords as committed selections.
    // The user needs to CLICK/TAB a suggestion to advance context.
    return 'start';
}

// ============================================================================
// FUZZY SEARCH
// ============================================================================

function _fuzzyScore(query, text) {
    const q = query.toLowerCase();
    const t = text.toLowerCase();

    if (t === q) return 100;
    if (t.startsWith(q)) return 90;
    if (t.includes(q)) return 70;

    const tWords = t.split(/[\s\-_]+/);
    for (const w of tWords) {
        if (w.startsWith(q)) return 80;
        if (w.includes(q)) return 60;
    }

    const qWords = q.split(/\s+/);
    if (qWords.length > 1) {
        const allMatch = qWords.every(qw => tWords.some(tw => tw.includes(qw)));
        if (allMatch) return 65;
    }

    return 0;
}

// ============================================================================
// MAIN SUGGEST FUNCTION
// ============================================================================

export function suggest(text) {
    const context = _getContext(text);
    const query = _getCurrentQuery(text);
    let candidates = [];

    // ── Context-driven: show the RIGHT pool, not everything ──

    if (context === 'start') {
        // Starting: show triggers, actions, AND devices — user might start any way
        // Keywords + actions (always show if no query, or if matching)
        [...KEYWORDS.filter(k => k.type === 'trigger'), ...ACTIONS].forEach(item => {
            const score = query ? _fuzzyScore(query, item.text) : 40;
            if (score > 0 || !query) candidates.push({ ...item, score: score + 10 });
        });
        // Devices too — user might start with a device name
        _allDevices.forEach(d => {
            const score = query ? _fuzzyScore(query, d.friendly_name) : 0;
            if (score > 0) {
                candidates.push({
                    text: d.friendly_name, type: 'device',
                    hint: d.model || '', ieee: d.ieee,
                    isGroup: d._is_group || false, score,
                });
            }
        });
        // Connectors too in case they type "then" etc
        if (query) {
            [...KEYWORDS, ...CONDITIONS, ...TIMES, ...DURATIONS].forEach(item => {
                const score = _fuzzyScore(query, item.text);
                if (score > 0) candidates.push({ ...item, score });
            });
        }

    } else if (context === 'device') {
        // After when/if → show ALL devices (sensors + actuators)
        _allDevices.forEach(d => {
            const score = query ? _fuzzyScore(query, d.friendly_name) : 50;
            if (score > 0 || !query) {
                candidates.push({
                    text: d.friendly_name, type: 'device',
                    hint: d.model || '', ieee: d.ieee,
                    isGroup: d._is_group || false, score,
                });
            }
        });

    } else if (context === 'target_device') {
        // After action → show only actuators
        _actuators.forEach(d => {
            const score = query ? _fuzzyScore(query, d.friendly_name) : 50;
            if (score > 0 || !query) {
                candidates.push({
                    text: d.friendly_name, type: 'device',
                    hint: d.model || '', ieee: d.ieee,
                    isGroup: d._is_group || false, score,
                });
            }
        });

    } else if (context === 'condition') {
        // After device → show conditions + that device's attributes
        const lastDevice = _selections.filter(s => s.type === 'device').pop();
        const attrs = lastDevice ? (_deviceAttrs[lastDevice.data?.ieee] || []) : [];

        // Device-specific attributes first
        attrs.forEach(a => {
            const score = query ? _fuzzyScore(query, a.attribute) : 60;
            if (score > 0 || !query) {
                candidates.push({
                    text: a.attribute, type: 'attribute',
                    hint: `current: ${a.current_value}`, score: score + 20,
                });
            }
        });

        // Then generic condition phrases
        CONDITIONS.forEach(c => {
            const score = query ? _fuzzyScore(query, c.text) : 50;
            if (score > 0 || !query) {
                candidates.push({ ...c, score });
            }
        });

    } else if (context === 'connector') {
        // After condition/time → show connectors
        KEYWORDS.filter(k => ['connector', 'time', 'delay', 'prereq'].includes(k.type)).forEach(k => {
            const score = query ? _fuzzyScore(query, k.text) : 50;
            if (score > 0 || !query) {
                candidates.push({ ...k, score });
            }
        });

    } else if (context === 'action') {
        // After "then"/"else" → show actions
        ACTIONS.forEach(a => {
            const score = query ? _fuzzyScore(query, a.text) : 50;
            if (score > 0 || !query) {
                candidates.push({ ...a, score });
            }
        });

    } else if (context === 'time') {
        // After "at"/"between" → show time values
        TIMES.forEach(t => {
            const score = query ? _fuzzyScore(query, t.text) : 50;
            if (score > 0 || !query) {
                candidates.push({ ...t, score });
            }
        });

    } else if (context === 'duration') {
        // After "after"/"wait" → show durations
        DURATIONS.forEach(d => {
            const score = query ? _fuzzyScore(query, d.text) : 50;
            if (score > 0 || !query) {
                candidates.push({ ...d, score });
            }
        });

    } else {
        // 'any' — search everything, rank by query match
        _allDevices.forEach(d => {
            const score = query ? _fuzzyScore(query, d.friendly_name) : 1;
            if (score > 0) candidates.push({
                text: d.friendly_name, type: 'device',
                hint: d.model || '', ieee: d.ieee,
                isGroup: d._is_group || false, score,
            });
        });
        [...KEYWORDS, ...ACTIONS, ...CONDITIONS, ...TIMES, ...DURATIONS].forEach(item => {
            const score = query ? _fuzzyScore(query, item.text) : 1;
            if (score > 0) candidates.push({ ...item, score });
        });
    }

    // Sort by score desc
    candidates.sort((a, b) => b.score - a.score || a.text.localeCompare(b.text));

    return {
        suggestions: candidates.slice(0, 12),
        context,
        query,
        hint: _getHint(context),
    };
}

function _getCurrentQuery(text) {
    // With chip-based input, the text IS the current query
    // (selections are tracked separately as chips)
    return (text || '').trim();
}

function _getHint(context) {
    const hints = {
        'start':         'Start with "when", "if", "turn on", or a device name',
        'device':        'Type a device or group name...',
        'condition':     '"is on", "detects motion", "is above 25", or an attribute name',
        'connector':     '"then", "at", "between", "only if", "after", "else"',
        'action':        '"turn on", "turn off", "toggle", "set brightness"',
        'target_device': 'Which device or group to control?',
        'time':          '"9am", "21:00", "sunset", "midnight"',
        'duration':      '"5 minutes", "30 seconds", "1 hour"',
        'any':           'Continue typing — devices, actions, conditions all match',
    };
    return hints[context] || hints['any'];
}

// ============================================================================
// SELECTION TRACKING
// ============================================================================

export function recordSelection(type, text, data) {
    _selections.push({ type, text, data, at: Date.now() });
    if (_selections.length > 10) _selections.shift();
}

export function getSelections() {
    return [..._selections];
}

export function removeSelection(index) {
    if (index >= 0 && index < _selections.length) {
        _selections.splice(index, 1);
    }
}

export function clearSelections() {
    _selections = [];
}

// ============================================================================
// RULE BUILDER — from selections + text
// ============================================================================

export function buildRule(text) {
    const rule = {
        name: '',
        source_ieee: null,
        conditions: [],
        prerequisites: [],
        then_sequence: [],
        else_sequence: [],
        cooldown: 5,
    };

    let inElse = false;
    let pendingAction = null;

    for (const sel of _selections) {
        switch (sel.type) {
            case 'device':
                if (pendingAction) {
                    // Device after action → target
                    const step = {
                        type: 'command',
                        command: pendingAction,
                        target_ieee: sel.data.ieee,
                        value: null,
                        endpoint_id: null,
                    };
                    if (inElse) rule.else_sequence.push(step);
                    else rule.then_sequence.push(step);
                    pendingAction = null;
                } else if (!rule.source_ieee) {
                    // First device → source
                    rule.source_ieee = sel.data.ieee;
                }
                break;

            case 'action':
                pendingAction = sel.data?.command || sel.text;
                break;

            case 'condition':
                rule.conditions.push({
                    attribute: sel.data?.attr || 'state',
                    operator: sel.data?.op || 'eq',
                    value: sel.data?.val !== undefined ? sel.data.val : 'ON',
                });
                break;

            case 'time_val':
                rule.prerequisites.push({
                    type: 'time_window',
                    time_from: sel.data?.time || '00:00',
                    time_to: _addMinutes(sel.data?.time || '00:00', 2),
                });
                break;

            case 'duration':
                const seq = inElse ? rule.else_sequence : rule.then_sequence;
                seq.push({ type: 'delay', seconds: sel.data?.seconds || 60 });
                break;

            case 'connector':
                if (sel.text === 'else' || sel.text === 'otherwise') inElse = true;
                break;
        }
    }

    // If we have actions but no source, use the first target as source
    if (!rule.source_ieee && rule.then_sequence.length) {
        rule.source_ieee = rule.then_sequence[0].target_ieee;
    }

    // Default condition if none specified
    if (!rule.conditions.length && rule.source_ieee) {
        rule.conditions.push({ attribute: 'state', operator: 'eq', value: 'ON' });
    }

    // Generate name
    rule.name = text.trim().substring(0, 60);

    return rule;
}

export function isComplete() {
    const hasSource = _selections.some(s => s.type === 'device');
    const hasAction = _selections.some(s => s.type === 'action');
    const hasTarget = _selections.filter(s => s.type === 'device').length >= 2 ||
                      (_selections.some(s => s.type === 'action') && _selections.some(s => s.type === 'device'));
    return hasSource && hasAction;
}

function _addMinutes(timeStr, mins) {
    const [h, m] = timeStr.split(':').map(Number);
    const total = h * 60 + m + mins;
    return `${Math.floor(total / 60).toString().padStart(2, '0')}:${(total % 60).toString().padStart(2, '0')}`;
}