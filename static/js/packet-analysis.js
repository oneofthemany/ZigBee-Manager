/**
 * Packet Analyser - Deep Packet Inspection for Zigbee Messages
 * ============================================================
 * fully fledged implementation of ZCL (Zigbee Cluster Library) analysis.
 * Covers Global Commands, Cluster Specific Commands, and Tuya Protocols.
 *
 * Now also decodes individual attributes in Report Attributes / Read
 * Attributes Response payloads into human-readable values (centidegree
 * temperatures, percentages, enums, etc.) for the side-by-side debug view.
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

// ZCL Data Type Codes (ZCL spec section 2.6.2)
const ZCL_DATA_TYPES = {
    0x00: "no data",
    0x08: "data8",
    0x09: "data16",
    0x10: "boolean",
    0x18: "bitmap8",
    0x19: "bitmap16",
    0x1B: "bitmap32",
    0x20: "uint8",
    0x21: "uint16",
    0x22: "uint24",
    0x23: "uint32",
    0x24: "uint40",
    0x25: "uint48",
    0x27: "uint64",
    0x28: "int8",
    0x29: "int16",
    0x2A: "int24",
    0x2B: "int32",
    0x2F: "int64",
    0x30: "enum8",
    0x31: "enum16",
    0x39: "single (float)",
    0x41: "octstr",
    0x42: "string",
    0x43: "long octstr",
    0x44: "long string",
    0x48: "array",
    0x4C: "struct",
    0xE0: "ToD",
    0xE1: "date",
    0xE2: "UTC",
    0xE8: "clusterId",
    0xE9: "attribId",
    0xEA: "bacOID",
    0xF0: "EUI64",
    0xF1: "key128",
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
        0x0021: "BatteryPercentageRemaining",
        0x0031: "BatterySize",
        0x0033: "BatteryQuantity",
        0x0034: "BatteryRatedVoltage",
        0x0035: "BatteryAlarmMask",
        0x0036: "BatteryVoltageMinThreshold"
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
        0x0008: "PIHeatingDemand",
        0x0011: "OccupiedCoolingSetpoint",
        0x0012: "OccupiedHeatingSetpoint",
        0x0015: "MinHeatSetpointLimit",
        0x0016: "MaxHeatSetpointLimit",
        0x001B: "ControlSequenceOfOperation",
        0x001C: "SystemMode",
        0x001E: "RunningMode",
        0x0023: "TemperatureSetpointHold",
        0x0029: "RunningState",
    },
    0x0204: { // Thermostat UI Config
        0x0000: "TemperatureDisplayMode",
        0x0001: "KeypadLockout",
    },
    0x0400: { 0x0000: "MeasuredValue" }, // Illuminance
    0x0402: {                            // Temperature
        0x0000: "MeasuredValue",
        0x0001: "MinMeasuredValue",
        0x0002: "MaxMeasuredValue",
        0x0003: "Tolerance",
    },
    0x0403: { 0x0000: "MeasuredValue" }, // Pressure
    0x0405: { 0x0000: "MeasuredValue" }, // Humidity
    0x0406: { 0x0000: "Occupancy" },     // Occupancy
    0x0500: { // IAS Zone
        0x0000: "ZoneState",
        0x0001: "ZoneType",
        0x0002: "ZoneStatus",
    },
    0x0019: { // OTA Upgrade
        0x0000: "UpgradeServerID",
        0x0001: "FileOffset",
        0x0002: "CurrentFileVersion",
        0x0006: "ImageUpgradeStatus",
    },
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

// Enum decoders for known cluster+attribute combinations.
// Returned strings are appended to the human-readable value display.
const ENUM_DECODERS = {
    // Basic - PowerSource
    "0x0000:0x0007": {
        0x00: "Unknown",
        0x01: "Mains (single phase)",
        0x02: "Mains (3 phase)",
        0x03: "Battery",
        0x04: "DC source",
        0x05: "Emergency mains constant power",
        0x06: "Emergency mains transfer switch",
    },
    // Thermostat - SystemMode (0x0201:0x001C)
    "0x0201:0x001c": {
        0x00: "Off",
        0x01: "Auto",
        0x03: "Cool",
        0x04: "Heat",
        0x05: "Emergency Heating",
        0x06: "Precooling",
        0x07: "Fan only",
        0x08: "Dry",
        0x09: "Sleep",
    },
    // Thermostat - RunningMode (0x0201:0x001E)
    "0x0201:0x001e": {
        0x00: "Off",
        0x03: "Cool",
        0x04: "Heat",
    },
    // Thermostat - ControlSequenceOfOperation (0x0201:0x001B)
    "0x0201:0x001b": {
        0x00: "Cooling Only",
        0x01: "Cooling With Reheat",
        0x02: "Heating Only",
        0x03: "Heating With Reheat",
        0x04: "Cooling and Heating",
        0x05: "Cooling and Heating with Reheat",
    },
    // Thermostat - TemperatureSetpointHold (0x0201:0x0023)
    "0x0201:0x0023": {
        0x00: "Setpoint Hold Off",
        0x01: "Setpoint Hold On",
    },
    // Window Covering - WindowCoveringType (0x0102:0x0000)
    "0x0102:0x0000": {
        0x00: "Roller Shade",
        0x01: "Roller Shade (2 motor)",
        0x02: "Roller Shade Exterior",
        0x03: "Roller Shade Exterior (2 motor)",
        0x04: "Drapery",
        0x05: "Awning",
        0x06: "Shutter",
        0x07: "Tilt Blind (Tilt Only)",
        0x08: "Tilt Blind (Lift and Tilt)",
        0x09: "Projector Screen",
    },
    // Thermostat UI Config - TemperatureDisplayMode (0x0204:0x0000)
    "0x0204:0x0000": {
        0x00: "Celsius",
        0x01: "Fahrenheit",
    },
    // Thermostat UI Config - KeypadLockout (0x0204:0x0001)
    "0x0204:0x0001": {
        0x00: "No lockout",
        0x01: "Level 1",
        0x02: "Level 2",
        0x03: "Level 3",
        0x04: "Level 4",
        0x05: "Level 5",
    },
    // IAS Zone - ZoneState (0x0500:0x0000)
    "0x0500:0x0000": {
        0x00: "Not Enrolled",
        0x01: "Enrolled",
    },
    // IAS Zone - ZoneType (0x0500:0x0001) - common values only
    "0x0500:0x0001": {
        0x0000: "Standard CIE",
        0x000D: "Motion Sensor",
        0x0015: "Contact Switch",
        0x0028: "Fire Sensor",
        0x002A: "Water Sensor",
        0x002B: "Carbon Monoxide Sensor",
        0x002C: "Personal Emergency Device",
        0x002D: "Vibration / Movement Sensor",
        0x010F: "Remote Control",
        0x0115: "Key Fob",
        0x021D: "Keypad",
        0x0225: "Standard Warning Device",
        0x0226: "Glass Break Sensor",
        0x0229: "Security Repeater",
    },
};

// Bitmap decoders. Returns array of active flag names.
const BITMAP_DECODERS = {
    // IAS Zone - ZoneStatus (0x0500:0x0002)
    "0x0500:0x0002": [
        [1 << 0, "Alarm1"],
        [1 << 1, "Alarm2"],
        [1 << 2, "Tamper"],
        [1 << 3, "Battery Low"],
        [1 << 4, "Supervision Reports"],
        [1 << 5, "Restore Reports"],
        [1 << 6, "Trouble"],
        [1 << 7, "AC Mains Fault"],
        [1 << 8, "Test Mode"],
        [1 << 9, "Battery Defect"],
    ],
    // Occupancy Sensing - Occupancy (0x0406:0x0000)
    "0x0406:0x0000": [
        [1 << 0, "Occupied"],
    ],
    // Thermostat - RunningState (0x0201:0x0029)
    "0x0201:0x0029": [
        [1 << 0, "Heat On"],
        [1 << 1, "Cool On"],
        [1 << 2, "Fan On"],
        [1 << 3, "Heat 2nd Stage On"],
        [1 << 4, "Cool 2nd Stage On"],
        [1 << 5, "Fan 2nd Stage On"],
        [1 << 6, "Fan 3rd Stage On"],
    ],
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
 * analyse a single Tuya DP
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
// 2b. ZCL ATTRIBUTE INTERPRETATION
// =============================================================================

/**
 * Convert a single ZCL attribute report entry into a human-readable
 * interpretation object suitable for the side-by-side debug view.
 *
 * Input shape (from packet.decoded.attributes):
 *   { id: "0x0000", name: "0x0000", type: "0x29", value: 2176 }
 *
 * Output shape:
 *   {
 *     id: "0x0000",
 *     attr_name: "MeasuredValue",
 *     type_hex: "0x29",
 *     type_name: "int16",
 *     raw_value: 2176,
 *     display_value: "21.76 °C",
 *     interpretation: "Ambient temperature (centidegrees → °C)",
 *     extra: ["..."]
 *   }
 */
