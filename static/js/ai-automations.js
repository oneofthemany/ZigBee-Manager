/**
 * AI Automations - Natural Language Rule Generation
 * Location: static/js/ai-automations.js
 *
 * Adds an "AI Assist" panel to the global Automations tab.
 * User types natural language, AI generates a rule, user reviews
 * the pre-filled form, then saves via the existing automation API.
 */

import { initAutomationTab } from './modal/automation.js';

let _aiConfigured = false;
let _deviceNames = [];

// ============================================================================
// INIT — called once from automations-page.js
// ============================================================================

export async function initAIAutomations() {
    try {
        const [statusRes] = await Promise.all([
            fetch('/api/ai/status'),
        ]);
        const statusData = await statusRes.json();
        _aiConfigured = statusData.configured;
    } catch {
        _aiConfigured = false;
    }

    // Load NLP registry (devices, attributes, actuators)
    try {
        const { loadRegistry } = await import('./nlp-automation.js');
        await loadRegistry();
    } catch (e) {
        console.warn('NLP registry load failed:', e);
    }
}

export function isAIConfigured() { return _aiConfigured; }

// ============================================================================
// RENDER — returns the AI panel HTML for injection into automations page
// ============================================================================

export function renderAIPanel() {
    return `
    <div id="ai-auto-panel" class="card mb-3 border-info">
        <div class="card-header bg-info bg-opacity-10 d-flex justify-content-between align-items-center py-2">
            <strong><i class="fas fa-brain me-1"></i> AI Automation Builder</strong>
            <div class="d-flex gap-2 align-items-center">
                <span id="ai-status-badge" class="badge ${_aiConfigured ? 'bg-success' : 'bg-warning text-dark'} small">
                    ${_aiConfigured ? 'Connected' : 'Not Configured'}
                </span>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._aiToggleSettings()"
                        title="AI Settings"><i class="fas fa-cog"></i></button>
            </div>
        </div>
        <div class="card-body">
            <!-- Settings (hidden by default) -->
            <div id="ai-settings" style="display:none" class="mb-3 p-2 bg-light rounded small">
                <div id="ai-settings-alert"></div>
                <div class="row g-2 mb-2">
                    <div class="col-md-3">
                        <label class="form-label small mb-0">Provider</label>
                        <select class="form-select form-select-sm" id="ai-provider" onchange="window._aiProviderChanged(this.value)">
                            <option value="ollama">Ollama (local)</option>
                            <option value="openai">OpenAI</option>
                            <option value="anthropic">Anthropic</option>
                            <option value="custom">Custom</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small mb-0">Model</label>
                        <input type="text" class="form-control form-control-sm" id="ai-model" placeholder="e.g. llama3.1:8b-instruct-q4_K_M">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small mb-0">API Key <span class="text-muted" id="ai-key-hint">(not required for Ollama)</span></label>
                        <div class="input-group input-group-sm">
                            <input type="password" class="form-control form-control-sm" id="ai-apikey" placeholder="Leave blank for Ollama">
                            <span class="input-group-text" id="ai-key-badge"></span>
                        </div>
                    </div>
                    <div class="col-md-2 d-flex align-items-end">
                        <button class="btn btn-sm btn-primary w-100" onclick="window._aiSaveSettings()">
                            <i class="fas fa-save me-1"></i>Save
                        </button>
                    </div>
                </div>
                <div class="row g-2">
                    <div class="col-md-5">
                        <label class="form-label small mb-0">Base URL <span class="text-muted">(auto-detected per provider)</span></label>
                        <input type="text" class="form-control form-control-sm" id="ai-baseurl" placeholder="http://localhost:11434/v1">
                    </div>
                    <div class="col-md-2">
                        <label class="form-label small mb-0">Temperature</label>
                        <input type="number" class="form-control form-control-sm" id="ai-temp" value="0.3" min="0" max="2" step="0.1">
                    </div>
                    <div class="col-md-2">
                        <label class="form-label small mb-0">Max Tokens</label>
                        <input type="number" class="form-control form-control-sm" id="ai-maxtokens" value="2000" min="500" max="8000" step="100">
                    </div>
                    <div class="col-md-3 d-flex align-items-end">
                        <button class="btn btn-sm btn-outline-info w-100" onclick="window._aiTestConnection()">
                            <i class="fas fa-plug me-1"></i>Test Connection
                        </button>
                    </div>
                </div>
            </div>

            <!-- Chip-based automation builder -->
            <div style="position:relative">
                <div id="ai-builder" class="form-control d-flex flex-wrap align-items-center gap-1 p-2" 
                     style="min-height:44px;cursor:text;position:relative"
                     onclick="document.getElementById('ai-typed').focus()">
                    <div id="ai-chips" class="d-flex flex-wrap align-items-center gap-1"></div>
                    <input type="text" id="ai-typed" class="border-0 flex-grow-1" 
                           style="outline:none;min-width:120px;font-size:0.9rem;background:transparent;color:inherit"
                           placeholder="Start with 'when', 'if', 'turn on', or a device name..."
                           onkeydown="window._aiPromptKey(event)"
                           oninput="window._aiAutocomplete(this.value)"
                           autocomplete="off">
                </div>
                <div class="d-flex gap-1 mt-1">
                    <button class="btn btn-sm btn-success text-white flex-grow-1" onclick="window._aiGenerate()" id="ai-gen-btn"
                            title="Build rule from your selections (instant, no LLM)">
                        <i class="fas fa-bolt me-1"></i> Build Rule
                    </button>
                    <button class="btn btn-sm btn-outline-info" onclick="window._aiGenerateLLM()" id="ai-llm-btn"
                            title="Send to LLM for complex rules (slower, requires Ollama)">
                        <i class="fas fa-brain me-1"></i> AI
                    </button>
                    <button class="btn btn-sm btn-outline-secondary" onclick="window._aiClearBuilder()"
                            title="Clear all">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                <div id="ai-autocomplete" class="list-group position-absolute w-100 shadow-sm" style="z-index:1050;max-height:220px;overflow-y:auto;display:none"></div>
            </div>
            <div id="ai-progress" class="mt-1" style="display:none"></div>

            <!-- Result area -->
            <div id="ai-result" class="mt-2" style="display:none"></div>
        </div>
    </div>`;
}

