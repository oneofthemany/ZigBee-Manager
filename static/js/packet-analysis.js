/**
 * Packet Analyser - Deep Packet Inspection for Zigbee Messages
 * ============================================================
 * fully fledged implementation of ZCL (Zigbee Cluster Library) analysis.
 * Covers Global Commands, Cluster Specific Commands, and Tuya Protocols.
 */

// =============================================================================
// 1. ZIGBEE CONSTANTS & REGISTRIES
// =============================================================================

const CLUSTER_NAMES = {
    // General
    0x0000: "Basic",
    0x0001: "Power Configuration",
    0x0002: "Device Temperature",
    0x0003: "Identify",
    0x0004: "Groups",
    0x0005: "Scenes",
    0x0006: "On/Off",
    0x0008: "Level Control",
    0x0009: "Alarms",
    0x000A: "Time",
    0x000D: "Analog Output",
    0x0019: "OTA Upgrade",
    0x0020: "Poll Control",
    0x0021: "Green Power",

    // Closures
    0x0100: "Shade Configuration",
    0x0101: "Door Lock",
    0x0102: "Window Covering",

    // HVAC
    0x0200: "Pump Configuration",
    0x0201: "Thermostat",
    0x0202: "Fan Control",
    0x0204: "Thermostat UI Config",

    // Lighting
    0x0300: "Color Control",
    0x0301: "Ballast Configuration",

    // Measurement & Sensing
    0x0400: "Illuminance Measurement",
    0x0402: "Temperature Measurement",
    0x0403: "Pressure Measurement",
    0x0405: "Humidity Measurement",
    0x0406: "Occupancy Sensing",
    0x042A: "PM2.5 Measurement",

    // Security & Safety
    0x0500: "IAS Zone",
    0x0501: "IAS Ace",
    0x0502: "IAS Warning Device",

    // Smart Energy / Metering
    0x0702: "Simple Metering",
    0x0B04: "Electrical Measurement",

    // Manufacturer Specific
    0xEF00: "Tuya Manufacturer Specific",
    0xE001: "Tuya Private Cluster 2",
    0xFC01: "Philips Manufacturer Specific",
    0xFC02: "Ikea Manufacturer Specific",
};

const GLOBAL_COMMANDS = {
    0x00: "Read Attributes",
    0x01: "Read Attributes Response",
    0x02: "Write Attributes",
    0x03: "Write Attributes Undivided",
    0x04: "Write Attributes Response",
    0x05: "Write Attributes No Response",
    0x06: "Configure Reporting",
    0x07: "Configure Reporting Response",
    0x08: "Read Reporting Config",
    0x09: "Read Reporting Config Response",
    0x0A: "Report Attributes",
    0x0B: "Default Response",
    0x0C: "Discover Attributes",
    0x0D: "Discover Attributes Response",
};

// Common Attributes for decoding "Report Attributes" payloads
// Format: { ClusterID: { AttributeID: "Name" } }
const COMMON_ATTRIBUTES = {
    0x0000: { // Basic
        0x0000: "ZCLVersion",
        0x0001: "ApplicationVersion",
        0x0002: "StackVersion",
        0x0003: "HWVersion",
        0x0004: "ManufacturerName",
        0x0005: "ModelIdentifier",
        0x0006: "DateCode",
        0x0007: "PowerSource",
        0x4000: "SWBuildID"
    },
    0x0001: { // Power
        0x0020: "BatteryVoltage",
        0x0021: "BatteryPercentageRemaining"
    },
    0x0006: { // On/Off
        0x0000: "OnOff",
        0x4001: "OnTime",
        0x4002: "OffWaitTime",
        0x8000: "ChildLock (Tuya)",
        0x8001: "BacklightMode (Tuya)",
        0x8002: "PowerOnBehavior (Tuya)"
    },
    0x0008: { // Level
        0x0000: "CurrentLevel"
    },
    0x0102: { // Window Covering
        0x0000: "WindowCoveringType",
        0x0008: "CurrentPositionLiftPercentage",
        0x0009: "CurrentPositionTiltPercentage"
    },
    0x0201: { // Thermostat
        0x0000: "LocalTemperature",
        0x0011: "OccupiedCoolingSetpoint",
        0x0012: "OccupiedHeatingSetpoint",
        0x001C: "SystemMode",
        0x001E: "RunningMode"
    },
    0x0400: { 0x0000: "MeasuredValue" }, // Illuminance
    0x0402: { 0x0000: "MeasuredValue" }, // Temperature
    0x0405: { 0x0000: "MeasuredValue" }, // Humidity
    0x0406: { 0x0000: "Occupancy" },     // Occupancy
};