function interpretZclAttribute(clusterId, attr) {
    const idNum = typeof attr.id === 'string' ? parseInt(attr.id, 16) : attr.id;
    const typeNum = typeof attr.type === 'string' ? parseInt(attr.type, 16) : attr.type;
    const idHex = `0x${(idNum || 0).toString(16).padStart(4, '0')}`;
    const typeHex = `0x${(typeNum || 0).toString(16).padStart(2, '0')}`;
    const cidHex = `0x${(clusterId || 0).toString(16).padStart(4, '0')}`;

    const attrName = (COMMON_ATTRIBUTES[clusterId] && COMMON_ATTRIBUTES[clusterId][idNum])
        || `Attribute ${idHex}`;
    const typeName = ZCL_DATA_TYPES[typeNum] || `type ${typeHex}`;

    const result = {
        id: idHex,
        attr_name: attrName,
        type_hex: typeHex,
        type_name: typeName,
        raw_value: attr.value,
        display_value: formatRawValue(attr.value),
        interpretation: "",
        extra: [],
    };

    const key = `${cidHex}:${idHex.toLowerCase()}`;

    // 1) Cluster-specific scaled values (centidegree, percent halved, etc.)
    if (clusterId === 0x0402 && idNum === 0x0000) {
        // Temperature MeasuredValue (int16 centidegrees C)
        if (attr.value === -32768) {
            result.display_value = "Invalid / unavailable (0x8000)";
            result.interpretation = "Ambient temperature";
        } else {
            result.display_value = `${(attr.value / 100).toFixed(2)} °C`;
            result.interpretation = "Ambient temperature (centidegrees → °C)";
        }
    } else if (clusterId === 0x0402 && (idNum === 0x0001 || idNum === 0x0002)) {
        result.display_value = `${(attr.value / 100).toFixed(2)} °C`;
        result.interpretation = `${attrName} (centidegrees → °C)`;
    } else if (clusterId === 0x0402 && idNum === 0x0003) {
        // Tolerance (uint16, centidegrees)
        result.display_value = `±${(attr.value / 100).toFixed(2)} °C`;
        result.interpretation = "Sensor tolerance (centidegrees → °C)";
    } else if (clusterId === 0x0405 && idNum === 0x0000) {
        // Humidity MeasuredValue (uint16, centi-%)
        result.display_value = `${(attr.value / 100).toFixed(2)} %RH`;
        result.interpretation = "Relative humidity (centi-% → %RH)";
    } else if (clusterId === 0x0403 && idNum === 0x0000) {
        // Pressure MeasuredValue (int16, kPa)
        result.display_value = `${attr.value} kPa (~${(attr.value * 10).toFixed(0)} hPa)`;
        result.interpretation = "Atmospheric pressure (kPa)";
    } else if (clusterId === 0x0400 && idNum === 0x0000) {
        // Illuminance MeasuredValue (uint16): lux = 10^((value-1)/10000)
        if (attr.value === 0) {
            result.display_value = "0 lx (too dark)";
        } else if (attr.value === 0xFFFF) {
            result.display_value = "Invalid / unavailable";
        } else {
            const lux = Math.pow(10, (attr.value - 1) / 10000);
            result.display_value = `${attr.value} → ${lux.toFixed(1)} lx`;
        }
        result.interpretation = "Illuminance (logarithmic scale)";
    } else if (clusterId === 0x0201 && (idNum === 0x0000 || idNum === 0x0011 || idNum === 0x0012 ||
                                        idNum === 0x0015 || idNum === 0x0016)) {
        // Thermostat temperatures (int16 centidegrees)
        if (attr.value === -32768) {
            result.display_value = "Invalid / unavailable (0x8000)";
        } else {
            result.display_value = `${(attr.value / 100).toFixed(2)} °C`;
        }
        result.interpretation = `${attrName} (centidegrees → °C)`;
    } else if (clusterId === 0x0201 && idNum === 0x0008) {
        // PIHeatingDemand (uint8, 0-100)
        result.display_value = `${attr.value} %`;
        result.interpretation = "Heating demand (proportional valve open)";
    } else if (clusterId === 0x0001 && idNum === 0x0020) {
        // BatteryVoltage (uint8, in 100 mV)
        result.display_value = `${(attr.value / 10).toFixed(1)} V`;
        result.interpretation = "Battery voltage (100 mV units)";
    } else if (clusterId === 0x0001 && idNum === 0x0021) {
        // BatteryPercentageRemaining (uint8, 0–200; spec encodes half-percent)
        if (attr.value === 0xFF) {
            result.display_value = "Invalid / unknown";
        } else {
            result.display_value = `${(attr.value / 2).toFixed(1)} %`;
        }
        result.interpretation = "Battery remaining (raw is half-percent)";
    } else if (clusterId === 0x0006 && idNum === 0x0000) {
        // OnOff (boolean)
        result.display_value = attr.value ? "ON" : "OFF";
        result.interpretation = "On/Off state";
    } else if (clusterId === 0x0008 && idNum === 0x0000) {
        // CurrentLevel (uint8, 0–254)
        const pct = (attr.value / 254 * 100).toFixed(0);
        result.display_value = `${attr.value}/254 (~${pct} %)`;
        result.interpretation = "Brightness / level";
    } else if (clusterId === 0x0102 && (idNum === 0x0008 || idNum === 0x0009)) {
        // Window Covering position (uint8, percent)
        result.display_value = `${attr.value} %`;
        result.interpretation = `${attrName} (% closed)`;
    }

    // 2) Enum decode (after unit conversions, in case both apply — none currently do)
    if (ENUM_DECODERS[key] && typeof attr.value === 'number') {
        const enumName = ENUM_DECODERS[key][attr.value];
        if (enumName) {
            result.display_value = `${attr.value} → ${enumName}`;
            if (!result.interpretation) result.interpretation = "Enumerated value";
        }
    }

    // 3) Bitmap decode
    if (BITMAP_DECODERS[key] && typeof attr.value === 'number') {
        const flags = BITMAP_DECODERS[key]
            .filter(([mask]) => (attr.value & mask) !== 0)
            .map(([, name]) => name);
        const flagStr = flags.length ? flags.join(', ') : '(none)';
        const valueHex = `0x${attr.value.toString(16).padStart(4, '0')}`;
        result.display_value = `${valueHex} → ${flagStr}`;
        if (!result.interpretation) result.interpretation = "Bitmap flags";
    }

    // 4) Type-driven fallbacks for anything not covered above
    if (!result.interpretation) {
        if (typeNum === 0x10) {
            result.display_value = attr.value ? "true" : "false";
            result.interpretation = "Boolean";
        } else if (typeNum === 0x42 || typeNum === 0x44) {
            result.interpretation = "Character string";
        } else if (typeNum === 0x41 || typeNum === 0x43) {
            result.interpretation = "Octet string";
        } else if (typeNum === 0x30 || typeNum === 0x31) {
            result.interpretation = "Enumerated value (no decoder for this attribute)";
        } else if (typeNum === 0x18 || typeNum === 0x19 || typeNum === 0x1B) {
            const valueHex = `0x${(attr.value || 0).toString(16)}`;
            result.display_value = `${attr.value} (${valueHex})`;
            result.interpretation = "Bitmap (no decoder for this attribute)";
        } else if (typeNum >= 0x20 && typeNum <= 0x2F) {
            result.interpretation = "Numeric value";
        } else {
            result.interpretation = `Raw ${typeName}`;
        }
    }

    return result;
}

