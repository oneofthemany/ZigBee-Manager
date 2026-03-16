/**
 * main.js
 * Application entry point
 * * Imports and initializes all sub-modules.
 * Exposes necessary functions to the global window object for HTML onclick events.
 */

import { state } from './state.js';
import { updateLastSeenTimes } from './utils.js';
import { initWS } from './websocket.js';
import { fetchAllDevices } from './devices.js';
import { initGroups } from './groups.js';
import { initMQTTExplorer, handleMQTTMessage } from './mqtt-explorer.js';
import { initEditor, getEditorInstance } from './editor.js';
let editorInitialised = false;

import { initSettings } from './settings.js';
initSettings();

import {
    loadTabs,
    filterByTab,
    openTabManager,
    createNewTab,
    deleteTab,
    manageTabDevices,
    toggleDeviceInTab
} from './tabs.js';

import {
    initAutomationsPage,
    loadAutomationsPage
} from './automations-page.js';

import {
    initZones,
    recalibrateZone,
    deleteZone,
    viewZoneDetails
} from './zones.js';

import {
    openDeviceModal,
    saveConfig,
    getDeviceStateHtml
} from './device-modal.js';
import {
    filterLogs,
    clearLogs,
    toggleDebug,
    toggleVerboseLogging,
    viewDebugPackets,
    refreshDebugPackets,
    clearDebugFilters,
    downloadDebugLog
} from './logging.js';
import {
    loadConfigYaml,
    saveConfigYaml,
    restartSystem,
    loadSSLStatus,
    toggleSSL
} from './system.js';
import {
    sendCommand,
    adjustSetpoint,
    doAction,
    renamePrompt,
    togglePairing,
    permitJoinVia,
    checkPairingStatus,
    bindDevices,
    startTouchlinkScan,
    openBannedModal,
    handleUnbanClick,
    cleanupOrphans,
    matterCommission,
    checkMatterStatus
} from './actions.js';
import {
    initMesh,
    loadMeshTopology,
    dashboardMeshRefresh,
    dashboardMeshReset,
    dashboardMeshCenter,
    toggleMeshLabels
} from './mesh.js';

import { renderOTATab, handleOTAProgress } from './modal/ota.js';

// ============================================================================
// EXPOSE FUNCTIONS GLOBALLY
// ============================================================================

// Device management
window.openDeviceModal = openDeviceModal;
window.renamePrompt = renamePrompt;
window.fetchAllDevices = fetchAllDevices;
window.getDeviceStateHtml = getDeviceStateHtml;
window.cleanupOrphans = cleanupOrphans;

// Device actions
window.sendCommand = sendCommand;
window.adjustSetpoint = adjustSetpoint;
window.doAction = doAction;
window.saveConfig = saveConfig;

// Pairing
window.togglePairing = togglePairing;
window.permitJoinVia = permitJoinVia;
window.checkPairingStatus = checkPairingStatus;
window.startTouchlinkScan = startTouchlinkScan;
window.bindDevices = bindDevices;

// Matter
window.matterCommission = matterCommission;
window.checkMatterStatus = checkMatterStatus;

// Logging & Debug
window.filterLogs = filterLogs;
window.clearLogs = clearLogs;
window.toggleDebug = toggleDebug;
window.toggleVerboseLogging = toggleVerboseLogging;
window.viewDebugPackets = viewDebugPackets;
window.refreshDebugPackets = refreshDebugPackets;
window.clearDebugFilters = clearDebugFilters;
window.downloadDebugLog = downloadDebugLog;

// System & Config
window.loadConfigYaml = loadConfigYaml;
window.saveConfigYaml = saveConfigYaml;
window.restartSystem = restartSystem;
window.loadSSLStatus = loadSSLStatus;
window.toggleSSL = toggleSSL;

window.applyManualChannel = function() {
    const ch = parseInt(document.getElementById('manualChannelSelect').value);
    const sel = document.getElementById('cfg_channel');
    if (sel) sel.value = ch;
    document.getElementById('spectrumStatus').innerHTML =
        `<span class="text-primary">Channel ${ch} set in config. Click Save on the Configuration tab.</span>`;
};

// Mesh Topology
window.loadMeshTopology = loadMeshTopology;
window.dashboardMeshRefresh = dashboardMeshRefresh;
window.dashboardMeshReset = dashboardMeshReset;
window.dashboardMeshCenter = dashboardMeshCenter;
window.toggleMeshLabels = toggleMeshLabels;

window.state = state;

// Device Stuff
window.bindDevices = bindDevices;
window.openBannedModal = openBannedModal;
window.handleUnbanClick = handleUnbanClick;

// Automations Page
window.loadAutomationsPage = loadAutomationsPage;

// OTA
window.renderOTATab = renderOTATab;