// ============================================================================
// GENERATE
// ============================================================================

async function _aiGenerate() {
    const btn = document.getElementById('ai-gen-btn');
    const result = document.getElementById('ai-result');
    const progress = document.getElementById('ai-progress');
    const typed = document.getElementById('ai-typed');

    // Build prompt from chips + typed text
    let prompt = '';
    try {
        const { getSelections } = await import('./nlp-automation.js');
        prompt = getSelections().map(s => s.text).join(' ');
    } catch (e) {}
    if (typed?.value) prompt += (prompt ? ' ' : '') + typed.value.trim();
    if (!prompt) return;

    // Hide autocomplete
    _aiHideAutocomplete();

    // Show loading with countdown
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Generating...';
    result.style.display = 'block';
    result.innerHTML = '<div class="text-muted small"><i class="fas fa-spinner fa-spin"></i> Analysing your request and generating automation rule...</div>';

    // Progress timer (ARM inference can take 30-120s)
    let elapsed = 0;
    if (progress) {
        progress.style.display = 'block';
        progress.innerHTML = `<div class="progress" style="height:3px"><div id="ai-prog-bar" class="progress-bar progress-bar-striped progress-bar-animated bg-info" style="width:0%"></div></div>
            <div class="text-muted small mt-1" id="ai-prog-text">LLM processing on device — this may take up to 2 minutes on ARM...</div>`;
    }
    const progTimer = setInterval(() => {
        elapsed++;
        const bar = document.getElementById('ai-prog-bar');
        const text = document.getElementById('ai-prog-text');
        if (bar) bar.style.width = `${Math.min(elapsed / 120 * 100, 95)}%`;
        if (text) {
            if (elapsed < 10) text.textContent = 'Sending request to LLM...';
            else if (elapsed < 30) text.textContent = `Model is processing (${elapsed}s)...`;
            else if (elapsed < 60) text.textContent = `Still generating — ARM inference is slower than x86 (${elapsed}s)...`;
            else text.textContent = `Nearly there — complex rules take longer (${elapsed}s)...`;
        }
    }, 1000);

    try {
        // Use AbortController for 180s timeout
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 180000);

        const res = await fetch('/api/ai/automation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
            signal: controller.signal,
        });
        clearTimeout(timeout);
        const data = await res.json();

        if (data.success) {
            _showGeneratedRule(data.rule, data.explanation);
        } else {
            result.innerHTML = `<div class="alert alert-warning mb-0 py-2 small">
                <i class="fas fa-exclamation-triangle me-1"></i> ${data.error || 'Failed to generate rule'}
                ${data.raw_response ? `<details class="mt-1"><summary>Raw response</summary><pre class="mb-0 small">${_esc(data.raw_response)}</pre></details>` : ''}
            </div>`;
        }
    } catch (e) {
        const msg = e.name === 'AbortError'
            ? 'Request timed out after 3 minutes. The LLM may be overloaded — try a simpler prompt or check Ollama status.'
            : e.message;
        result.innerHTML = `<div class="alert alert-danger mb-0 py-2 small">
            <i class="fas fa-times-circle me-1"></i> ${msg}
        </div>`;
    } finally {
        clearInterval(progTimer);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-magic me-1"></i> Generate';
        if (progress) progress.style.display = 'none';
    }
}