// Specific Commands (Cluster Specific)
// Format: { ClusterID: { CommandID: "Name" } }
const CLUSTER_SPECIFIC_COMMANDS = {
    0x0003: { // Identify
        0x00: "Identify",
        0x01: "Identify Query",
        0x40: "Trigger Effect"
    },
    0x0006: { // On/Off
        0x00: "Off",
        0x01: "On",
        0x02: "Toggle",
        0x40: "Off With Effect",
        0x41: "On With Recall Global Scene",
        0x42: "On With Timed Off"
    },
    0x0008: { // Level
        0x00: "Move to Level",
        0x01: "Move",
        0x02: "Step",
        0x03: "Stop",
        0x04: "Move to Level (with On/Off)"
    },
    0x0102: { // Window Covering
        0x00: "Up/Open",
        0x01: "Down/Close",
        0x02: "Stop",
        0x05: "Go to Lift Percentage",
        0x08: "Go to Tilt Percentage"
    },
    0x0500: { // IAS Zone
        0x00: "Zone Status Change Notification",
        0x01: "Zone Enroll Request"
    }
};

/**
 * Tuya Constants
 */
const TUYA_DP_TYPES = {
    0x00: "RAW", 0x01: "BOOL", 0x02: "VALUE", 0x03: "STRING", 0x04: "ENUM", 0x05: "BITMAP"
};

const TUYA_COMMANDS = {
    0x00: "SET_DATA", 0x01: "GET_DATA", 0x02: "SET_DATA_RESPONSE",
    0x03: "QUERY_DATA", 0x06: "ACTIVE_STATUS_REPORT", 0x24: "TIME_REQUEST"
};

const TUYA_COMMON_DPS = {
    1:  { name: "State/Presence", types: [0x01, 0x04], hints: ["Boolean ON/OFF", "Enum: 0=none, 1=presence, 2=move"] },
    2:  { name: "Sensitivity", types: [0x02], hints: ["Range: 1-10"] },
    3:  { name: "Distance Min", types: [0x02], hints: ["Scale: 0.01 (cm to meters)"] },
    4:  { name: "Distance Max", types: [0x02], hints: ["Scale: 0.01 (cm to meters)"] },
    9:  { name: "Distance", types: [0x02], hints: ["Scale: 0.01 or 0.1 depending on model"] },
    101: { name: "Illuminance", types: [0x02], hints: ["Lux"] },
    104: { name: "Presence/Lux", types: [0x01, 0x04, 0x02], hints: ["Model dependent"] },
    105: { name: "Hold Time", types: [0x02], hints: ["Seconds"] }
};


// =============================================================================
// 2. PARSING LOGIC
// =============================================================================

/**
 * Parse Tuya Payload (This is the old, rough client-side implementation.
 * We will rely on the backend's rich 'tuya_dps' data if available in the packet
 * and only use this as a fallback).
 */
