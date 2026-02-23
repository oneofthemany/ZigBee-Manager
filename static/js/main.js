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
    cleanupOrphans
} from './actions.js';
import {
    initMesh,
    loadMeshTopology,
    dashboardMeshRefresh,
    dashboardMeshReset,
    dashboardMeshCenter,
    toggleMeshLabels
} from './mesh.js';

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

    // Initialise Zones
    initZones();

    // Initial fetch
    fetchAllDevices();

    // Check if pairing is currently active (Persistence)
    if(typeof checkPairingStatus === 'function') checkPairingStatus();

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

    console.log("Zigbee Gateway Frontend Initialized");
});