// ============================================================================
// AUTOCOMPLETE — Context-aware fuzzy search with chip-based input
// ============================================================================

let _acSelected = -1;
let _lastResult = null;

const CHIP_COLORS = {
    device: '#3b82f6',      // blue
    action: '#22c55e',      // green
    trigger: '#8b5cf6',     // purple
    connector: '#64748b',   // slate
    condition: '#b45309',   // dark amber (better contrast)
    attribute: '#06b6d4',   // cyan
    time: '#db2777',        // dark pink (better contrast)
    time_val: '#db2777',
    duration: '#ea580c',    // dark orange (better contrast)
    delay: '#ea580c',
    prereq: '#6366f1',      // indigo
};

const CHIP_ICONS = {
    device: '💡', action: '⚡', trigger: '🔔', connector: '→',
    condition: '❓', attribute: '📊', time: '🕐', time_val: '🕐',
    duration: '⏱', delay: '⏱', prereq: '🔒',
};

function _renderChip(type, text, index) {
    const bg = CHIP_COLORS[type] || '#94a3b8';
    const icon = CHIP_ICONS[type] || '';
    return `<span class="d-inline-flex align-items-center gap-1 px-2 py-1" 
                  style="background:${bg};color:#fff;font-size:0.78rem;font-weight:500;border-radius:20px;cursor:default;white-space:nowrap;line-height:1.4"
                  title="${type}">
                ${icon} ${_esc(text)}
                <span onclick="event.stopPropagation();window._aiRemoveChip(${index})" 
                      style="cursor:pointer;opacity:0.7;margin-left:2px;font-size:0.7rem;color:#fff"
                      onmouseenter="this.style.opacity='1'" onmouseleave="this.style.opacity='0.7'">✕</span>
            </span>`;
}

function _refreshChips() {
    const container = document.getElementById('ai-chips');
    if (!container) return;
    import('./nlp-automation.js').then(m => {
        const selections = m.getSelections();
        container.innerHTML = selections.map((s, i) => _renderChip(s.type, s.text, i)).join('');
    }).catch(() => {});
}