function formatRawValue(v) {
    if (v === null || v === undefined) return "(null)";
    if (typeof v === 'number') return v.toString();
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch { return String(v); }
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
        tuya_analysis: null, // Will hold the structured data from the backend
        attribute_reports: [], // List of decoded ZCL attribute interpretations
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
        const attrs = packet.decoded?.attributes || [];
        if (attrs.length > 0) {
            analysis.attribute_reports = attrs.map(a => interpretZclAttribute(cid, a));

            // Build a one-line summary from the decoded attributes.
            const parts = analysis.attribute_reports.map(r =>
                `${r.attr_name}=${r.display_value}`
            );
            analysis.summary = `${analysis.cluster_name} Report: ${parts.join(', ')}`;
        } else {
            analysis.summary = `${analysis.cluster_name} Report (no attributes parsed)`;
            if (COMMON_ATTRIBUTES[cid]) {
                analysis.recommendations.push(
                    `ℹ️ This cluster usually reports: ${Object.values(COMMON_ATTRIBUTES[cid]).join(', ')}`
                );
            }
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

/**
 * Render the human-readable side of the debug view.
 * The caller (logging.js) now wraps this in a 2-column layout next to the
 * raw JSON, so this function only emits the "decoded" panel.
 */
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
    const cmdHex = (analysis.command_id >= 0)
        ? `0x${analysis.command_id.toString(16).padStart(2, '0')}`
        : '?';

    let html = '<div class="packet-analysis border-start border-3 border-primary ps-3">';

    // Header
    html += `<div class="d-flex justify-content-between align-items-start mb-2 flex-wrap gap-1">`;
    html += `<div>`;
    html += `<strong>${escapeHtml(analysis.cluster_name)}</strong>`;
    html += `<span class="text-muted ms-2 small">(0x${cidHex})</span>`;
    html += `</div>`;
    html += `<span class="badge bg-secondary">${escapeHtml(analysis.command)} <span class="text-white-50 ms-1">${cmdHex}</span></span>`;
    html += `</div>`;

    // Summary
    if (analysis.summary) {
        html += `<div class="mb-2"><strong>Summary:</strong> ${escapeHtml(analysis.summary)}</div>`;
    }

    // --- A. Decoded ZCL attribute reports (Read Response / Report Attributes) ---
    if (analysis.attribute_reports && analysis.attribute_reports.length > 0) {
        html += `<div class="zcl-attrs bg-dark p-2 rounded mb-2">`;
        html += `<div class="small text-warning mb-2"><i class="fas fa-list-ul"></i> Decoded Attributes</div>`;

        analysis.attribute_reports.forEach(r => {
            html += `<div class="dp-item border-start border-info ps-2 mb-2">`;
            html += `<div class="d-flex justify-content-between align-items-start flex-wrap gap-1">`;
            html += `<strong class="text-info">${escapeHtml(r.attr_name)} <span class="text-muted small ms-1">${r.id}</span></strong>`;
            html += `<span class="badge bg-info">${escapeHtml(r.type_name)} <span class="text-white-50 ms-1">${r.type_hex}</span></span>`;
            html += `</div>`;

            html += `<div class="row g-1 small mt-1">`;
            html += `<div class="col-md-6"><strong>Raw:</strong> <code class="text-light text-break">${escapeHtml(formatRawValue(r.raw_value))}</code></div>`;
            html += `<div class="col-md-6 text-success"><strong>Decoded:</strong> <code class="text-success text-break">${escapeHtml(r.display_value)}</code></div>`;
            html += `</div>`;

            if (r.interpretation) {
                html += `<div class="small text-muted mt-1"><i class="fas fa-info-circle me-1"></i>${escapeHtml(r.interpretation)}</div>`;
            }

            if (r.extra && r.extra.length > 0) {
                r.extra.forEach(e => {
                    html += `<div class="small text-muted mt-1">${escapeHtml(e)}</div>`;
                });
            }
            html += `</div>`;
        });
        html += `</div>`;
    }

    // --- B. Tuya Deep Analysis (backend-decoded) ---
    if (analysis.cluster_id === 0xEF00 && packet.tuya_dps && packet.tuya_dps.length > 0) {
        const dps = packet.tuya_dps;
        html += `<div class="tuya-details bg-dark p-2 rounded mb-2">`;
        html += `<div class="small text-warning mb-2"><i class="fas fa-microchip"></i> Tuya Protocol Analysis (Handler Decoded)</div>`;

        dps.forEach(dp => {
            // Format raw hex data
            let rawDataStr = dp.raw_hex;
            if (dp.dp_type === 0x02 && rawDataStr.length === 8) {
                // VALUE (4-byte integer)
                rawDataStr = `${rawDataStr.slice(0, 2)} ${rawDataStr.slice(2, 4)} ${rawDataStr.slice(4, 6)} ${rawDataStr.slice(6, 8)}`;
            }

            html += `<div class="dp-item border-start border-info ps-2 mb-2">`;
            html += `<div class="d-flex justify-content-between align-items-start flex-wrap gap-1">`;
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
    // --- C. Tuya client-side fallback ---
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
    if (text === null || text === undefined) return '';
    if (typeof text !== 'string') text = String(text);
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}