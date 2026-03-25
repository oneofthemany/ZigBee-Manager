/* ============================================================================
   Setup Wizard — Multi-Step First-Run Configuration
   ============================================================================

   Steps:
     1. Coordinator Detection  — auto-detect Zigbee USB adapter
     2. Integration Mode       — Standalone vs Home Assistant
     3. MQTT Configuration     — broker details (HA mode only)
     4. Summary & Apply        — review and write config

   Listens for WebSocket events: "setup_scan_progress"
   API endpoints:
     GET  /api/setup/status
     GET  /api/setup/ports
     POST /api/setup/scan
     POST /api/setup/apply
     POST /api/setup/apply-integration
     POST /api/setup/skip
   ============================================================================ */

(function () {
    'use strict';

    // ── State ────────────────────────────────────────────────────────────
    let wizardVisible = false;
    let scanning = false;
    let selectedResult = null;
    let scanResults = [];
    let currentStep = 1;
    const TOTAL_STEPS = 4;

    // Collected config across steps
    let wizardConfig = {
        coordinator: null,      // selected adapter result
        integrationMode: null,  // 'standalone' | 'homeassistant'
        mqtt: {
            broker_host: '',
            broker_port: 1883,
            username: '',
            password: '',
            base_topic: 'zigbee_manager',
            discovery_prefix: 'homeassistant',
        },
    };

    // ── DOM references (created lazily) ──────────────────────────────────
    let overlay = null;

    // ── Adapter family → icon / color mapping ────────────────────────────
    const ADAPTER_ICONS = {
        'Silicon Labs EZSP (Ember)':             { icon: 'fas fa-microchip',       color: 'primary' },
        'Silicon Labs CPC Multi-PAN (RCP)':      { icon: 'fas fa-project-diagram', color: 'info' },
        'Dresden Elektronik ConBee/RaspBee':     { icon: 'fas fa-wifi',            color: 'success' },
        'Texas Instruments Z-Stack':             { icon: 'fas fa-broadcast-tower', color: 'warning' },
        'Unknown Zigbee-like device':            { icon: 'fas fa-question-circle', color: 'secondary' },
    };

    // =====================================================================
    // OVERLAY / DOM
    // =====================================================================

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

    function show() {
        createOverlay();
        requestAnimationFrame(() => overlay.classList.add('active'));
        wizardVisible = true;
    }

    function hide() {
        if (overlay) overlay.classList.remove('active');
        wizardVisible = false;
    }

    // =====================================================================
    // STEP INDICATOR
    // =====================================================================

    function renderStepIndicator() {
        const steps = [
            { num: 1, label: 'Coordinator' },
            { num: 2, label: 'Integration' },
            { num: 3, label: 'MQTT' },
            { num: 4, label: 'Summary' },
        ];

        return `
            <div class="setup-steps mb-4">
                ${steps.map(s => {
                    const state = s.num < currentStep ? 'completed'
                                : s.num === currentStep ? 'active'
                                : 'pending';
                    // Skip step 3 indicator if standalone mode
                    if (s.num === 3 && wizardConfig.integrationMode === 'standalone') {
                        return '';
                    }
                    return `
                        <div class="setup-step ${state}">
                            <div class="step-dot">
                                ${state === 'completed'
                                    ? '<i class="fas fa-check"></i>'
                                    : s.num}
                            </div>
                            <div class="step-label">${s.label}</div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;
    }

    // =====================================================================
    // STEP 1: COORDINATOR DETECTION
    // =====================================================================

    function renderStep1Welcome(reason, currentPort) {
        const el = getContent();

        let reasonText = '';
        if (reason === 'no_config') {
            reasonText = 'Welcome! Let\'s set up your Zigbee coordinator and configure the application.';
        } else if (reason === 'no_port_configured') {
            reasonText = 'No serial port is configured for the Zigbee coordinator.';
        } else if (reason === 'port_missing') {
            reasonText = `The configured port <code>${currentPort}</code> is not present. The adapter may have been unplugged or the port changed.`;
        } else if (reason === 'mqtt_not_configured') {
            reasonText = 'Your coordinator is detected but MQTT integration needs to be configured.';
        } else {
            reasonText = 'Let\'s configure your Zigbee Matter Manager.';
        }

        el.innerHTML = `
            ${renderStepIndicator()}
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
                    Skip Setup
                </button>
                <button class="btn btn-primary" onclick="window._setupWizard.startScan()" id="setupScanBtn">
                    <i class="fas fa-search me-1"></i> Auto-Detect Coordinator
                </button>
            </div>
        `;

        refreshPorts();
    }

    function renderScanning() {
        const el = getContent();
        el.innerHTML = `
            ${renderStepIndicator()}
            <h4><i class="fas fa-satellite-dish me-2 text-primary fa-pulse"></i>Scanning...</h4>
            <p class="subtitle">Probing serial ports for Zigbee adapters. This takes 15–40 seconds.</p>

            <div class="scan-progress-bar mb-3">
                <div class="scan-progress-fill" id="setupProgressFill" style="width: 0%"></div>
            </div>
            <div class="small text-muted mb-3" id="setupProgressText">Starting scan...</div>

            <div id="setupScanLog" class="scan-log border rounded p-2 mb-3" style="max-height:200px; overflow-y:auto; font-size:0.8rem;">
            </div>

            <div class="setup-actions">
                <button class="btn btn-outline-danger btn-sm" onclick="window._setupWizard.cancelScan()">
                    <i class="fas fa-times me-1"></i> Cancel
                </button>
            </div>
        `;
    }

    function renderStep1Results(results) {
        const el = getContent();
        const zigbeeResults = results.filter(r => r.adapter_family !== 'Non-Zigbee serial device');
        scanResults = zigbeeResults;

        if (zigbeeResults.length === 0) {
            el.innerHTML = `
                ${renderStepIndicator()}
                <div class="no-adapters">
                    <i class="fas fa-exclamation-triangle d-block"></i>
                    <h5>No Zigbee Adapters Found</h5>
                    <p class="text-muted small">
                        Make sure your coordinator is plugged in and the device is passed through to the container.
                    </p>
                </div>
                <div class="setup-actions">
                    <button class="btn btn-outline-secondary" onclick="window._setupWizard.startScan()">
                        <i class="fas fa-redo me-1"></i> Scan Again
                    </button>
                    <button class="btn btn-outline-secondary" onclick="window._setupWizard.skip()">
                        Configure Manually
                    </button>
                </div>
            `;
            return;
        }

        // Auto-select if only one result
        if (zigbeeResults.length === 1) {
            selectedResult = zigbeeResults[0];
        }

        el.innerHTML = `
            ${renderStepIndicator()}
            <h4><i class="fas fa-check-circle me-2 text-success"></i>Coordinator Detected</h4>
            <p class="subtitle">Select the adapter to use for your Zigbee network.</p>

            <div class="adapter-results mb-3">
                ${zigbeeResults.map((r, i) => renderAdapterCard(r, i)).join('')}
            </div>

            <div class="setup-actions">
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.startScan()">
                    <i class="fas fa-redo me-1"></i> Re-scan
                </button>
                <button class="btn btn-primary" onclick="window._setupWizard.goToStep(2)"
                        id="setupNextBtn" ${selectedResult ? '' : 'disabled'}>
                    Next <i class="fas fa-arrow-right ms-1"></i>
                </button>
            </div>
        `;
    }

    function renderAdapterCard(result, index) {
        const ai = ADAPTER_ICONS[result.adapter_family] || ADAPTER_ICONS['Unknown Zigbee-like device'];
        const isSelected = selectedResult && selectedResult.port === result.port;
        const baud = result.baud_rate ? `${result.baud_rate} baud` : '';
        const flow = result.flow_control && result.flow_control !== 'none'
            ? ` / ${result.flow_control}` : '';
        const fw = result.firmware_version || '';

        return `
            <div class="adapter-result ${isSelected ? 'selected' : ''}"
                 onclick="window._setupWizard.selectResult(${index})">
                <div class="d-flex align-items-center">
                    <div class="adapter-icon text-${ai.color}">
                        <i class="${ai.icon}"></i>
                    </div>
                    <div class="flex-grow-1">
                        <div class="adapter-name">${result.adapter_family}</div>
                        <div class="adapter-port">${result.port}</div>
                        ${fw ? `<span class="badge bg-light text-dark adapter-badge">${fw}</span>` : ''}
                        ${result.board_name ? `<span class="badge bg-light text-dark adapter-badge">${result.board_name}</span>` : ''}
                        ${baud ? `<div class="adapter-detail mt-1">${baud}${flow}</div>` : ''}
                    </div>
                </div>
            </div>
        `;
    }

    // =====================================================================
    // STEP 2: INTEGRATION MODE
    // =====================================================================

    function renderStep2() {
        currentStep = 2;
        const el = getContent();

        el.innerHTML = `
            ${renderStepIndicator()}
            <h4><i class="fas fa-plug me-2 text-primary"></i>Integration Mode</h4>
            <p class="subtitle">How will you use Zigbee Matter Manager?</p>

            <div class="integration-options mb-4">
                <div class="integration-option ${wizardConfig.integrationMode === 'standalone' ? 'selected' : ''}"
                     onclick="window._setupWizard.selectIntegration('standalone')">
                    <div class="d-flex align-items-start">
                        <div class="integration-icon text-success">
                            <i class="fas fa-server"></i>
                        </div>
                        <div class="flex-grow-1">
                            <div class="fw-bold">Standalone</div>
                            <div class="small text-muted mt-1">
                                Run independently without Home Assistant.
                                Control devices directly through the web UI.
                                MQTT will be disabled.
                            </div>
                        </div>
                        <div class="integration-check">
                            ${wizardConfig.integrationMode === 'standalone'
                                ? '<i class="fas fa-check-circle text-success"></i>'
                                : '<i class="far fa-circle text-muted"></i>'}
                        </div>
                    </div>
                </div>

                <div class="integration-option ${wizardConfig.integrationMode === 'homeassistant' ? 'selected' : ''}"
                     onclick="window._setupWizard.selectIntegration('homeassistant')">
                    <div class="d-flex align-items-start">
                        <div class="integration-icon text-info">
                            <i class="fas fa-home"></i>
                        </div>
                        <div class="flex-grow-1">
                            <div class="fw-bold">Home Assistant</div>
                            <div class="small text-muted mt-1">
                                Connect to Home Assistant via MQTT.
                                Devices will appear in HA automatically via MQTT Discovery.
                                You'll need an MQTT broker (e.g. Mosquitto).
                            </div>
                        </div>
                        <div class="integration-check">
                            ${wizardConfig.integrationMode === 'homeassistant'
                                ? '<i class="fas fa-check-circle text-info"></i>'
                                : '<i class="far fa-circle text-muted"></i>'}
                        </div>
                    </div>
                </div>
            </div>

            <div class="setup-actions">
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.goToStep(1)">
                    <i class="fas fa-arrow-left me-1"></i> Back
                </button>
                <button class="btn btn-primary" onclick="window._setupWizard.nextFromStep2()"
                        id="setupStep2Next" ${wizardConfig.integrationMode ? '' : 'disabled'}>
                    Next <i class="fas fa-arrow-right ms-1"></i>
                </button>
            </div>
        `;
    }

    function selectIntegration(mode) {
        wizardConfig.integrationMode = mode;
        renderStep2();
    }

    function nextFromStep2() {
        if (!wizardConfig.integrationMode) return;
        if (wizardConfig.integrationMode === 'homeassistant') {
            goToStep(3);
        } else {
            // Standalone — skip MQTT, go to summary
            goToStep(4);
        }
    }

    // =====================================================================
    // STEP 3: MQTT CONFIGURATION (HA mode only)
    // =====================================================================

    function renderStep3() {
        currentStep = 3;
        const el = getContent();
        const m = wizardConfig.mqtt;

        el.innerHTML = `
            ${renderStepIndicator()}
            <h4><i class="fas fa-network-wired me-2 text-primary"></i>MQTT Broker</h4>
            <p class="subtitle">
                Enter your MQTT broker details. This is typically Mosquitto running on your
                Home Assistant host or a dedicated broker.
            </p>

            <div class="row g-3 mb-3">
                <div class="col-8">
                    <label class="form-label small fw-semibold">Broker Host / IP</label>
                    <input type="text" class="form-control" id="wizMqttHost"
                           value="${m.broker_host}" placeholder="192.168.1.x or mqtt.local">
                </div>
                <div class="col-4">
                    <label class="form-label small fw-semibold">Port</label>
                    <input type="number" class="form-control" id="wizMqttPort"
                           value="${m.broker_port}" placeholder="1883">
                </div>
            </div>

            <div class="row g-3 mb-3">
                <div class="col-6">
                    <label class="form-label small fw-semibold">Username</label>
                    <input type="text" class="form-control" id="wizMqttUser"
                           value="${m.username}" placeholder="mqtt_user">
                </div>
                <div class="col-6">
                    <label class="form-label small fw-semibold">Password</label>
                    <input type="password" class="form-control" id="wizMqttPass"
                           value="${m.password}" placeholder="mqtt_password">
                </div>
            </div>

            <div class="row g-3 mb-3">
                <div class="col-6">
                    <label class="form-label small fw-semibold">Base Topic</label>
                    <input type="text" class="form-control" id="wizMqttTopic"
                           value="${m.base_topic}" placeholder="zigbee_manager">
                    <div class="form-text">Topic prefix for all MQTT messages</div>
                </div>
                <div class="col-6">
                    <label class="form-label small fw-semibold">HA Discovery Prefix</label>
                    <input type="text" class="form-control" id="wizMqttDiscovery"
                           value="${m.discovery_prefix}" placeholder="homeassistant">
                    <div class="form-text">Must match HA's MQTT discovery prefix</div>
                </div>
            </div>

            <div id="wizMqttTestResult" class="mb-3" style="display:none;"></div>

            <div class="setup-actions">
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.goToStep(2)">
                    <i class="fas fa-arrow-left me-1"></i> Back
                </button>
                <button class="btn btn-outline-info" onclick="window._setupWizard.testMqtt()">
                    <i class="fas fa-plug me-1"></i> Test Connection
                </button>
                <button class="btn btn-primary" onclick="window._setupWizard.saveMqttAndNext()">
                    Next <i class="fas fa-arrow-right ms-1"></i>
                </button>
            </div>
        `;
    }

    function saveMqttFields() {
        wizardConfig.mqtt.broker_host = (document.getElementById('wizMqttHost')?.value || '').trim();
        wizardConfig.mqtt.broker_port = parseInt(document.getElementById('wizMqttPort')?.value) || 1883;
        wizardConfig.mqtt.username = (document.getElementById('wizMqttUser')?.value || '').trim();
        wizardConfig.mqtt.password = (document.getElementById('wizMqttPass')?.value || '').trim();
        wizardConfig.mqtt.base_topic = (document.getElementById('wizMqttTopic')?.value || 'zigbee_manager').trim();
        wizardConfig.mqtt.discovery_prefix = (document.getElementById('wizMqttDiscovery')?.value || 'homeassistant').trim();
    }

    function saveMqttAndNext() {
        saveMqttFields();

        // Basic validation
        if (!wizardConfig.mqtt.broker_host) {
            const el = document.getElementById('wizMqttTestResult');
            if (el) {
                el.style.display = 'block';
                el.innerHTML = `<div class="alert alert-warning small mb-0">
                    <i class="fas fa-exclamation-triangle me-1"></i>
                    Please enter a broker host address.
                </div>`;
            }
            return;
        }

        goToStep(4);
    }

    async function testMqtt() {
        saveMqttFields();
        const el = document.getElementById('wizMqttTestResult');
        if (!el) return;

        el.style.display = 'block';
        el.innerHTML = `<div class="alert alert-info small mb-0">
            <span class="scan-spinner me-1"></span> Testing connection to ${wizardConfig.mqtt.broker_host}:${wizardConfig.mqtt.broker_port}...
        </div>`;

        try {
            const res = await fetch('/api/setup/test-mqtt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(wizardConfig.mqtt),
            });
            const data = await res.json();

            if (data.success) {
                el.innerHTML = `<div class="alert alert-success small mb-0">
                    <i class="fas fa-check-circle me-1"></i> Connected successfully to MQTT broker.
                </div>`;
            } else {
                el.innerHTML = `<div class="alert alert-danger small mb-0">
                    <i class="fas fa-times-circle me-1"></i> Connection failed: ${data.error || 'Unknown error'}
                </div>`;
            }
        } catch (e) {
            el.innerHTML = `<div class="alert alert-danger small mb-0">
                <i class="fas fa-times-circle me-1"></i> Test failed: ${e.message}
            </div>`;
        }
    }

    // =====================================================================
    // STEP 4: SUMMARY & APPLY
    // =====================================================================

    function renderStep4() {
        currentStep = 4;
        const el = getContent();
        const coord = wizardConfig.coordinator || selectedResult || {};
        const mode = wizardConfig.integrationMode;
        const m = wizardConfig.mqtt;

        const ai = ADAPTER_ICONS[coord.adapter_family] || ADAPTER_ICONS['Unknown Zigbee-like device'];

        el.innerHTML = `
            ${renderStepIndicator()}
            <h4><i class="fas fa-clipboard-check me-2 text-primary"></i>Review Configuration</h4>
            <p class="subtitle">Confirm your settings before applying.</p>

            <div class="summary-section mb-3">
                <div class="summary-header">
                    <i class="fas fa-microchip me-1"></i> Coordinator
                </div>
                <div class="summary-body">
                    <div class="row small">
                        <div class="col-4 text-muted">Adapter</div>
                        <div class="col-8 fw-semibold">${coord.adapter_family || 'Unknown'}</div>
                    </div>
                    <div class="row small">
                        <div class="col-4 text-muted">Port</div>
                        <div class="col-8"><code>${coord.port || 'N/A'}</code></div>
                    </div>
                    ${coord.baud_rate ? `
                    <div class="row small">
                        <div class="col-4 text-muted">Baud Rate</div>
                        <div class="col-8">${coord.baud_rate}</div>
                    </div>` : ''}
                    ${coord.board_name ? `
                    <div class="row small">
                        <div class="col-4 text-muted">Board</div>
                        <div class="col-8">${coord.board_name}</div>
                    </div>` : ''}
                    ${coord.firmware_version ? `
                    <div class="row small">
                        <div class="col-4 text-muted">Firmware</div>
                        <div class="col-8">${coord.firmware_version}</div>
                    </div>` : ''}
                </div>
            </div>

            <div class="summary-section mb-3">
                <div class="summary-header">
                    <i class="fas fa-plug me-1"></i> Integration
                </div>
                <div class="summary-body">
                    <div class="row small">
                        <div class="col-4 text-muted">Mode</div>
                        <div class="col-8 fw-semibold">
                            ${mode === 'homeassistant'
                                ? '<i class="fas fa-home text-info me-1"></i>Home Assistant (MQTT)'
                                : '<i class="fas fa-server text-success me-1"></i>Standalone'}
                        </div>
                    </div>
                    ${mode === 'standalone' ? `
                    <div class="row small">
                        <div class="col-4 text-muted">MQTT</div>
                        <div class="col-8 text-warning">Disabled</div>
                    </div>` : ''}
                </div>
            </div>

            ${mode === 'homeassistant' ? `
            <div class="summary-section mb-3">
                <div class="summary-header">
                    <i class="fas fa-network-wired me-1"></i> MQTT
                </div>
                <div class="summary-body">
                    <div class="row small">
                        <div class="col-4 text-muted">Broker</div>
                        <div class="col-8"><code>${m.broker_host}:${m.broker_port}</code></div>
                    </div>
                    <div class="row small">
                        <div class="col-4 text-muted">Username</div>
                        <div class="col-8">${m.username || '<span class="text-muted">none</span>'}</div>
                    </div>
                    <div class="row small">
                        <div class="col-4 text-muted">Base Topic</div>
                        <div class="col-8"><code>${m.base_topic}</code></div>
                    </div>
                    <div class="row small">
                        <div class="col-4 text-muted">Discovery</div>
                        <div class="col-8"><code>${m.discovery_prefix}</code></div>
                    </div>
                </div>
            </div>` : ''}

            <div class="setup-actions">
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.goToStep(${mode === 'homeassistant' ? 3 : 2})">
                    <i class="fas fa-arrow-left me-1"></i> Back
                </button>
                <button class="btn btn-success" onclick="window._setupWizard.applyAll()" id="setupApplyBtn">
                    <i class="fas fa-check me-1"></i> Apply & Start
                </button>
            </div>
        `;
    }

    // =====================================================================
    // APPLY ALL SETTINGS
    // =====================================================================

    async function applyAll() {
        const el = getContent();
        el.innerHTML = `
            <h4><i class="fas fa-cog fa-spin me-2 text-primary"></i>Applying Configuration...</h4>
            <p class="subtitle">Writing settings to config.yaml and starting services.</p>
            <div class="scan-progress-bar mb-3">
                <div class="scan-progress-fill" style="width: 30%; transition: width 2s;"></div>
            </div>
        `;

        try {
            // Step A: Apply coordinator config
            const coord = wizardConfig.coordinator || selectedResult;
            if (coord) {
                const coordRes = await fetch('/api/setup/apply', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(coord),
                });
                const coordData = await coordRes.json();
                if (!coordData.success) {
                    throw new Error(coordData.error || 'Failed to save coordinator config');
                }
            }

            // Update progress
            el.querySelector('.scan-progress-fill').style.width = '60%';

            // Step B: Apply integration mode + MQTT
            const integrationRes = await fetch('/api/setup/apply-integration', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode: wizardConfig.integrationMode,
                    mqtt: wizardConfig.integrationMode === 'homeassistant'
                        ? wizardConfig.mqtt
                        : null,
                }),
            });
            const integrationData = await integrationRes.json();
            if (!integrationData.success) {
                throw new Error(integrationData.error || 'Failed to save integration config');
            }

            // Update progress
            el.querySelector('.scan-progress-fill').style.width = '100%';

            // Show success
            setTimeout(() => renderApplySuccess(), 500);

        } catch (e) {
            renderApplyError(e.message);
        }
    }

    function renderApplySuccess() {
        const el = getContent();
        el.innerHTML = `
            <div class="text-center py-3">
                <i class="fas fa-check-circle text-success" style="font-size: 3rem;"></i>
                <h4 class="mt-3">Setup Complete!</h4>
                <p class="subtitle">
                    Your Zigbee Matter Manager is configured and ready.
                    ${wizardConfig.integrationMode === 'homeassistant'
                        ? 'Devices will appear in Home Assistant via MQTT Discovery.'
                        : 'Running in standalone mode — control devices from the web UI.'}
                </p>
                <button class="btn btn-success btn-lg" onclick="window._setupWizard.finish()">
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
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.goToStep(4)">
                    <i class="fas fa-redo me-1"></i> Try Again
                </button>
                <button class="btn btn-outline-secondary" onclick="window._setupWizard.skip()">
                    Configure Manually
                </button>
            </div>
        `;
    }

    // =====================================================================
    // NAVIGATION
    // =====================================================================

    function goToStep(step) {
        currentStep = step;
        switch (step) {
            case 1:
                if (scanResults.length > 0) {
                    renderStep1Results(scanResults);
                } else {
                    renderStep1Welcome('', '');
                }
                break;
            case 2:
                wizardConfig.coordinator = selectedResult;
                renderStep2();
                break;
            case 3:
                renderStep3();
                break;
            case 4:
                renderStep4();
                break;
        }
    }

    // =====================================================================
    // ACTIONS (shared)
    // =====================================================================

    async function startScan() {
        if (scanning) return;
        scanning = true;
        scanResults = [];
        selectedResult = null;
        currentStep = 1;

        renderScanning();

        try {
            const res = await fetch('/api/setup/scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            const data = await res.json();
            if (!data.success) {
                scanning = false;
                renderStep1Results([]);
            }
        } catch (e) {
            scanning = false;
            renderStep1Results([]);
        }
    }

    function cancelScan() {
        scanning = false;
        goToStep(1);
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
                        ${p.vid ? `<span class="badge bg-light text-dark ms-1">${p.vid}:${p.pid}</span>` : ''}
                    </span>
                </div>
            `).join('');
        } catch (e) {
            el.innerHTML = `<div class="text-center py-3 text-muted small">Could not load ports.</div>`;
        }
    }

    function selectResult(index) {
        selectedResult = scanResults[index];
        renderStep1Results(scanResults);
    }

    function appendLog(text, type) {
        const log = document.getElementById('setupScanLog');
        if (!log) return;
        const cls = type === 'success' ? 'text-success'
                  : type === 'error' ? 'text-danger'
                  : 'text-muted';
        log.innerHTML += `<div class="${cls}">${text}</div>`;
        log.scrollTop = log.scrollHeight;
    }

    async function apply() {
        // Legacy single-step apply — now handled by applyAll
        await applyAll();
    }

    async function skip() {
        try {
            await fetch('/api/setup/skip', { method: 'POST' });
        } catch (e) { /* ignore */ }
        hide();
    }

    async function finish() {
        hide();

        try {
            const res = await fetch('/api/setup/start-zigbee', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                if (typeof window.alert === 'function') {
                    // Will use toast if toasts.js is loaded
                    window.alert('Zigbee network starting...');
                }
            }
        } catch (e) {
            console.warn('Failed to hot-start Zigbee:', e);
        }

        // Reload after a moment to pick up new state
        setTimeout(() => location.reload(), 3000);
    }

    // =====================================================================
    // WEBSOCKET PROGRESS HANDLER
    // =====================================================================

    function onScanProgress(payload) {
        if (!payload) return;

        const { phase, message, progress_pct, detail } = payload;

        // Update progress bar
        const fill = document.getElementById('setupProgressFill');
        if (fill) fill.style.width = `${progress_pct || 0}%`;

        const text = document.getElementById('setupProgressText');
        if (text) text.textContent = message;

        // Determine log type
        const type = phase === 'complete'
                   ? (detail?.error ? 'error' : 'success')
                   : message.includes('✅') ? 'success'
                   : message.includes('Error') || message.includes('failed') ? 'error'
                   : 'info';
        appendLog(message, type);

        // Scan complete?
        if (phase === 'complete') {
            scanning = false;
            const results = detail?.results || [];
            renderStep1Results(results);
        }
    }

    // =====================================================================
    // INIT
    // =====================================================================

    async function init() {
        try {
            const res = await fetch('/api/setup/status');
            const data = await res.json();

            if (data.needs_setup && !data.skipped) {
                show();
                currentStep = 1;

                // If coordinator is already configured but MQTT isn't,
                // jump to step 2
                if (data.reason === 'mqtt_not_configured') {
                    // Pre-fill coordinator info
                    wizardConfig.coordinator = { port: data.current_port };
                    selectedResult = wizardConfig.coordinator;
                    renderStep2();
                } else {
                    renderStep1Welcome(data.reason, data.current_port);
                }
            }
        } catch (e) {
            console.debug('Setup wizard: status check failed, skipping', e);
        }
    }

    // ── Public API ───────────────────────────────────────────────────────

    window._setupWizard = {
        init,
        show,
        hide,
        startScan,
        cancelScan,
        refreshPorts,
        selectResult,
        selectIntegration,
        nextFromStep2,
        saveMqttAndNext,
        testMqtt,
        goToStep,
        applyAll,
        apply,
        skip,
        finish,
        onScanProgress,
    };

    // ── WebSocket hook ───────────────────────────────────────────────────

    const _origWsHandler = window._onWsMessage;

    window._onWsMessage = function (data) {
        if (data.type === 'setup_scan_progress') {
            onScanProgress(data.payload);
            return;
        }
        if (typeof _origWsHandler === 'function') {
            _origWsHandler(data);
        }
    };

    // ── Auto-init ────────────────────────────────────────────────────────

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        setTimeout(init, 500);
    }

})();