async function _aiRemoveChip(index) {
    try {
        const { removeSelection } = await import('./nlp-automation.js');
        removeSelection(index);
    } catch (e) {}
    _refreshChips();
    document.getElementById('ai-typed')?.focus();
    // Re-trigger suggestions
    setTimeout(() => _aiAutocomplete(document.getElementById('ai-typed')?.value || ''), 50);
}

function _aiClearBuilder() {
    import('./nlp-automation.js').then(m => m.clearSelections()).catch(() => {});
    const chips = document.getElementById('ai-chips');
    const typed = document.getElementById('ai-typed');
    if (chips) chips.innerHTML = '';
    if (typed) { typed.value = ''; typed.placeholder = "Start with 'when', 'if', 'turn on', or a device name..."; }
    _aiHideAutocomplete();
    const result = document.getElementById('ai-result');
    if (result) result.style.display = 'none';
    const hintEl = document.getElementById('ai-progress');
    if (hintEl) hintEl.style.display = 'none';
}

async function _aiAutocomplete(value) {
    const dropdown = document.getElementById('ai-autocomplete');
    const hintEl = document.getElementById('ai-progress');
    if (!dropdown) return;

    // Show hint even with empty input
    try {
        const { suggest } = await import('./nlp-automation.js');
        _lastResult = suggest(value || '');
    } catch (e) {
        _aiHideAutocomplete();
        return;
    }

    const { suggestions, hint, context } = _lastResult;

    if (hintEl) {
        hintEl.style.display = 'block';
        hintEl.innerHTML = `<div class="text-muted small"><i class="fas fa-lightbulb me-1 text-warning"></i>${_esc(hint)}</div>`;
    }

    // Update placeholder based on context
    const typed = document.getElementById('ai-typed');
    if (typed) {
        const placeholders = {
            'start': "when, if, turn on...",
            'device': "Type device name...",
            'condition': "is on, is open, detects motion...",
            'connector': "then, at, else, only if...",
            'action': "turn on, turn off, toggle...",
            'target_device': "Type device to control...",
            'time': "9am, sunset, midnight...",
            'duration': "5 minutes, 30 seconds...",
        };
        typed.placeholder = placeholders[context] || "Continue typing...";
    }

    if (!suggestions.length) {
        _aiHideAutocomplete();
        return;
    }

    _acSelected = -1;

    dropdown.innerHTML = suggestions.map(s => {
        const bg = CHIP_COLORS[s.type] || '#94a3b8';
        const icon = CHIP_ICONS[s.type] || '';
        const hintText = s.hint ? `<span class="text-muted" style="font-size:0.7rem">${_esc(s.hint)}</span>` : '';
        const dataJson = encodeURIComponent(JSON.stringify(s));
        return `<button class="list-group-item list-group-item-action py-1 px-2 small d-flex justify-content-between align-items-center"
                    onclick="window._aiInsertSuggestion(decodeURIComponent('${dataJson}'))"
                    onmouseenter="this.classList.add('active')"
                    onmouseleave="this.classList.remove('active')">
                    <span>${icon} <strong>${_esc(s.text)}</strong> ${hintText}</span>
                    <span style="background:${bg};color:#fff;font-size:0.6rem;border-radius:10px;padding:2px 6px">${s.type}</span>
                </button>`;
    }).join('');
    dropdown.style.display = 'block';
}

async function _aiInsertSuggestion(jsonStr) {
    const s = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr;

    // Record the selection
    try {
        const { recordSelection } = await import('./nlp-automation.js');
        recordSelection(s.type, s.text, s);
    } catch (e) {}

    // Clear typed text and refresh chips
    const typed = document.getElementById('ai-typed');
    if (typed) typed.value = '';

    _refreshChips();
    _aiHideAutocomplete();

    // Focus back and show next suggestions
    if (typed) typed.focus();
    setTimeout(() => _aiAutocomplete(''), 100);
}

