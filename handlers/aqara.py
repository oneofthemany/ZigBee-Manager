"""
Aqara/Xiaomi specific handlers.
Handles: Buttons, Cubes, Vibration sensors (MultistateInput)
"""
import logging
import struct
from typing import Any, Dict, List

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.aqara")


# ============================================================
# XIAOMI STRUCTURED ATTRIBUTE PARSER
# ============================================================
def parse_xiaomi_struct(data: bytes) -> dict:
    """
    Parse Xiaomi/Aqara structured attribute data (typically attribute 0x00DF or 0x00F7).

    Format: Each sub-attribute is encoded as:
      - 1 byte: sub-attribute ID
      - 1 byte: data type (0x10=bool, 0x20=uint8, 0x21=uint16, 0x23=uint32, 0x28=int8, etc)
      - N bytes: value (length depends on type)

    Returns dict of {sub_attr_id: value}
    """
    result = {}
    pos = 0

    while pos < len(data):
        if pos + 2 > len(data):
            break

        sub_attr_id = data[pos]
        data_type = data[pos + 1]
        pos += 2

        try:
            if data_type == 0x10:  # Bool
                if pos + 1 > len(data): break
                value = bool(data[pos])
                pos += 1
            elif data_type == 0x20:  # uint8
                if pos + 1 > len(data): break
                value = data[pos]
                pos += 1
            elif data_type == 0x21:  # uint16 LE
                if pos + 2 > len(data): break
                value = struct.unpack('<H', data[pos:pos+2])[0]
                pos += 2
            elif data_type == 0x23:  # uint32 LE
                if pos + 4 > len(data): break
                value = struct.unpack('<I', data[pos:pos+4])[0]
                pos += 4
            elif data_type == 0x25:  # uint48 LE
                if pos + 6 > len(data): break
                value = int.from_bytes(data[pos:pos+6], 'little')
                pos += 6
            elif data_type == 0x28:  # int8 LE
                if pos + 1 > len(data): break
                value = struct.unpack('<b', data[pos:pos+1])[0]
                pos += 1
            elif data_type == 0x29:  # int16 LE
                if pos + 2 > len(data): break
                value = struct.unpack('<h', data[pos:pos+2])[0]
                pos += 2
            elif data_type == 0x2B:  # int32 LE
                if pos + 4 > len(data): break
                value = struct.unpack('<i', data[pos:pos+4])[0]
                pos += 4
            elif data_type == 0x39:  # float
                if pos + 4 > len(data): break
                value = struct.unpack('<f', data[pos:pos+4])[0]
                pos += 4
            elif data_type in (0x41, 0x42):  # octet/char string
                if pos + 1 > len(data): break
                str_len = data[pos]
                pos += 1
                if pos + str_len > len(data): break
                value = data[pos:pos+str_len]
                if data_type == 0x42:
                    value = value.decode('utf-8', errors='ignore')
                pos += str_len
            else:
                logger.debug(f"Unknown Xiaomi data type: 0x{data_type:02X}")
                break

            result[sub_attr_id] = value
        except Exception as e:
            logger.error(f"Error parsing Xiaomi struct at pos {pos}: {e}")
            break

    return result


# Map sub-attribute IDs to meaningful names and converters
XIAOMI_ATTR_MAP = {
    0x01: ("battery_voltage_mV", lambda v: v),  # mV
    0x03: ("device_temperature", lambda v: v),  # Celsius
    0x04: ("power", lambda v: v / 10.0),  # 0.1W units
    0x05: ("voltage", lambda v: v / 10.0),  # 0.1V units
    0x06: ("current", lambda v: v / 1000.0),  # mA to A
    0x07: ("consumption", lambda v: v),  # Energy
    0x08: ("power_factor", lambda v: v),
    0x09: ("frequency", lambda v: v / 10.0),  # 0.1 Hz
    0x64: ("switch_state", lambda v: bool(v)),
    0x95: ("power_consumption", lambda v: v),
    0x96: ("voltage_96", lambda v: v),
    0x97: ("current_97", lambda v: v),
    0x98: ("power_98", lambda v: v),
    0x9A: ("energy", lambda v: v),
    0x9B: ("indicator_mode", lambda v: v),
    0x0152: ("trigger_indicator", lambda v: bool(v)),
}