function parseTuyaPayload(hexPayload) {
    try {
        const data = hexToBytes(hexPayload);
        if (data.length < 4) return null;

        const results = [];
        let offset = 0;
        let sequence = null;

        // Sequence number check
        if (data[0] === 0x00 && data[1] < 0x80) {
            sequence = (data[0] << 8) | data[1];
            offset = 2;
        }

        while (offset < data.length - 3) {
            try {
                const dp_id = data[offset];
                const dp_type = data[offset + 1];
                const dp_len = (data[offset + 2] << 8) | data[offset + 3];

                if (offset + 4 + dp_len > data.length) break;
                const dp_data = data.slice(offset + 4, offset + 4 + dp_len);

                let value, valueStr;
                if (dp_type === 0x01) { // BOOL
                    value = dp_data[0] !== 0;
                    valueStr = value ? "True" : "False";
                } else if (dp_type === 0x02) { // VALUE
                    value = 0;
                    for (let i = 0; i < dp_data.length; i++) value = (value << 8) | dp_data[i];
                    valueStr = value.toString();
                } else if (dp_type === 0x04) { // ENUM
                    value = dp_data[0] || 0;
                    valueStr = value.toString();
                } else if (dp_type === 0x03) { // STRING
                    valueStr = bytesToString(dp_data);
                    value = valueStr;
                } else {
                    valueStr = bytesToHex(dp_data);
                    value = valueStr;
                }

                results.push({
                    dp_id, dp_type,
                    dp_type_name: TUYA_DP_TYPES[dp_type] || `0x${dp_type.toString(16)}`,
                    value, valueStr,
                    raw_hex: bytesToHex(dp_data)
                });
                offset += 4 + dp_len;
            } catch (e) { break; }
        }
        return { sequence, dps: results };
    } catch (e) { return null; }
}

/**
 * Analyze a single Tuya DP
 */
function analyseTuyaDP(dp) {
    const interpretation = {
        dp_id: dp.dp_id,
        dp_type_name: dp.dp_type_name,
        value: dp.valueStr,
        raw_hex: dp.raw_hex,
        meaning: "Unknown DP",
        hints: [],
        potential_issues: [],
        derived_states: []
    };

    const commonDP = TUYA_COMMON_DPS[dp.dp_id];
    if (commonDP) {
        interpretation.meaning = commonDP.name;
        interpretation.hints = commonDP.hints;

        // Simple derived state examples
        if (dp.dp_id === 1 && dp.dp_type === 0x04) {
            const states = {0: "none", 1: "presence", 2: "move"};
            interpretation.derived_states.push(`state = "${states[dp.value] || 'unknown'}"`);
        }
    }
    return interpretation;
}

// =============================================================================
// 3. MAIN ANALYSIS FUNCTION
// =============================================================================

/**
 * Main Packet Analysis Function
 * Robust against undefined/missing data
 */
export function analysePacket(packet) {
    // 1. Safety Normalization
    const cid = packet.cluster_id !== undefined ? packet.cluster_id : (packet.cluster || 0);
    const cmdId = packet.decoded?.command_id !== undefined ? packet.decoded.command_id : -1;
    const isClusterSpecific = packet.decoded?.frame_control?.cluster_specific || false;

    // 2. Base Analysis Object
    const analysis = {
        timestamp: packet.timestamp_str,
        ieee: packet.ieee,
        cluster_id: cid,
        cluster_name: CLUSTER_NAMES[cid] || `0x${cid.toString(16).padStart(4, '0')}`,
        command: "Unknown",
        command_id: cmdId,
        summary: "",
        details: [],
        recommendations: [],
        tuya_analysis: null // Will hold the structured data from the backend
    };

    // 3. Command Resolution
    if (isClusterSpecific) {
        // Look up Cluster Specific Command
        if (CLUSTER_SPECIFIC_COMMANDS[cid] && CLUSTER_SPECIFIC_COMMANDS[cid][cmdId]) {
            analysis.command = CLUSTER_SPECIFIC_COMMANDS[cid][cmdId];
        } else if (cid === 0xEF00) {
            analysis.command = TUYA_COMMANDS[cmdId] || `Tuya Cmd 0x${cmdId.toString(16)}`;
        } else {
            analysis.command = packet.decoded?.command_name || `Cluster Cmd 0x${cmdId.toString(16)}`;
        }
    } else {
        // Look up Global ZCL Command
        analysis.command = GLOBAL_COMMANDS[cmdId] || `Global Cmd 0x${cmdId.toString(16)}`;
    }

    // 4. Detailed Analysis Logic

    // --- A. Tuya Analysis (Prioritise Backend Data: packet.tuya_dps) ---
    if (cid === 0xEF00) {
        analysis.summary = analysis.command;

        if (packet.tuya_dps && packet.tuya_dps.length > 0) {
            // Use RICH, STRUCTURED DATA from backend
            analysis.tuya_analysis = {
                dps: packet.tuya_dps
            };

            // Build rich summary from backend data
            const summaries = packet.tuya_dps.map(dp => {
                const name = dp.dp_def_name || `DP${dp.dp_id}`;
                return `${name}=${dp.parsed_value}${dp.dp_def_unit}`;
            });
            analysis.summary += `: ${summaries.join(', ')}`;

        } else if (packet.decoded?.payload) {
            // FALLBACK: Use rough client-side parsing
            const parsed = parseTuyaPayload(packet.decoded.payload);
            if (parsed && parsed.dps.length > 0) {
                analysis.tuya_analysis = {
                    sequence: parsed.sequence,
                    dps: parsed.dps.map(dp => analyseTuyaDP(dp))
                };
                const summaries = parsed.dps.map(dp => {
                    const name = TUYA_COMMON_DPS[dp.dp_id]?.name || `DP${dp.dp_id}`;
                    return `${name}=${dp.valueStr}`;
                });
                analysis.summary += `: ${summaries.join(', ')}`;
            }
        }
    }

    // --- B. ZCL Attribute Reporting (0x0A) or Read Response (0x01) ---
    else if ((cmdId === 0x0A || cmdId === 0x01) && !isClusterSpecific) {
        analysis.summary = `${analysis.cluster_name} Report`;

        if (COMMON_ATTRIBUTES[cid]) {
             analysis.recommendations.push(`ℹ️ This cluster usually reports: ${Object.values(COMMON_ATTRIBUTES[cid]).join(', ')}`);
        }
    }

    // --- C. Specific Cluster Logic ---
    else if (cid === 0x0006 && isClusterSpecific) { // On/Off
        analysis.summary = `Switch ${analysis.command}`;
    }
    else if (cid === 0x0406) { // Occupancy
        analysis.summary = "Occupancy Sensor Activity";
    }
    else if (cid === 0x0500 && cmdId === 0x00) { // IAS Zone Status
        analysis.summary = "Security Sensor Status Change";
        analysis.recommendations.push("✓ Critical security packet");
    }

    return analysis;
}