function _aiHideAutocomplete() {
    const dropdown = document.getElementById('ai-autocomplete');
    if (dropdown) dropdown.style.display = 'none';
    _acSelected = -1;
}

function _aiPromptKey(event) {
    const dropdown = document.getElementById('ai-autocomplete');
    const visible = dropdown && dropdown.style.display !== 'none';
    const items = visible ? dropdown.querySelectorAll('.list-group-item') : [];
    const typed = document.getElementById('ai-typed');

    if (event.key === 'Enter') {
        event.preventDefault();
        if (visible && _acSelected >= 0 && items[_acSelected]) {
            items[_acSelected].click();
        } else if (visible && items.length) {
            items[0].click();
        } else if (!visible) {
            _aiTryNLPOrGenerate();
        }
        return;
    }

    if (event.key === 'Tab' && visible && items.length) {
        event.preventDefault();
        items[_acSelected >= 0 ? _acSelected : 0].click();
        return;
    }

    // Backspace with empty input → remove last chip
    if (event.key === 'Backspace' && typed && !typed.value) {
        import('./nlp-automation.js').then(m => {
            const sels = m.getSelections();
            if (sels.length) {
                m.removeSelection(sels.length - 1);
                _refreshChips();
                setTimeout(() => _aiAutocomplete(''), 50);
            }
        }).catch(() => {});
        return;
    }

    if (!visible || !items.length) return;

    if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (_acSelected >= 0) items[_acSelected].classList.remove('active');
        _acSelected = Math.min(_acSelected + 1, items.length - 1);
        items[_acSelected].classList.add('active');
        items[_acSelected].scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (_acSelected >= 0) items[_acSelected].classList.remove('active');
        _acSelected = Math.max(_acSelected - 1, 0);
        items[_acSelected].classList.add('active');
        items[_acSelected].scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'Escape') {
        _aiHideAutocomplete();
    }
}

// ============================================================================
// BUILD — try NLP selections first, fall back to LLM
// ============================================================================

async function _aiTryNLPOrGenerate() {
    _aiHideAutocomplete();

    try {
        const { isComplete, buildRule, clearSelections, getSelections } = await import('./nlp-automation.js');
        const sels = getSelections();

        if (isComplete()) {
            // Build text from selections for the rule name
            const text = sels.map(s => s.text).join(' ');
            const rule = buildRule(text);
            const result = document.getElementById('ai-result');
            if (result) result.style.display = 'block';
            _showGeneratedRule(rule, 'Built locally from your selections (no LLM needed)');
            return;
        }
    } catch (e) {
        console.debug('NLP build incomplete:', e);
    }

    // Fall back to LLM — build prompt from chips + typed text
    _aiGenerate();
}

// ============================================================================
// DISPLAY GENERATED RULE
// ============================================================================