window.otaCheckAll = async function() {
    const btn = document.getElementById('otaCheckAllBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Checking...';
    }
    try {
        const resp = await fetch('/api/ota/check-all');
        const data = await resp.json();
        const count = data.devices_with_updates || 0;
        if (count > 0) {
            let msg = `${count} device(s) have firmware updates available:\n\n`;
            for (const [ieee, info] of Object.entries(data.updates || {})) {
                const dev = state.deviceCache[ieee];
                const name = dev ? dev.friendly_name : ieee;
                msg += `• ${name}: ${info.current_version} → ${info.new_version}\n`;
            }
            msg += '\nOpen each device\'s OTA tab to install.';
            alert(msg);
        } else {
            alert('All devices are up to date — no firmware updates available.');
        }
    } catch (e) {
        alert('OTA check failed: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-microchip"></i> Check OTA';
        }
    }
};

// Zones Management
window.recalibrateZone = recalibrateZone;
window.deleteZone = deleteZone;
window.viewZoneDetails = viewZoneDetails;

// Tabs Management
window.loadTabs = loadTabs;
window.filterByTab = filterByTab;
window.openTabManager = openTabManager;
window.createNewTab = createNewTab;
window.deleteTab = deleteTab;
window.manageTabDevices = manageTabDevices;
window.toggleDeviceInTab = toggleDeviceInTab;

// ============================================================================
// APPLICATION INITIALISATION
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // Initialise WebSocket connection
    initWS();

    // Start update interval for "last seen" times
    setInterval(updateLastSeenTimes, 1000);

    // Initialise Tabs
    loadTabs();

    // Initialise Mesh Tab listener
    initMesh();

    // Initialise Groups
    initGroups();

    // Initialise Explorer
    initMQTTExplorer();

    // Initialise Automations Page
    initAutomationsPage();

    // Initialise Zones
    initZones();

    // Initial fetch
    fetchAllDevices();

    // Check if pairing is currently active (Persistence)
    if(typeof checkPairingStatus === 'function') checkPairingStatus();

    // Initial matter
    checkMatterStatus();

    // Initialise Settings Tab listener
    const settingsTab = document.querySelector('button[data-bs-target="#settings"]');
    if(settingsTab) {
        settingsTab.addEventListener('click', () => {
            loadConfigYaml();
            loadSSLStatus();  // ADD
        });
    }

    const topologyTab = document.querySelector('button[data-bs-target="#topology"]');
    if (topologyTab) {
        topologyTab.addEventListener('shown.bs.tab', () => {
            // This ensures the D3 force simulation or SVG scales
            // correctly once the container is actually visible.
            if (typeof loadMeshTopology === 'function') {
                loadMeshTopology();
            }
        });
    }

    document.querySelector('[data-bs-target="#editor-tab"]')?.addEventListener('shown.bs.tab', () => {
        if (!editorInitialised) {
            initEditor();
            editorInitialised = true;
        } else {
            const inst = getEditorInstance();
            if (inst) inst.layout();
        }
    });

    // Check for pending test recovery on every page load
    fetch('/api/editor/test-status').then(r => r.json()).then(data => {
        if (data.pending) {
            const banner = document.createElement('div');
            banner.id = 'testRecoveryBanner';
            banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;';
            let remaining = data.remaining || 120;
            banner.innerHTML = `
                <div style="background:#1e1e1e;border-bottom:2px solid #dc3545;padding:8px 16px;">
                    <div class="d-flex align-items-center justify-content-between">
                        <div class="d-flex align-items-center gap-2" style="color:#fff;font-size:13px;">
                            <i class="fas fa-flask" style="color:#ffc107;"></i>
                            <strong>Test Deploy Active</strong>
                            <span style="color:#ffc107;"><span id="testCountdown">${remaining}</span>s remaining</span>
                        </div>
                        <div class="d-flex gap-2">
                            <button class="btn btn-sm btn-success px-3" onclick="fetch('/api/editor/test-confirm',{method:'POST'}).then(r=>r.json()).then(d=>{if(d.success){document.getElementById('testRecoveryBanner').remove()}else{alert(d.error)}})">
                                <i class="fas fa-check me-1"></i> Confirm
                            </button>
                            <button class="btn btn-sm btn-danger px-3" onclick="fetch('/api/editor/test-rollback',{method:'POST'}).then(r=>r.json()).then(d=>{alert(d.message||d.error);document.getElementById('testRecoveryBanner').remove()})">
                                <i class="fas fa-undo me-1"></i> Rollback
                            </button>
                        </div>
                    </div>
                    <div class="progress mt-1" style="height:3px;">
                        <div id="testProgressBar" class="progress-bar bg-warning" style="width:100%;transition:width 1s linear;"></div>
                    </div>
                </div>`;
            document.body.prepend(banner);
            const iv = setInterval(() => {
                remaining--;
                const el = document.getElementById('testCountdown');
                const bar = document.getElementById('testProgressBar');
                if (el) el.textContent = remaining;
                if (bar) bar.style.width = (remaining / (data.remaining || 120) * 100) + '%';
                if (remaining <= 0) { clearInterval(iv); location.reload(); }
            }, 1000);
        }
    }).catch(() => {});

    console.log("Zigbee Gateway Frontend Initialized");
});