// =============================================================================
// 4. RENDERING (HTML GENERATION)
// =============================================================================

export function renderPacketAnalysis(packet) {
    let analysis;
    try {
        analysis = analysePacket(packet);
    } catch (e) {
        console.error("Analysis failed", e);
        return `<div class="alert alert-danger">Analysis Error: ${e.message}</div>`;
    }

    // Safe Hex Display
    const cidHex = (analysis.cluster_id || 0).toString(16).padStart(4, '0');

    let html = '<div class="packet-analysis border-start border-3 border-primary ps-3 mb-3">';

    // Header
    html += `<div class="d-flex justify-content-between align-items-start mb-2">`;
    html += `<div>`;
    html += `<strong>${analysis.cluster_name}</strong>`;
    html += `<span class="text-muted ms-2 small">(0x${cidHex})</span>`;
    html += `</div>`;
    html += `<span class="badge bg-secondary">${analysis.command}</span>`;
    html += `</div>`;

    // Summary
    if (analysis.summary) {
        html += `<div class="mb-2"><strong>Summary:</strong> ${escapeHtml(analysis.summary)}</div>`;
    }

    // Tuya Deep Analysis Block (Using structured data from backend: packet.tuya_dps)
    if (analysis.cluster_id === 0xEF00 && packet.tuya_dps && packet.tuya_dps.length > 0) {
        const dps = packet.tuya_dps;
        html += `<div class="tuya-details bg-dark p-2 rounded mb-2">`;
        html += `<div class="small text-warning mb-2"><i class="fas fa-microchip"></i> Tuya Protocol Analysis (Handler Decoded)</div>`;

        dps.forEach(dp => {
            // Get the value after conversion/scaling, or fallback to parsed value
            const finalValue = dp.parsed_value * dp.dp_def_scale + dp.dp_def_unit;

            // Format raw hex data
            let rawDataStr = dp.raw_hex;
            if (dp.dp_type === 0x02 && rawDataStr.length === 8) {
                // VALUE (4-byte integer)
                rawDataStr = `${rawDataStr.slice(0, 2)} ${rawDataStr.slice(2, 4)} ${rawDataStr.slice(4, 6)} ${rawDataStr.slice(6, 8)}`;
            } else if (dp.dp_type === 0x01 && rawDataStr.length === 2) {
                // BOOL (1-byte)
                rawDataStr = `${rawDataStr}`;
            }

            html += `<div class="dp-item border-start border-info ps-2 mb-2">`;
            html += `<div class="d-flex justify-content-between align-items-start">`;
            html += `<strong class="text-info">DP ${dp.dp_id}: ${escapeHtml(dp.dp_def_name)}</strong>`;
            html += `<span class="badge bg-info">${TUYA_DP_TYPES[dp.dp_type] || `0x${dp.dp_type.toString(16)}`} (Len: ${dp.dp_len})</span>`;
            html += `</div>`;

            // Row 1: Raw Payload and Type
            html += `<div class="row g-1 small mt-1">`;
            html += `<div class="col-md-6"><strong>Raw Hex Data:</strong> <code class="text-light text-break">${escapeHtml(rawDataStr)}</code></div>`;
            html += `<div class="col-md-6"><strong>Decoded Raw Value:</strong> <code class="text-light text-break">${escapeHtml(dp.raw_value)}</code></div>`;
            html += `</div>`;

            // Row 2: Scaled/Converted Value
            html += `<div class="row g-1 small mt-1 border-top pt-1 border-secondary border-opacity-25">`;
            html += `<div class="col-md-6 text-success"><strong>Final Value:</strong> <code class="text-success text-break">${escapeHtml(dp.parsed_value * dp.dp_def_scale + dp.dp_def_unit)}</code></div>`;

            // Show scale only if it's not 1.0
            if (dp.dp_def_scale !== 1.0) {
                html += `<div class="col-md-6 text-muted"><strong>Scaling Applied:</strong> x${dp.dp_def_scale}</div>`;
            } else {
                html += `<div class="col-md-6 text-muted"><strong>Scaling Applied:</strong> None</div>`;
            }

            html += `</div>`;
            html += `</div>`;
        });
        html += `</div>`;
    }
    // FALLBACK: Use rough client-side parsing if backend data is missing
    else if (analysis.tuya_analysis) {
        const ta = analysis.tuya_analysis;
        html += `<div class="tuya-details bg-dark p-2 rounded mb-2">`;
        html += `<div class="small text-warning mb-2"><i class="fas fa-microchip"></i> Tuya Protocol Analysis (Client-Side Fallback)</div>`;

        if (ta.sequence !== null) html += `<div class="small mb-1 text-muted">Seq: ${ta.sequence}</div>`;

        ta.dps.forEach(dp => {
            html += `<div class="dp-item border-start border-info ps-2 mb-2">`;
            html += `<div class="d-flex justify-content-between">`;
            html += `<strong class="text-info">DP ${dp.dp_id}</strong>`;
            html += `<span class="badge bg-info">${dp.dp_type_name}</span>`;
            html += `</div>`;

            html += `<div class="small mt-1">`;
            html += `<strong>${escapeHtml(dp.meaning)}:</strong> <code class="text-light">${escapeHtml(dp.value)}</code>`;
            html += `</div>`;

            if (dp.derived_states.length > 0) {
                html += `<div class="small text-success mt-1">`;
                dp.derived_states.forEach(s => html += `<div>→ ${escapeHtml(s)}</div>`);
                html += `</div>`;
            }
            html += `</div>`;
        });
        html += `</div>`;
    }


    // Recommendations / Hints
    if (analysis.recommendations.length > 0) {
        html += `<div class="recommendations border-top border-secondary pt-2 mt-2">`;
        analysis.recommendations.forEach(rec => {
            html += `<div class="small text-info">${escapeHtml(rec)}</div>`;
        });
        html += `</div>`;
    }

    html += '</div>';
    return html;
}

// =============================================================================
// 5. UTILITIES
// =============================================================================

function hexToBytes(hex) {
    const bytes = [];
    for (let i = 0; i < hex.length; i += 2) bytes.push(parseInt(hex.substr(i, 2), 16));
    return bytes;
}

function bytesToHex(bytes) {
    return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

function bytesToString(bytes) {
    return new TextDecoder().decode(new Uint8Array(bytes));
}

function escapeHtml(text) {
    if (typeof text !== 'string') return text;
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}