function _showGeneratedRule(rule, explanation) {
    const result = document.getElementById('ai-result');

    // Conditions summary
    const condHtml = (rule.conditions || []).map(c => {
        if (c.type === 'time_window') return `<span class="badge bg-info">🕐 ${c.time_from}-${c.time_to}</span>`;
        return `<span class="badge bg-primary">${c.attribute} ${c.operator} ${c.value}</span>`;
    }).join(' <small class="text-muted">AND</small> ');

    // Steps summary
    const stepsHtml = s => (s || []).map(step => {
        if (step.type === 'command') return `<span class="badge bg-success">${step.command}${step.value != null ? '=' + step.value : ''}</span> <small>${step.target_ieee || '?'}</small>`;
        if (step.type === 'delay') return `<span class="badge bg-warning text-dark">wait ${step.seconds}s</span>`;
        if (step.type === 'if_then_else') return `<span class="badge bg-purple" style="background:#6f42c1">IF/THEN/ELSE</span>`;
        if (step.type === 'parallel') return `<span class="badge bg-dark">parallel</span>`;
        return `<span class="badge bg-secondary">${step.type}</span>`;
    }).join(' → ');

    result.innerHTML = `
    <div class="card border-success">
        <div class="card-header bg-success bg-opacity-10 py-2 d-flex justify-content-between align-items-center">
            <strong class="small"><i class="fas fa-check-circle text-success me-1"></i> Generated Rule Preview</strong>
            <div class="d-flex gap-2">
                <button class="btn btn-sm btn-outline-success" onclick="window._aiSaveRule()">
                    <i class="fas fa-save me-1"></i> Save Directly
                </button>
                <button class="btn btn-sm btn-outline-primary" onclick="window._aiEditRule()">
                    <i class="fas fa-edit me-1"></i> Review & Edit
                </button>
            </div>
        </div>
        <div class="card-body py-2 small">
            <p class="mb-1 text-muted"><i class="fas fa-info-circle me-1"></i> ${_esc(explanation)}</p>
            <div class="mb-1"><strong>Name:</strong> ${_esc(rule.name || '(unnamed)')}</div>
            <div class="mb-1"><strong>Source:</strong> <code>${rule.source_ieee}</code></div>
            <div class="mb-1"><strong>IF:</strong> ${condHtml}</div>
            ${(rule.prerequisites || []).length ? `<div class="mb-1"><strong>CHECK:</strong> ${rule.prerequisites.map(p => {
                if (p.type === 'time_window') return `<span class="badge bg-info">🕐 ${p.time_from}-${p.time_to}</span>`;
                return `<span class="badge bg-secondary">${p.negate ? 'NOT ' : ''}${p.ieee} ${p.attribute} ${p.operator} ${p.value}</span>`;
            }).join(' ')}</div>` : ''}
            <div class="mb-1"><strong>THEN:</strong> ${stepsHtml(rule.then_sequence)}</div>
            ${(rule.else_sequence || []).length ? `<div class="mb-1"><strong>ELSE:</strong> ${stepsHtml(rule.else_sequence)}</div>` : ''}
            <details class="mt-1"><summary class="text-muted">Raw JSON</summary><pre class="mb-0 small bg-light p-2 rounded" style="max-height:200px;overflow:auto">${_esc(JSON.stringify(rule, null, 2))}</pre></details>
        </div>
    </div>`;

    // Store rule for save/edit
    window._aiGeneratedRule = rule;
}

// ============================================================================
// SAVE / EDIT ACTIONS
// ============================================================================

async function _aiSaveRule() {
    const rule = window._aiGeneratedRule;
    if (!rule) return;

    try {
        const res = await fetch('/api/automations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(rule)
        });
        const data = await res.json();

        const result = document.getElementById('ai-result');
        if (data.success) {
            result.innerHTML = `<div class="alert alert-success mb-0 py-2 small">
                <i class="fas fa-check-circle me-1"></i> Rule saved: <strong>${data.rule?.name || data.rule?.id}</strong>
            </div>`;
            // Clear the builder
            window._aiClearBuilder();
            window._aiGeneratedRule = null;
            // Refresh the rules list if the page refresh function exists
            if (typeof window._apRefresh === 'function') window._apRefresh();
        } else {
            result.innerHTML = `<div class="alert alert-danger mb-0 py-2 small">
                <i class="fas fa-times-circle me-1"></i> Save failed: ${data.error || data.detail || 'Unknown error'}
            </div>`;
        }
    } catch (e) {
        document.getElementById('ai-result').innerHTML = `<div class="alert alert-danger mb-0 py-2 small">${e.message}</div>`;
    }
}

