/**
 * Device Configuration Definitions & Helpers
 * Location: static/js/modal/config.js
 */

// ============================================================================
// HELPERS
// ============================================================================

export function hasCluster(device, clusterId) {
    if (!device.capabilities) return false;
    return device.capabilities.some(ep =>
        (ep.inputs && ep.inputs.some(c => c.id === clusterId)) ||
        (ep.outputs && ep.outputs.some(c => c.id === clusterId))
    );
}

// --- SPECIFIC DEVICE MATCHERS ---

export function isZyM100(d) {
    const man = (d.manufacturer || '').toLowerCase();
    const mod = (d.model || '').toLowerCase();
    return man.includes('_tze204_7gclukjs') || mod.includes('zy-m100');
}

export function isTuyaRadar(d) {
    const man = (d.manufacturer || '').toLowerCase();
    const mod = (d.model || '').toLowerCase();
    
    // 1. Must be Tuya-based
    const isTuya = man.includes('_tze') || man.includes('tuya') || hasCluster(d, 0xEF00);
    if (!isTuya) return false;

    // 2. EXCLUSION: Explicitly reject Blinds/Lights/Switches sharing TS0601
    if (mod.includes('curtain') || mod.includes('blind') || mod.includes('light') || mod.includes('switch')) return false;

    // 3. INCLUSION: Must match specific radar signatures
    return mod.includes('radar') ||
           mod.includes('mmwave') ||
           mod.includes('24g') ||
           man.includes('_tze204') || 
           man.includes('zy-m100') ||
           (mod.includes('ts0601') && (man.includes('human') || man.includes('presence')));
}

export function isAqaraTRV(d) {
    const man = (d.manufacturer || '').toLowerCase();
    const mod = (d.model || '').toLowerCase();
    
    if (!man.includes('lumi') && !man.includes('aqara')) return false;

    // STRICT: Only accept known TRV Model signatures.
    // We do NOT check clusters here because Aqara Lights/Switches often 
    // expose 0x201 (Thermostat) for internal temperature, causing false positives.
    return mod.includes('airrtc') || mod.includes('thermostat') || mod.includes('agl001');
}

export function isAqaraSwitch(d) {
    const man = (d.manufacturer || '').toLowerCase();
    const mod = (d.model || '').toLowerCase();

    if (!man.includes('lumi') && !man.includes('aqara')) return false;
    if (isAqaraTRV(d)) return false; // Safety check

    // Match Switches, Relays, Plugs
    return mod.includes('switch') || mod.includes('relay') || mod.includes('plug') || mod.includes('ctrl');
}

export function isPhilipsMotion(d) {
    const man = (d.manufacturer || '').toLowerCase();
    return (man.includes('philips') || man.includes('signify')) && hasCluster(d, 0x0406);
}

export function isIkeaTradfri(d) {
    const man = (d.manufacturer || '').toLowerCase();
    return man.includes('ikea');
}

// ============================================================================
// CONFIG DEFINITIONS
// ============================================================================

export const CONFIG_DEFINITIONS = [
    // --- Tuya Radar Sensors ---
    {
        key: 'radar_sensitivity',
        label: 'Radar Sensitivity',
        type: 'number',
        min: 0, max: 10,
        condition: (d) => isTuyaRadar(d)
    },
    {
        key: 'presence_sensitivity',
        label: 'Presence Sensitivity',
        type: 'number',
        min: 0, max: 10,
        condition: (d) => isTuyaRadar(d)
    },
    {
        key: 'keep_time',
        label: 'Keep Time (s)',
        type: 'number',
        min: 0, max: 3600,
        condition: (d) => isTuyaRadar(d)
    },
    {
        key: 'detection_distance_min',
        label: 'Min Distance (m)',
        type: 'number',
        step: 0.01, min: 0, max: 10,
        condition: (d) => isTuyaRadar(d)
    },
    {
        key: 'detection_distance_max',
        label: 'Max Distance (m)',
        type: 'number',
        step: 0.01, min: 0, max: 10,
        condition: (d) => isTuyaRadar(d)
    },
    {
        key: 'fading_time',
        label: 'Fading Time (s)',
        type: 'number',
        min: 0, max: 3600,
        condition: (d) => isTuyaRadar(d) && !isZyM100(d)
    },

    // --- Thermostats (Generic) ---
    {
        key: 'local_temperature_calibration',
        label: 'Temp Calibration (°C)',
        type: 'number',
        step: 0.1, min: -10, max: 10,
        condition: (d) => hasCluster(d, 0x0201)
    },

    // --- Aqara TRV Specifics (Strictly filtered) ---
    {
        key: 'window_detection',
        label: 'Window Detection',
        type: 'select',
        options: [{value: 0, label: 'Disabled'}, {value: 1, label: 'Enabled'}],
        condition: (d) => isAqaraTRV(d)
    },
    {
        key: 'child_lock',
        label: 'Child Lock',
        type: 'select',
        options: [{value: 'UNLOCK', label: 'Unlock'}, {value: 'LOCK', label: 'Lock'}],
        condition: (d) => isAqaraTRV(d)
    },
    {
        key: 'valve_detection',
        label: 'Valve Detection',
        type: 'select',
        options: [{value: 0, label: 'Disabled'}, {value: 1, label: 'Enabled'}],
        condition: (d) => isAqaraTRV(d)
    },
    {
        key: 'motor_calibration',
        label: 'Calibrate Valve',
        type: 'select',
        options: [{value: 0, label: 'Idle'}, {value: 1, label: 'Start Calibration'}],
        condition: (d) => isAqaraTRV(d)
    },

    // --- Aqara Switches ---
    {
        key: 'power_outage_memory',
        label: 'Power Outage Memory',
        type: 'select',
        options: [{value: 0, label: 'Off'}, {value: 1, label: 'On/Restore'}],
        condition: (d) => isAqaraSwitch(d)
    },

    // --- Generic Motion ---
    {
        key: 'occupancy_timeout',
        label: 'Occupancy Timeout (s)',
        type: 'number',
        min: 0, max: 65535,
        condition: (d) => hasCluster(d, 0x0406)
    }
];

export const getClusterName = (id, defaultName) => {
    // ... (Your existing map logic, shortened for brevity if needed)
    return defaultName; // Placeholder, relying on imports usually
};

