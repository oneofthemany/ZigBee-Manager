/**
 * Shared Application State
 * Central store for all global state variables
 */

export const state = {
    socket: null,
    allLogs: [],
    devices: [],
    currentDeviceIeee: null,
    deviceCache: {},
    debugEnabled: false,
    verboseLogging: false,
    isRestarting: false,
    pairingInterval: null,
    tableSortInitialised: false,
    deviceFilter: null,
    controlInteractionActive: false,  // Prevents modal refresh during slider/picker interaction
    heatingManaged: { enabled: false, ieees: new Set() }  // IEEEs managed by heating controller
};
window._getDeviceState = function(ieee) {
    return state.deviceCache[ieee]?.state || {};
};