async function _aiEditRule() {
    const rule = window._aiGeneratedRule;
    if (!rule || !rule.source_ieee) return;

    // Open the create panel with the source device pre-selected, then populate the form
    const createPanel = document.getElementById('ap-create-panel');
    const srcSelect = document.getElementById('ap-source-select');
    if (!createPanel || !srcSelect) return;

    createPanel.style.display = 'block';
    srcSelect.value = rule.source_ieee;

    // Trigger source selection to init the form
    if (typeof window._apSourceSelected === 'function') {
        await window._apSourceSelected(rule.source_ieee);
    }

    // Wait for form init then populate with AI-generated values
    setTimeout(() => {
        _populateForm(rule);
    }, 300);

    // Scroll to form
    createPanel.scrollIntoView({ behavior: 'smooth' });
}

function _populateForm(rule) {
    // Name
    const nameEl = document.getElementById('a-name');
    if (nameEl && rule.name) nameEl.value = rule.name;

    // Cooldown
    const cdEl = document.getElementById('a-cd');
    if (cdEl && rule.cooldown) cdEl.value = rule.cooldown;

    // Note: Full form population for conditions/prerequisites/steps would require
    // calling _aEdit with a synthetic rule object, which the existing form builder
    // supports. For now, we auto-open the form and set name + cooldown.
    // The user can manually adjust conditions and sequences using the visual builder.
    // A full auto-populate would call window._aEdit() with the rule data directly.
}

// ============================================================================
// SETTINGS
// ============================================================================

function _aiToggleSettings() {
    const el = document.getElementById('ai-settings');
    if (!el) return;
    const visible = el.style.display !== 'none';
    el.style.display = visible ? 'none' : 'block';

    if (!visible) _aiLoadSettings();
}

// Provider defaults — mirrors PROVIDER_DEFAULTS in ai_assistant.py
const PROVIDER_DEFAULTS = {
    ollama:    { base_url: 'http://localhost:11434/v1', model: 'llama3.1:8b-instruct-q4_K_M', requires_key: false },
    openai:    { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', requires_key: true },
    anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-20250514', requires_key: true },
    custom:    { base_url: '', model: '', requires_key: false },
};

async function _aiLoadSettings() {
    try {
        const res = await fetch('/api/ai/status');
        const data = await res.json();

        const set = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined) el.value = val; };
        set('ai-provider', data.provider || 'ollama');
        set('ai-model', data.model || '');
        set('ai-baseurl', data.base_url || '');
        set('ai-temp', data.temperature ?? 0.3);
        set('ai-maxtokens', data.max_tokens ?? 2000);

        // API key indicator
        const badge = document.getElementById('ai-key-badge');
        if (badge) {
            badge.innerHTML = data.has_api_key
                ? '<i class="fas fa-check text-success"></i>'
                : '<i class="fas fa-times text-muted"></i>';
        }

        // Update key hint based on provider
        _updateKeyHint(data.provider || 'ollama');
    } catch { /* ignore */ }
}

function _aiProviderChanged(provider) {
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;

    // Auto-fill model and base_url placeholders/values
    const modelEl = document.getElementById('ai-model');
    const urlEl = document.getElementById('ai-baseurl');
    const keyEl = document.getElementById('ai-apikey');

    if (modelEl) {
        modelEl.value = defaults.model;
        modelEl.placeholder = defaults.model || 'Model name';
    }
    if (urlEl) {
        urlEl.value = defaults.base_url;
        urlEl.placeholder = defaults.base_url || 'Base URL';
    }
    // Clear API key field when switching provider
    if (keyEl) keyEl.value = '';

    _updateKeyHint(provider);
}

function _updateKeyHint(provider) {
    const hint = document.getElementById('ai-key-hint');
    if (!hint) return;
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;
    hint.textContent = defaults.requires_key
        ? `(required for ${provider})`
        : '(not required for Ollama)';
}

