/* ============================================================================
   Setup Wizard — Coordinator Auto-Detection Frontend
   ============================================================================
   Drop-in file: static/js/setup-wizard.js
   Load in index.html AFTER main.js (needs WebSocket manager)

   Listens for WebSocket events: "setup_scan_progress"
   API endpoints:
     GET  /api/setup/status
     GET  /api/setup/ports
     POST /api/setup/scan
     POST /api/setup/apply
     POST /api/setup/skip
   ============================================================================ */

(function () {
    'use strict';

    // ── State ────────────────────────────────────────────────────────────
    let wizardVisible = false;
    let scanning = false;
    let selectedResult = null;
    let scanResults = [];

    // ── DOM references (created lazily) ──────────────────────────────────
    let overlay = null;

    // ── Adapter family → icon / color mapping ────────────────────────────
    const ADAPTER_ICONS = {
        'Silicon Labs EZSP (Ember)':             { icon: 'fas fa-microchip',     color: 'primary' },
        'Silicon Labs CPC Multi-PAN (RCP)':      { icon: 'fas fa-project-diagram', color: 'info' },
        'Dresden Elektronik ConBee/RaspBee':     { icon: 'fas fa-wifi',          color: 'success' },
        'Texas Instruments Z-Stack':             { icon: 'fas fa-broadcast-tower', color: 'warning' },
        'Unknown Zigbee-like device':            { icon: 'fas fa-question-circle', color: 'secondary' },
    };

    // ── Build the overlay HTML ───────────────────────────────────────────

    function createOverlay() {
        if (document.getElementById('setupWizardOverlay')) {
            overlay = document.getElementById('setupWizardOverlay');
            return;
        }

        overlay = document.createElement('div');
        overlay.id = 'setupWizardOverlay';
        overlay.innerHTML = `
            <div class="setup-wizard-card" id="setupWizardCard">
                <div id="setupWizardContent"></div>
            </div>
        `;
        document.body.appendChild(overlay);
    }

    function getContent() {
        return document.getElementById('setupWizardContent');
    }

    // ── Show / Hide ──────────────────────────────────────────────────────

    function show() {
        createOverlay();
        requestAnimationFrame(() => overlay.classList.add('active'));
        wizardVisible = true;
    }

    function hide() {
        if (overlay) overlay.classList.remove('active');
        wizardVisible = false;
    }

    // ── Render: Welcome / pre-scan ───────────────────────────────────────

    function renderWelcome(reason, currentPort) {
        const el = getContent();

        let reasonText = '';
        if (reason === 'no_config') {
            reasonText = 'No configuration file found. Let\'s set up your Zigbee coordinator.';
        } else if (reason === 'no_port_configured') {
            reasonText = 'No serial port is configured for the Zigbee coordinator.';
        } else if (reason === 'port_missing') {
            reasonText = `The configured port <code>${currentPort}</code> is not present. The adapter may have been unplugged or the port changed.`;
        }

        el.innerHTML = `
            <h4><i class="fas fa-hat-wizard me-2 text-primary"></i>Coordinator Setup</h4>
            <p class="subtitle">${reasonText}</p>

            <div class="mb-3">
                <div class="d-flex align-items-center mb-2">
                    <i class="fas fa-usb me-2 text-muted"></i>
                    <span class="fw-semibold small">Detected Serial Ports</span>
                    <button class="btn btn-sm btn-link ms-auto p-0" onclick="window._setupWizard.refreshPorts()" title="Refresh">
                        <i class="fas fa-sync-alt"></i>
                    </button>
                </div>
                <div id="setupPortList" class="border rounded" style="max-height:150px; overflow-y:auto;">
                    <div class="text-center py-3 text-muted small">
                        <span class="scan-spinner"></span> Checking ports...
                    </div>
                </div>
            </div>

            <div class="setup-actions">
                <button class="btn btn-outline-secondary btn-sm" onclick="window._setupWizard.skip()">
                    Configure Manually
                </button>
                <button class="btn btn-primary" onclick="window._setupWizard.startScan()" id="setupScanBtn">
                    <i class="fas fa-search me-1"></i> Auto-Detect Coordinator
                </button>
            </div>
        `;

        // Load ports
        refreshPorts();
    }

    async function refreshPorts() {
        const el = document.getElementById('setupPortList');
        if (!el) return;

        try {
            const res = await fetch('/api/setup/ports');
            const data = await res.json();
            const ports = data.ports || [];

            if (ports.length === 0) {
                el.innerHTML = `
                    <div class="text-center py-3 text-muted small">
                        <i class="fas fa-exclamation-triangle text-warning me-1"></i>
                        No serial ports detected. Plug in your Zigbee adapter.
                    </div>
                `;
                return;
            }

            el.innerHTML = ports.map(p => `
                <div class="port-list-item">
                    <span class="port-name">${p.port}</span>
                    <span class="port-desc">
                        ${p.manufacturer ? p.manufacturer + ' ' : ''}${p.product || p.description}
                        ${p.vid ? `<span class="text-muted ms-1">[${p.vid}:${p.pid}]</span>` : ''}
                    </span>
                </div>
            `).join('');
        } catch (e) {
            el.innerHTML = `<div class="text-center py-3 text-danger small">Failed to list ports: ${e.message}</div>`;
        }
    }

    // ── Render: Scanning ─────────────────────────────────────────────────

    function renderScanning() {
        const el = getContent();
        el.innerHTML = `
            <h4><i class="fas fa-satellite-dish me-2 text-primary"></i>Scanning...</h4>
            <p class="subtitle">Probing serial ports for Zigbee coordinators. This may take 30–60 seconds.</p>

            <div class="scan-progress-bar indeterminate" id="setupProgressBar">
                <div class="bar-fill" id="setupProgressFill"></div>
            </div>

            <div class="scan-log" id="setupScanLog">
                <div class="log-entry info">Starting scan...</div>
            </div>

            <div class="setup-actions">
                <button class="btn btn-outline-danger btn-sm" onclick="window._setupWizard.cancelScan()" disabled>
                    Cancel
                </button>
            </div>
        `;
    }

    function appendLog(message, type = 'info') {
        const log = document.getElementById('setupScanLog');
        if (!log) return;

        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        entry.textContent = message;
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    function updateProgress(pct) {
        const bar = document.getElementById('setupProgressBar');
        const fill = document.getElementById('setupProgressFill');
        if (!bar || !fill) return;

        if (pct > 0) {
            bar.classList.remove('indeterminate');
            fill.style.width = pct + '%';
        }
    }

    // ── Render: Results ──────────────────────────────────────────────────

    function renderResults(results) {
        const el = getContent();
        scanResults = results;
        selectedResult = null;

        const zigbee = results.filter(r => r.adapter_family !== 'Non-Zigbee serial device');
        const nonZigbee = results.filter(r => r.adapter_family === 'Non-Zigbee serial device');

        if (zigbee.length === 0) {
            el.innerHTML = `
                <h4><i class="fas fa-hat-wizard me-2 text-primary"></i>Scan Complete</h4>

                <div class="no-adapters">
                    <i class="fas fa-exclamation-triangle d-block"></i>
                    <h5>No Zigbee Adapters Found</h5>
                    <p class="text-muted small mb-3">
                        Make sure your Zigbee USB coordinator is plugged in and recognised by the OS.
                        ${nonZigbee.length > 0 ? `<br>${nonZigbee.length} non-Zigbee serial device(s) were skipped.` : ''}
                    </p>
                </div>

                <div class="setup-actions">
                    <button class="btn btn-outline-secondary btn-sm" onclick="window._setupWizard.skip()">
                        Configure Manually
                    </button>
                    <button class="btn btn-primary" onclick="window._setupWizard.startScan()">
                        <i class="fas fa-redo me-1"></i> Scan Again
                    </button>
                </div>
            `;
            return;
        }

        // Auto-select if only one result
        if (zigbee.length === 1) {
            selectedResult = zigbee[0];
        }

        el.innerHTML = `
            <h4><i class="fas fa-check-circle me-2 text-success"></i>Adapter${zigbee.length > 1 ? 's' : ''} Found</h4>
            <p class="subtitle">
                ${zigbee.length === 1
                    ? 'Your Zigbee coordinator has been detected. Review the details and apply.'
                    : 'Multiple adapters detected. Select the one you want to use.'}
            </p>

            <div id="setupResultsList">
                ${zigbee.map((r, i) => renderAdapterCard(r, i)).join('')}
            </div>

            ${nonZigbee.length > 0 ? `
                <div class="text-muted small mt-2 mb-2">
                    <i class="fas fa-info-circle me-1"></i>
                    ${nonZigbee.length} non-Zigbee serial device(s) skipped
                </div>
            ` : ''}

            <div class="setup-actions">
                <button class="btn btn-outline-secondary btn-sm" onclick="window._setupWizard.startScan()">
                    <i class="fas fa-redo me-1"></i> Rescan
                </button>
                <button class="btn btn-outline-secondary btn-sm" onclick="window._setupWizard.skip()">
                    Configure Manually
                </button>
                <button class="btn btn-success" id="setupApplyBtn" onclick="window._setupWizard.apply()"
                        ${selectedResult ? '' : 'disabled'}>
                    <i class="fas fa-check me-1"></i> Apply &amp; Start
                </button>
            </div>
        `;

        // If auto-selected, mark it
        if (selectedResult) {
            const card = document.querySelector('.adapter-result');
            if (card) card.classList.add('selected');
        }
    }

    function renderAdapterCard(result, index) {
        const info = ADAPTER_ICONS[result.adapter_family] || { icon: 'fas fa-microchip', color: 'secondary' };
        const isSelected = selectedResult && selectedResult.port === result.port;

        const details = [];
        if (result.board_name) details.push(result.board_name);
        if (result.firmware_version) details.push(result.firmware_version);
        if (result.stack_version) details.push(result.stack_version);
        if (result.eui64) details.push(`EUI: ${result.eui64}`);

        const baud = result.baud_rate ? `${result.baud_rate} baud` : '';
        const flow = result.flow_control && result.flow_control !== 'none'
            ? `, ${result.flow_control.toUpperCase()} flow` : '';

        return `
            <div class="adapter-result ${isSelected ? 'selected' : ''}"
                 onclick="window._setupWizard.selectResult(${index})" data-index="${index}">
                <div class="d-flex align-items-start">
                    <div class="adapter-icon text-${info.color}">
                        <i class="${info.icon}"></i>
                    </div>
                    <div class="flex-grow-1">
                        <div class="d-flex align-items-center gap-2 flex-wrap">
                            <span class="adapter-name">${result.adapter_family}</span>
                            <span class="badge bg-${info.color} adapter-badge">${result.hardware_id || ''}</span>
                        </div>
                        <div class="adapter-port mt-1">${result.port}</div>
                        <div class="adapter-detail mt-1">
                            ${details.join(' · ')}
                            ${baud ? `<br>${baud}${flow}` : ''}
                        </div>

                        ${Object.keys(result.extra || {}).length > 0 ? `
                            <div class="adapter-detail mt-1">
                                ${Object.entries(result.extra).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                            </div>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;
    }

    // ── Render: Applying ─────────────────────────────────────────────────

    function renderApplying() {
        const el = getContent();
        el.innerHTML = `
            <h4><i class="fas fa-cog fa-spin me-2 text-primary"></i>Applying Configuration...</h4>
            <p class="subtitle">Writing coordinator settings to config.yaml and preparing to start.</p>
        `;
    }

    function renderApplySuccess(config) {
        const el = getContent();
        el.innerHTML = `
            <h4><i class="fas fa-check-circle me-2 text-success"></i>Setup Complete</h4>
            <p class="subtitle">
                Your Zigbee coordinator is configured on <code>${config.port || selectedResult?.port}</code>.
                The application will now start the Zigbee network.
            </p>

            <div class="setup-actions">
                <button class="btn btn-success" onclick="window._setupWizard.finish()">
                    <i class="fas fa-play me-1"></i> Start Application
                </button>
            </div>
        `;
    }

    function renderApplyError(error) {
        const el = getContent();
        el.innerHTML = `
            <h4><i class="fas fa-exclamation-circle me-2 text-danger"></i>Configuration Error</h4>
            <p class="subtitle text-danger">${error}</p>

            <div class="setup-actions">
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.startScan()">
                    <i class="fas fa-redo me-1"></i> Try Again
                </button>
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.skip()">
                    Configure Manually
                </button>
            </div>
        `;
    }

    // ── Actions ──────────────────────────────────────────────────────────

    async function startScan() {
        if (scanning) return;
        scanning = true;
        scanResults = [];
        selectedResult = null;

        renderScanning();

        try {
            const res = await fetch('/api/setup/scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            const data = await res.json();
            if (!data.success) {
                appendLog('Failed to start scan: ' + (data.detail || 'unknown error'), 'error');
                scanning = false;
            }
            // Progress events come via WebSocket — handled by onScanProgress()
        } catch (e) {
            appendLog('Scan request failed: ' + e.message, 'error');
            scanning = false;
        }
    }

    function cancelScan() {
        // We can't abort the serial I/O mid-scan, but we can dismiss the UI
        scanning = false;
        renderWelcome('no_port_configured', '');
    }

    function selectResult(index) {
        const zigbee = scanResults.filter(r => r.adapter_family !== 'Non-Zigbee serial device');
        if (index < 0 || index >= zigbee.length) return;

        selectedResult = zigbee[index];

        // Update selection UI
        document.querySelectorAll('.adapter-result').forEach((el, i) => {
            el.classList.toggle('selected', i === index);
        });

        const btn = document.getElementById('setupApplyBtn');
        if (btn) btn.disabled = false;
    }

    async function apply() {
        if (!selectedResult) return;

        renderApplying();

        try {
            // Step 1: Write config
            const res = await fetch('/api/setup/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(selectedResult),
            });
            const data = await res.json();

            if (!data.success) {
                renderApplyError(data.detail || data.error || 'Unknown error');
                return;
            }

            // Step 2: Start Zigbee with new config
            appendLog('Starting Zigbee network...', 'info');
            const startRes = await fetch('/api/setup/start-zigbee', { method: 'POST' });
            const startData = await startRes.json();

            if (startData.success) {
                renderApplySuccess(data.config || {});
            } else {
                renderApplyError('Config saved but Zigbee failed to start: ' + startData.error);
            }
        } catch (e) {
            renderApplyError(e.message);
        }
    }

    async function skip() {
        try {
            await fetch('/api/setup/skip', { method: 'POST' });
        } catch (e) {
            // Ignore — skipping is best-effort
        }
        hide();
    }

    function finish() {
        // Reload the page to pick up the new config and start normally
        hide();
        window.location.reload();
    }

    // ── WebSocket handler ────────────────────────────────────────────────

    function onScanProgress(payload) {
        if (!wizardVisible || !scanning) return;

        const { phase, message, progress_pct, detail } = payload;

        // Update progress bar
        if (progress_pct) updateProgress(progress_pct);

        // Append to log
        const type = phase === 'complete' ? (detail?.error ? 'error' : 'success')
                   : message.includes('✅') ? 'success'
                   : message.includes('Error') || message.includes('failed') ? 'error'
                   : 'info';
        appendLog(message, type);

        // Scan complete?
        if (phase === 'complete') {
            scanning = false;
            const results = detail?.results || [];
            if (results.length > 0) {
                renderResults(results);
            } else if (detail?.error === 'no_ports') {
                renderResults([]);
            } else {
                renderResults(results);
            }
        }
    }

    // ── Init: check setup status on page load ────────────────────────────

    async function init() {
        try {
            const res = await fetch('/api/setup/status');
            const data = await res.json();

            if (data.needs_setup && !data.skipped) {
                show();
                renderWelcome(data.reason, data.current_port);
            }
        } catch (e) {
            // API not available yet — app may still be starting
            console.debug('Setup wizard: status check failed, skipping', e);
        }
    }

    // ── Public API (attached to window for onclick handlers) ─────────────

    window._setupWizard = {
        init,
        show,
        hide,
        startScan,
        cancelScan,
        refreshPorts,
        selectResult,
        apply,
        skip,
        finish,
        onScanProgress,
    };

    // ── Hook into the app's WebSocket message handler ────────────────────
    // The main.js WebSocket dispatches messages by type.
    // We register for "setup_scan_progress" events.

    const _origWsHandler = window._onWsMessage;

    window._onWsMessage = function (data) {
        if (data.type === 'setup_scan_progress') {
            onScanProgress(data.payload);
            return;
        }
        // Pass through to original handler
        if (typeof _origWsHandler === 'function') {
            _origWsHandler(data);
        }
    };

    // Auto-init on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        // Small delay to let main.js initialise first
        setTimeout(init, 500);
    }

})();