# ============================================================
# MULTISTATE INPUT CLUSTER (0x0012)
# Used by: Aqara Buttons, Cube, Vibration Sensor
# ============================================================
@register_handler(0x0012)
class MultistateInputHandler(ClusterHandler):
    """
    Handles Multistate Input cluster (0x0012).
    Used by Aqara buttons to report clicks (single, double, hold, etc).
    """
    CLUSTER_ID = 0x0012
    REPORT_CONFIG = [
        ("present_value", 0, 3600, 1),  # Report immediately on change
    ]

    ATTR_PRESENT_VALUE = 0x0055

    # Aqara Button mappings (approximate, varies by model)
    # For WXKG11LM / WXKG12LM etc.
    ACTION_MAP = {
        0: "hold",
        1: "single",
        2: "double",
        3: "triple",
        4: "quadruple",
        16: "hold",
        17: "release",
        18: "shake",
        255: "release"
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_PRESENT_VALUE:
            if hasattr(value, 'value'): value = value.value

            # Lookup action name
            action_name = self.ACTION_MAP.get(value, f"action_{value}")

            logger.info(f"[{self.device.ieee}] Aqara Button Action: {action_name} (val={value})")

            # Update state with the last action
            # We use 'action' state which is standard for buttons in HA
            self.device.update_state({
                "action": action_name,
                "multistate_value": value
            })

            # Emit an event so we can trigger automations
            self.device.emit_event("button_press", {
                "action": action_name,
                "value": value
            })

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_PRESENT_VALUE:
            return "action_value"
        return super().get_attr_name(attrid)

    def get_discovery_configs(self) -> list:
        """Generate HA discovery for the button action."""
        return [
            {
                "component": "sensor",
                "object_id": "action",
                "config": {
                    "name": "Action",
                    "icon": "mdi:gesture-tap-button",
                    "value_template": "{{ value_json.action }}"
                }
            }
        ]


# ============================================================
# ANALOG INPUT CLUSTER (0x000C)
# Used by: Aqara Cube (Rotation) or Vibration Sensor
# ============================================================
@register_handler(0x000C)
class AqaraAnalogInputHandler(ClusterHandler):
    CLUSTER_ID = 0x000C
    ATTR_PRESENT_VALUE = 0x0055

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_PRESENT_VALUE:
            if hasattr(value, 'value'): value = value.value
            self.device.update_state({"analog_input": value})
            logger.debug(f"[{self.device.ieee}] Aqara Analog: {value}")


# ============================================================
# AQARA MANUFACTURER SPECIFIC CLUSTER (0xFCC0)
# Used by: Aqara TRV E1, Thermostats, Motion Sensors, Switches, etc.
# ============================================================
@register_handler(0xFCC0)
class AqaraManufacturerCluster(ClusterHandler):
    """
    Handles Aqara/Xiaomi manufacturer-specific cluster (0xFCC0).

    This cluster is manufacturer code 0x115F (LUMI/Aqara).
    It provides proprietary attributes for various Aqara devices:
    - Thermostats/TRVs: Window detection, child lock, valve calibration
    - Motion sensors: Detection interval, sensitivity, trigger indicator
    - Switches: Decoupled mode, power outage memory, indicator light
    - Temperature/Humidity sensors: Display unit, measurement interval

    Based on ZHA's XiaomiAqaraE1Cluster and OppleCluster patterns.
    """
    CLUSTER_ID = 0xFCC0
    MANUFACTURER_CODE = 0x115F  # LUMI/Aqara manufacturer code

    # ===== Common Attributes (multiple device types) =====
    ATTR_MODE = 0x0009                  # uint8 - Device mode
    ATTR_POWER_OUTAGE_MEM = 0x0201      # Bool - Power outage memory

    # ===== Switch/Relay Attributes =====
    ATTR_OPERATION_MODE = 0x0200        # uint8 - 0=Decoupled, 1=Coupled
    ATTR_SWITCH_MODE = 0x0004           # uint8 - 1=Fast, 2=Multi
    ATTR_SWITCH_TYPE = 0x000A           # uint8 - 1=Toggle, 2=Momentary
    ATTR_INDICATOR_LIGHT = 0x00F0       # uint8 - 0=Normal, 1=Reverse

    # ===== Motion Sensor Attributes =====
    ATTR_DETECTION_INTERVAL = 0x0102    # uint8 - Seconds between detections
    ATTR_MOTION_SENSITIVITY = 0x010C    # uint8 - 1=Low, 2=Medium, 3=High
    ATTR_TRIGGER_INDICATOR = 0x0152     # uint8 - 0=Off, 1=On

    # ===== Thermostat/TRV Attributes (E1: lumi.airrtc.agl001) =====
    ATTR_SYSTEM_MODE = 0x0271           # uint8 - System mode
    ATTR_WINDOW_DETECTION = 0x0272      # uint8 - 1=On, 0=Off
    ATTR_VALVE_DETECTION = 0x0273       # uint8 - 1=On, 0=Off
    ATTR_CHILD_LOCK = 0x0274            # uint8 - 1=Locked, 0=Unlocked
    ATTR_BATTERY_REPLACE = 0x0276       # uint8 - Battery low indicator
    ATTR_WINDOW_OPEN = 0x0277           # uint8 - 1=Open, 0=Closed (status)
    ATTR_VALVE_ALARM = 0x0278           # uint8 - Valve error status
    ATTR_MOTOR_CALIBRATION = 0x0279     # uint8 - 1=Start Calibration
    ATTR_SCHEDULE = 0x027B              # Data - Schedule programming
    ATTR_SENSOR_TYPE = 0x027C           # uint8 - Internal/External sensor
    ATTR_EXTERNAL_TEMP_INPUT = 0x027D   # int16 - External temp in 0.01°C

    # ===== Temperature/Humidity Sensor Attributes =====
    ATTR_TEMP_DISPLAY_UNIT = 0xFF01     # uint8 - 0=Celsius, 1=Fahrenheit
    ATTR_MEASUREMENT_INTERVAL = 0x00EF  # uint16 - Measurement interval seconds

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        """
        Handle attribute updates from the Aqara manufacturer cluster.
        Parses and updates device state based on attribute ID.
        """
        if hasattr(value, 'value'):
            value = value.value

        updates = {}

        # === Thermostat/TRV Attributes ===
        if attrid == self.ATTR_WINDOW_DETECTION:
            updates["window_detection"] = bool(value)
            logger.info(f"[{self.device.ieee}] Window detection: {'enabled' if value else 'disabled'}")

        elif attrid == self.ATTR_VALVE_DETECTION:
            updates["valve_detection"] = bool(value)
            logger.info(f"[{self.device.ieee}] Valve detection: {'enabled' if value else 'disabled'}")

        elif attrid == self.ATTR_CHILD_LOCK:
            updates["child_lock"] = bool(value)
            logger.info(f"[{self.device.ieee}] Child lock: {'locked' if value else 'unlocked'}")

        elif attrid == self.ATTR_WINDOW_OPEN:
            updates["window_open"] = bool(value)
            logger.info(f"[{self.device.ieee}] Window: {'open' if value else 'closed'}")

        elif attrid == self.ATTR_MOTOR_CALIBRATION:
            status = "calibrating" if value == 1 else "idle"
            updates["motor_calibration"] = status
            logger.info(f"[{self.device.ieee}] Motor calibration: {status}")

        elif attrid == self.ATTR_VALVE_ALARM:
            updates["valve_alarm"] = bool(value)
            if value:
                logger.warning(f"[{self.device.ieee}] Valve alarm triggered!")

        elif attrid == self.ATTR_SENSOR_TYPE:
            sensor_type = "external" if value == 1 else "internal"
            updates["sensor_type"] = sensor_type
            logger.info(f"[{self.device.ieee}] Sensor type: {sensor_type}")

        elif attrid == self.ATTR_EXTERNAL_TEMP_INPUT:
            # External temp in 0.01Â°C units
            temp_celsius = value / 100.0 if value != -32768 else None
            if temp_celsius is not None:
                updates["external_temperature"] = temp_celsius
                logger.debug(f"[{self.device.ieee}] External temp: {temp_celsius}Â°C")

        # === Switch/Relay Attributes ===
        elif attrid == self.ATTR_OPERATION_MODE:
            mode = "decoupled" if value == 0 else "coupled"
            updates["operation_mode"] = mode
            logger.info(f"[{self.device.ieee}] Operation mode: {mode}")

        elif attrid == self.ATTR_SWITCH_MODE:
            mode_map = {1: "fast", 2: "multi"}
            mode = mode_map.get(value, f"unknown_{value}")
            updates["switch_mode"] = mode
            logger.info(f"[{self.device.ieee}] Switch mode: {mode}")

        elif attrid == self.ATTR_SWITCH_TYPE:
            type_map = {1: "toggle", 2: "momentary"}
            switch_type = type_map.get(value, f"unknown_{value}")
            updates["switch_type"] = switch_type
            logger.info(f"[{self.device.ieee}] Switch type: {switch_type}")

        elif attrid == self.ATTR_INDICATOR_LIGHT:
            mode = "reverse" if value == 1 else "normal"
            updates["indicator_light"] = mode
            logger.info(f"[{self.device.ieee}] Indicator light: {mode}")

        elif attrid == self.ATTR_POWER_OUTAGE_MEM:
            updates["power_outage_memory"] = bool(value)
            logger.info(f"[{self.device.ieee}] Power outage memory: {'on' if value else 'off'}")

        # === Motion Sensor Attributes ===
        elif attrid == self.ATTR_DETECTION_INTERVAL:
            updates["detection_interval"] = value
            logger.info(f"[{self.device.ieee}] Detection interval: {value}s")

        elif attrid == self.ATTR_MOTION_SENSITIVITY:
            sens_map = {1: "low", 2: "medium", 3: "high"}
            sensitivity = sens_map.get(value, f"unknown_{value}")
            updates["motion_sensitivity"] = sensitivity
            logger.info(f"[{self.device.ieee}] Motion sensitivity: {sensitivity}")

        elif attrid == self.ATTR_TRIGGER_INDICATOR:
            updates["trigger_indicator"] = bool(value)
            logger.info(f"[{self.device.ieee}] Trigger indicator: {'on' if value else 'off'}")

        # === Common Attributes ===
        elif attrid == self.ATTR_MODE:
            updates["device_mode"] = value
            logger.debug(f"[{self.device.ieee}] Device mode: {value}")

        elif attrid == self.ATTR_BATTERY_REPLACE:
            updates["battery_low"] = bool(value)
            if value:
                logger.warning(f"[{self.device.ieee}] Battery replacement needed!")

        # === Temperature/Humidity Display ===
        elif attrid == self.ATTR_TEMP_DISPLAY_UNIT:
            unit = "fahrenheit" if value == 1 else "celsius"
            updates["temperature_unit"] = unit
            logger.info(f"[{self.device.ieee}] Temperature unit: {unit}")

        elif attrid == self.ATTR_MEASUREMENT_INTERVAL:
            updates["measurement_interval"] = value
            logger.info(f"[{self.device.ieee}] Measurement interval: {value}s")

        # === Xiaomi Structured Attributes (0x00DF and 0x00F7) ===
        elif attrid in (0x00DF, 0x00F7):
            # These are packed structs with multiple sub-attributes
            # 0x00DF: Common device telemetry (power, voltage, current)
            # 0x00F7: Device-specific settings and status
            if isinstance(value, bytes):
                try:
                    parsed = parse_xiaomi_struct(value)

                    # Convert to human-readable attributes
                    for sub_id, sub_value in parsed.items():
                        if sub_id in XIAOMI_ATTR_MAP:
                            attr_name, converter = XIAOMI_ATTR_MAP[sub_id]
                            try:
                                converted_value = converter(sub_value)
                                updates[attr_name] = converted_value
                                logger.info(f"[{self.device.ieee}] {attr_name}={converted_value}")
                            except Exception as e:
                                logger.error(f"[{self.device.ieee}] Error converting {attr_name}: {e}")
                        else:
                            # Unknown sub-attribute, log for debugging
                            logger.debug(f"[{self.device.ieee}] Unknown Xiaomi sub-attr 0x{sub_id:02X} = {sub_value}")
                            # Don't store as raw attribute to keep device state clean
                except Exception as e:
                    logger.error(f"[{self.device.ieee}] Error parsing Xiaomi struct 0x{attrid:04X}: {e}")
            else:
                # Not binary data, just store it
                logger.debug(f"[{self.device.ieee}] Aqara 0x{attrid:04X} non-bytes value: {value}")
                updates[f"opple_0x{attrid:04x}"] = value

        else:
            # Unknown attribute - log for debugging (but don't log 0x00DF/0x00F7 here)
            logger.debug(f"[{self.device.ieee}] Aqara 0xFCC0 unknown attr 0x{attrid:04x} = {value}")
            # Store with opple prefix for visibility
            updates[f"opple_0x{attrid:04x}"] = value

        # Update device state
        if updates:
            self.device.update_state(updates)

    async def configure(self):
        """
        Configure the Aqara manufacturer cluster.

        - Does NOT bind (0xFCC0 is manufacturer-specific, binding not needed)
        - Reads initial attribute values with manufacturer code 0x115F
        - Follows ZHA pattern: no binding, just initial read
        """
        logger.info(f"[{self.device.ieee}] Configuring Aqara manufacturer cluster 0xFCC0")
        await self.poll()
        return True

    async def poll(self) -> Dict[str, Any]:
        """
        Poll manufacturer-specific attributes.
        Must use manufacturer_code=0x115F for Aqara devices.
        """
        # Determine which attributes to read based on device type
        # Try to read common attributes first
        attrs_to_read = [
            self.ATTR_POWER_OUTAGE_MEM,  # Common across many devices
        ]

        # Add thermostat-specific attributes if we have a thermostat cluster
        if hasattr(self.device, 'hvac'):
            attrs_to_read.extend([
                self.ATTR_WINDOW_DETECTION,
                self.ATTR_CHILD_LOCK,
                self.ATTR_VALVE_DETECTION,
                self.ATTR_WINDOW_OPEN,
                self.ATTR_MOTOR_CALIBRATION,
                self.ATTR_VALVE_ALARM,
                self.ATTR_SENSOR_TYPE,
            ])

        # Add motion sensor attributes if we have occupancy cluster
        if hasattr(self.device, 'occupancy'):
            attrs_to_read.extend([
                self.ATTR_DETECTION_INTERVAL,
                self.ATTR_MOTION_SENSITIVITY,
                self.ATTR_TRIGGER_INDICATOR,
            ])

        # Add switch attributes if we have on/off cluster
        if hasattr(self.device, 'on_off'):
            attrs_to_read.extend([
                self.ATTR_OPERATION_MODE,
                self.ATTR_SWITCH_MODE,
                self.ATTR_SWITCH_TYPE,
                self.ATTR_INDICATOR_LIGHT,
            ])

        try:
            logger.debug(f"[{self.device.ieee}] Reading Aqara attrs: {[hex(a) for a in attrs_to_read]}")

            # CRITICAL: Must specify manufacturer=0x115F for Aqara devices
            result = await self.cluster.read_attributes(
                attrs_to_read,
                manufacturer=self.MANUFACTURER_CODE
            )

            # Parse results: result is (success_dict, failure_dict)
            if result and result[0]:
                success_attrs = result[0]
                logger.info(f"[{self.device.ieee}] Aqara poll success: {len(success_attrs)} attrs")
                for attrid, value in success_attrs.items():
                    self.attribute_updated(attrid, value)

            if result and result[1]:
                failed_attrs = result[1]
                logger.debug(f"[{self.device.ieee}] Aqara poll failures: {failed_attrs}")

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Aqara manufacturer cluster poll failed: {e}")

        return {}

    async def write_attribute(self, attr_id: int, value: Any) -> bool:
        """
        Write an attribute with manufacturer code.

        Args:
            attr_id: Attribute ID to write
            value: Value to write

        Returns:
            True if successful
        """
        try:
            logger.info(f"[{self.device.ieee}] Writing Aqara attr 0x{attr_id:04x} = {value}")

            # CRITICAL: Must use manufacturer code for Aqara writes
            result = await self.cluster.write_attributes(
                {attr_id: value},
                manufacturer=self.MANUFACTURER_CODE
            )

            # result is list of write_attribute_record
            if result and result[0].status == 0:  # SUCCESS
                logger.info(f"[{self.device.ieee}] Aqara write success: 0x{attr_id:04x}")
                # Read back to confirm
                await self.read_attribute(attr_id)
                return True
            else:
                logger.error(f"[{self.device.ieee}] Aqara write failed: {result}")
                return False

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Aqara write exception: {e}")
            return False

    async def read_attribute(self, attr_id: int) -> Any:
        """Read a single attribute with manufacturer code."""
        try:
            result = await self.cluster.read_attributes(
                [attr_id],
                manufacturer=self.MANUFACTURER_CODE
            )
            if result and result[0]:
                value = result[0].get(attr_id)
                if value is not None:
                    self.attribute_updated(attr_id, value)
                    return value
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Aqara read 0x{attr_id:04x} failed: {e}")
        return None


    async def apply_configuration(self, updates: Dict[str, Any]):
        """Apply Aqara manufacturer-specific configuration updates."""
        config_map = {
            'power_outage_memory': self.ATTR_POWER_OUTAGE_MEM,
            'window_detection': self.ATTR_WINDOW_DETECTION,
            'child_lock': self.ATTR_CHILD_LOCK,
            'valve_detection': self.ATTR_VALVE_DETECTION,
            'motor_calibration': self.ATTR_MOTOR_CALIBRATION,
            'detection_interval': self.ATTR_DETECTION_INTERVAL,
            'motion_sensitivity': self.ATTR_MOTION_SENSITIVITY,
            'trigger_indicator': self.ATTR_TRIGGER_INDICATOR,
            'operation_mode': self.ATTR_OPERATION_MODE,
            'switch_mode': self.ATTR_SWITCH_MODE,
            'switch_type': self.ATTR_SWITCH_TYPE,
            'indicator_light': self.ATTR_INDICATOR_LIGHT,
        }

        for key, attr_id in config_map.items():
            if key in updates:
                try:
                    success = await self.write_attribute(attr_id, int(updates[key]))
                    if not success:
                        logger.debug(f"[{self.device.ieee}] Device doesn't support {key}")
                except Exception as e:
                    logger.debug(f"[{self.device.ieee}] Skipping unsupported {key}: {e}")

    def get_pollable_attributes(self) -> Dict[int, str]:
        """
        Return pollable attributes based on device type.
        Used by periodic polling if enabled.
        """
        base_attrs = {
            self.ATTR_POWER_OUTAGE_MEM: "power_outage_memory",
        }

        # Add device-specific attributes
        if hasattr(self.device, 'hvac'):  # Thermostat/TRV
            base_attrs.update({
                self.ATTR_WINDOW_DETECTION: "window_detection",
                self.ATTR_CHILD_LOCK: "child_lock",
                self.ATTR_VALVE_DETECTION: "valve_detection",
                self.ATTR_MOTOR_CALIBRATION: "motor_calibration",
                self.ATTR_WINDOW_OPEN: "window_open",
                self.ATTR_VALVE_ALARM: "valve_alarm",
            })

        if hasattr(self.device, 'occupancy'):  # Motion sensor
            base_attrs.update({
                self.ATTR_DETECTION_INTERVAL: "detection_interval",
                self.ATTR_MOTION_SENSITIVITY: "motion_sensitivity",
                self.ATTR_TRIGGER_INDICATOR: "trigger_indicator",
            })

        if hasattr(self.device, 'on_off'):  # Switch/Relay
            base_attrs.update({
                self.ATTR_OPERATION_MODE: "operation_mode",
                self.ATTR_INDICATOR_LIGHT: "indicator_light",
            })

        return base_attrs


    async def set_window_detection(self, enabled: bool):
        await self.write_attribute(
            self.ATTR_WINDOW_DETECTION,
            1 if enabled else 0
        )


    async def set_valve_detection(self, enabled: bool):
        await self.write_attribute(
            self.ATTR_VALVE_DETECTION,
            1 if enabled else 0
        )


    async def start_motor_calibration(self):
        """
        Starts valve motor calibration.
        Takes ~2 minutes, auto-resets to 0.
        """
        await self.write_attribute(
            self.ATTR_MOTOR_CALIBRATION,
            1
        )


    def process_command(self, command: str, value: Any):
        loop = self.device.loop

        if command == "motor_calibration":
            loop.create_task(self.start_motor_calibration())

        elif command == "window_detection":
            loop.create_task(self.set_window_detection(bool(value)))

        elif command == "valve_detection":
            loop.create_task(self.set_valve_detection(bool(value)))


    def get_configuration_options(self) -> List[Dict]:
        """
        Expose Aqara manufacturer-specific settings to the UI.
        Returns configuration options based on device capabilities.
        """
        options = []

        # === Thermostat/TRV Configuration ===
        if hasattr(self.device, 'hvac'):
            options.extend([
                {
                    "name": "window_detection",
                    "label": "Window Detection",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Disabled"},
                        {"value": 1, "label": "Enabled"}
                    ],
                    "description": "Automatically turn off heating when window is detected open",
                    "attribute_id": self.ATTR_WINDOW_DETECTION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "child_lock",
                    "label": "Child Lock",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Unlocked"},
                        {"value": 1, "label": "Locked"}
                    ],
                    "description": "Lock physical controls on device",
                    "attribute_id": self.ATTR_CHILD_LOCK,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "valve_detection",
                    "label": "Valve Detection",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Disabled"},
                        {"value": 1, "label": "Enabled"}
                    ],
                    "description": "Detect and report valve errors",
                    "attribute_id": self.ATTR_VALVE_DETECTION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "motor_calibration",
                    "label": "Valve Calibration",
                    "type": "button",
                    "action_value": 1,
                    "description": "Calibrate valve motor (takes ~2 minutes)",
                    "attribute_id": self.ATTR_MOTOR_CALIBRATION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "sensor_type",
                    "label": "Temperature Sensor",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Internal Sensor"},
                        {"value": 1, "label": "External Sensor"}
                    ],
                    "description": "Use internal or external temperature sensor",
                    "attribute_id": self.ATTR_SENSOR_TYPE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # === Motion Sensor Configuration ===
        if hasattr(self.device, 'occupancy'):
            options.extend([
                {
                    "name": "detection_interval",
                    "label": "Detection Interval",
                    "type": "number",
                    "min": 5,
                    "max": 300,
                    "unit": "seconds",
                    "description": "Minimum time between motion detections",
                    "attribute_id": self.ATTR_DETECTION_INTERVAL,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "motion_sensitivity",
                    "label": "Motion Sensitivity",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Low"},
                        {"value": 2, "label": "Medium"},
                        {"value": 3, "label": "High"}
                    ],
                    "description": "Motion detection sensitivity",
                    "attribute_id": self.ATTR_MOTION_SENSITIVITY,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "trigger_indicator",
                    "label": "LED Indicator",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Off"},
                        {"value": 1, "label": "On"}
                    ],
                    "description": "Flash LED when motion detected",
                    "attribute_id": self.ATTR_TRIGGER_INDICATOR,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # === Switch/Relay Configuration ===
        if hasattr(self.device, 'on_off'):
            options.extend([
                {
                    "name": "operation_mode",
                    "label": "Operation Mode",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Decoupled (Switch independent)"},
                        {"value": 1, "label": "Coupled (Switch controls relay)"}
                    ],
                    "description": "Decoupled mode allows switch to trigger automations without controlling relay",
                    "attribute_id": self.ATTR_OPERATION_MODE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "switch_mode",
                    "label": "Switch Mode",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Fast (Quick response)"},
                        {"value": 2, "label": "Multi (Support multi-press)"}
                    ],
                    "description": "Fast mode for immediate response, Multi for detecting double/triple press",
                    "attribute_id": self.ATTR_SWITCH_MODE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "switch_type",
                    "label": "Switch Type",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Toggle"},
                        {"value": 2, "label": "Momentary"}
                    ],
                    "attribute_id": self.ATTR_SWITCH_TYPE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "indicator_light",
                    "label": "Indicator Light",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Normal (On when relay on)"},
                        {"value": 1, "label": "Reverse (On when relay off)"}
                    ],
                    "attribute_id": self.ATTR_INDICATOR_LIGHT,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # === Common Configuration ===
        options.append({
            "name": "power_outage_memory",
            "label": "Power Outage Memory",
            "type": "select",
            "options": [
                {"value": 0, "label": "Off (Reset to default)"},
                {"value": 1, "label": "On (Remember last state)"}
            ],
            "description": "Remember device state after power loss",
            "attribute_id": self.ATTR_POWER_OUTAGE_MEM,
            "manufacturer_code": self.MANUFACTURER_CODE
        })

        return options

    def get_discovery_configs(self) -> list:
        """
        Generate Home Assistant MQTT discovery configs.
        Creates sensors for status attributes and configuration entities.
        """
        configs = []

        # Window open status sensor (for TRVs)
        if hasattr(self.device, 'hvac'):
            configs.append({
                "component": "binary_sensor",
                "object_id": "window_open",
                "config": {
                    "name": "Window Open",
                    "device_class": "window",
                    "value_template": "{{ value_json.window_open }}",
                    "payload_on": True,
                    "payload_off": False
                }
            })

            configs.append({
                "component": "binary_sensor",
                "object_id": "valve_alarm",
                "config": {
                    "name": "Valve Alarm",
                    "device_class": "problem",
                    "value_template": "{{ value_json.valve_alarm }}",
                    "payload_on": True,
                    "payload_off": False
                }
            })

        return configs