async function _aiSaveSettings() {
    const get = id => document.getElementById(id)?.value?.trim() || '';
    const alert = document.getElementById('ai-settings-alert');
    const config = {};

    // Always send provider
    config.provider = get('ai-provider') || 'ollama';

    // Only send non-empty optional fields
    const model = get('ai-model');
    if (model) config.model = model;

    const baseUrl = get('ai-baseurl');
    if (baseUrl) config.base_url = baseUrl;

    const apiKey = get('ai-apikey');
    if (apiKey) config.api_key = apiKey;

    const temp = parseFloat(get('ai-temp'));
    if (!isNaN(temp)) config.temperature = temp;

    const maxTokens = parseInt(get('ai-maxtokens'));
    if (!isNaN(maxTokens)) config.max_tokens = maxTokens;

    try {
        const res = await fetch('/api/ai/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        const data = await res.json();

        if (data.success || data.configured !== undefined) {
            _aiConfigured = data.configured;

            // Update header badge
            const badge = document.getElementById('ai-status-badge');
            if (badge) {
                badge.className = `badge ${_aiConfigured ? 'bg-success' : 'bg-warning text-dark'} small`;
                badge.textContent = _aiConfigured ? 'Connected' : 'Not Configured';
            }

            // Show feedback
            if (alert) {
                alert.innerHTML = `<div class="alert alert-success py-1 mb-2 small">
                    <i class="fas fa-check-circle me-1"></i> Settings saved to config.yaml
                    — ${data.provider}/${data.model}
                </div>`;
                setTimeout(() => { alert.innerHTML = ''; }, 4000);
            }

            // Refresh key indicator
            _aiLoadSettings();
        } else {
            if (alert) alert.innerHTML = `<div class="alert alert-danger py-1 mb-2 small">
                <i class="fas fa-times-circle me-1"></i> Save failed: ${data.error || 'Unknown error'}
            </div>`;
        }
    } catch (e) {
        if (alert) alert.innerHTML = `<div class="alert alert-danger py-1 mb-2 small">${e.message}</div>`;
    }
}

async function _aiTestConnection() {
    const alert = document.getElementById('ai-settings-alert');
    if (alert) alert.innerHTML = `<div class="alert alert-info py-1 mb-2 small">
        <i class="fas fa-spinner fa-spin me-1"></i> Testing connection...
    </div>`;

    try {
        const res = await fetch('/api/ai/automation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: 'Test: respond with a JSON object containing just {"test": true}' })
        });
        const data = await res.json();

        if (data.success || data.rule) {
            if (alert) alert.innerHTML = `<div class="alert alert-success py-1 mb-2 small">
                <i class="fas fa-check-circle me-1"></i> Connection successful — LLM responded
            </div>`;
        } else {
            if (alert) alert.innerHTML = `<div class="alert alert-warning py-1 mb-2 small">
                <i class="fas fa-exclamation-triangle me-1"></i> ${data.error || 'No response from LLM'}
            </div>`;
        }
    } catch (e) {
        if (alert) alert.innerHTML = `<div class="alert alert-danger py-1 mb-2 small">
            <i class="fas fa-times-circle me-1"></i> Connection failed: ${e.message}
        </div>`;
    }
}

// ============================================================================
// UTILS
// ============================================================================

function _esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

window._aiGenerate = _aiTryNLPOrGenerate;
window._aiGenerateLLM = _aiGenerate;
window._aiSaveRule = _aiSaveRule;
window._aiEditRule = _aiEditRule;
window._aiToggleSettings = _aiToggleSettings;
window._aiSaveSettings = _aiSaveSettings;
window._aiProviderChanged = _aiProviderChanged;
window._aiTestConnection = _aiTestConnection;
window._aiAutocomplete = _aiAutocomplete;
window._aiInsertSuggestion = _aiInsertSuggestion;
window._aiPromptKey = _aiPromptKey;
window._aiRemoveChip = _aiRemoveChip;
window._aiClearBuilder = _aiClearBuilder;
window._aiGeneratedRule = null;

// Close autocomplete when clicking outside
document.addEventListener('click', (e) => {
    if (!e.target.closest('#ai-builder') && !e.target.closest('#ai-autocomplete')) {
        _aiHideAutocomplete();
    }
});