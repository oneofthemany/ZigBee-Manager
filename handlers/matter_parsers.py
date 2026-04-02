"""
Matter Device Parsers — cluster/attribute interpretation for Matter devices.
============================================================================

Mirrors the Zigbee handlers/ architecture:
  - BaseMatterParser: extracts common info (BasicInformation cluster 40)
  - Device-type-specific parsers: Switch, Light, Sensor, etc.
  - Quirk parsers: IKEA, Eve, Nanoleaf, etc. (override base behaviour)

The parser is selected based on device type from Descriptor cluster (29)
and optionally by vendor/model for quirks.

Usage:
    parser = get_parser_for_node(node_attributes)
    device_info = parser.parse_basic_info(attributes)
    state = parser.build_state(attributes)
    commands = parser.get_commands(attributes)
    capabilities = parser.get_capabilities(attributes)
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("matter.parsers")


# =============================================================================
# MATTER CLUSTER & ATTRIBUTE CONSTANTS
# =============================================================================

class MatterClusters:
    """Matter cluster IDs."""
    DESCRIPTOR = 29
    BINDING = 30
    ACCESS_CONTROL = 31
    BASIC_INFORMATION = 40
    OTA_SOFTWARE = 41
    LOCALIZATION = 43
    GENERAL_COMMISSIONING = 48
    NETWORK_COMMISSIONING = 49
    GENERAL_DIAGNOSTICS = 51
    SOFTWARE_DIAGNOSTICS = 52
    THREAD_DIAGNOSTICS = 53
    WIFI_DIAGNOSTICS = 54
    SWITCH = 59
    ADMIN_COMMISSIONING = 60
    OPERATIONAL_CREDENTIALS = 62
    GROUP_KEY_MANAGEMENT = 63
    BOOLEAN_STATE = 69
    ICD_MANAGEMENT = 70
    ON_OFF = 6
    LEVEL_CONTROL = 8
    COLOR_CONTROL = 768
    IDENTIFY = 3
    GROUPS = 4
    SCENES = 5
    DOOR_LOCK = 257
    WINDOW_COVERING = 258
    THERMOSTAT = 513
    FAN_CONTROL = 514
    TEMPERATURE_MEASUREMENT = 1026
    PRESSURE_MEASUREMENT = 1027
    HUMIDITY_MEASUREMENT = 1029
    OCCUPANCY_SENSING = 1030
    ILLUMINANCE_MEASUREMENT = 1024
    FLOW_MEASUREMENT = 1028


class BasicInfoAttrs:
    """BasicInformation cluster (40) attribute IDs — from the Matter spec."""
    DATA_MODEL_REVISION = 0
    VENDOR_NAME = 1
    VENDOR_ID = 2
    PRODUCT_NAME = 3
    PRODUCT_ID = 4
    NODE_LABEL = 5
    LOCATION = 6
    HARDWARE_VERSION = 7
    HARDWARE_VERSION_STRING = 8
    SOFTWARE_VERSION = 9
    SOFTWARE_VERSION_STRING = 10
    SERIAL_NUMBER = 15
    UNIQUE_ID = 18
    CAPABILITY_MINIMA = 19
    PART_NUMBER = 12          # model number string (e.g. "E2490")
    PRODUCT_APPEARANCE = 20


class SwitchAttrs:
    """Switch cluster (59) attribute IDs."""
    NUMBER_OF_POSITIONS = 0
    CURRENT_POSITION = 1
    MULTI_PRESS_MAX = 2


class DescriptorAttrs:
    """Descriptor cluster (29) attribute IDs."""
    DEVICE_TYPE_LIST = 0
    SERVER_LIST = 1
    CLIENT_LIST = 2
    PARTS_LIST = 3
    TAG_LIST = 4


# Matter device type IDs (from Descriptor cluster attr 0)
MATTER_DEVICE_TYPES = {
    6: "Bridge",
    10: "Door Lock",
    13: "Cast Video Player",
    14: "Content App",
    15: "Generic Switch",
    17: "Dimmable Light",
    18: "On/Off Light",
    19: "Dimmable Plug-In Unit",
    22: "Root Node",
    256: "On/Off Light",
    257: "Dimmable Light",
    268: "Color Temperature Light",
    269: "Extended Color Light",
    770: "Temperature Sensor",
    771: "Pressure Sensor",
    772: "Flow Sensor",
    773: "Humidity Sensor",
    774: "On/Off Sensor",
    775: "Dimmable Sensor",
    2112: "Contact Sensor",
    2128: "Occupancy Sensor",
}


# =============================================================================
# BASE MATTER PARSER
# =============================================================================

class BaseMatterParser:
    """
    Base parser for all Matter devices.
    Extracts BasicInformation and provides default state/command builders.
    """

    # Human-friendly cluster names
    CLUSTER_NAMES = {
        3: "Identify", 4: "Groups", 5: "Scenes", 6: "On/Off",
        8: "Level Control", 29: "Descriptor", 30: "Binding",
        31: "Access Control", 40: "Basic Information",
        42: "OTA Software Update Requestor", 47: "Power Source",
        48: "General Commissioning", 49: "Network Commissioning",
        51: "General Diagnostics", 52: "Software Diagnostics",
        53: "Thread Network Diagnostics", 59: "Switch",
        60: "Administrator Commissioning", 62: "Operational Credentials",
        63: "Group Key Management", 69: "Boolean State",
        70: "ICD Management",
        257: "Door Lock", 258: "Window Covering",
        513: "Thermostat", 514: "Fan Control",
        768: "Color Control",
        1024: "Illuminance Measurement", 1026: "Temperature Measurement",
        1027: "Pressure Measurement", 1028: "Flow Measurement",
        1029: "Relative Humidity", 1030: "Occupancy Sensing",
    }

    def __init__(self):
        self.device_type = "Matter"

    def find_attr(self, attributes: dict, cluster: int, attr: int,
                  default=None, endpoint: int = None) -> Any:
        """
        Find a Matter attribute value.
        Searches specific endpoint first, then EP 0, 1, 2.
        """
        eps = [endpoint] if endpoint is not None else [0, 1, 2]
        for ep in eps:
            key = f"{ep}/{cluster}/{attr}"
            if key in attributes:
                return attributes[key]
        return default

    def get_all_endpoints(self, attributes: dict) -> List[int]:
        """Get all endpoint IDs present in the attributes."""
        eps = set()
        for key in attributes:
            parts = key.split("/")
            if len(parts) == 3:
                try:
                    eps.add(int(parts[0]))
                except ValueError:
                    pass
        return sorted(eps)

    def get_clusters_for_endpoint(self, attributes: dict, ep: int) -> List[int]:
        """Get all cluster IDs on a specific endpoint."""
        clusters = set()
        prefix = f"{ep}/"
        for key in attributes:
            if key.startswith(prefix):
                parts = key.split("/")
                if len(parts) == 3:
                    try:
                        clusters.add(int(parts[1]))
                    except ValueError:
                        pass
        return sorted(clusters)

    def get_device_types(self, attributes: dict) -> List[Tuple[int, int]]:
        """Get device type list from Descriptor cluster on EP0/EP1."""
        type_list = self.find_attr(attributes, MatterClusters.DESCRIPTOR,
                                   DescriptorAttrs.DEVICE_TYPE_LIST)
        if not type_list or not isinstance(type_list, list):
            return []
        result = []
        for entry in type_list:
            if isinstance(entry, dict):
                dt = entry.get("0", entry.get(0, 0))
                rev = entry.get("1", entry.get(1, 1))
                result.append((dt, rev))
        return result

    # ── Basic Info ──────────────────────────────────────────────────

    def parse_basic_info(self, attributes: dict) -> dict:
        """Extract device identity from BasicInformation cluster."""
        return {
            "vendor_name": self.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_NAME, "Unknown"),
            "vendor_id": self.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_ID, 0),
            "product_name": self.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_NAME, ""),
            "product_id": self.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_ID, 0),
            "node_label": self.find_attr(attributes, 40, BasicInfoAttrs.NODE_LABEL, ""),
            "part_number": self.find_attr(attributes, 40, BasicInfoAttrs.PART_NUMBER, ""),
            "hardware_version": self.find_attr(attributes, 40, BasicInfoAttrs.HARDWARE_VERSION_STRING, ""),
            "software_version": self.find_attr(attributes, 40, BasicInfoAttrs.SOFTWARE_VERSION_STRING, ""),
            "serial_number": self.find_attr(attributes, 40, BasicInfoAttrs.SERIAL_NUMBER, ""),
            "location": self.find_attr(attributes, 40, BasicInfoAttrs.LOCATION, ""),
        }

    def get_manufacturer(self, attributes: dict) -> str:
        """Get manufacturer name."""
        return self.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_NAME, "Unknown")

    def get_model(self, attributes: dict) -> str:
        """Get model identifier — prefers PartNumber, falls back to ProductName."""
        part = self.find_attr(attributes, 40, BasicInfoAttrs.PART_NUMBER, "")
        if part:
            return str(part)
        return self.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_NAME, "Unknown")

    def get_friendly_name(self, attributes: dict) -> str:
        """Get display name — prefers NodeLabel, falls back to ProductName."""
        label = self.find_attr(attributes, 40, BasicInfoAttrs.NODE_LABEL, "")
        if label:
            return str(label)
        return self.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_NAME, "Matter Device")

    # ── State Building ──────────────────────────────────────────────

    def build_state(self, attributes: dict, node_id: int, available: bool) -> dict:
        """Build normalised state dict. Override in subclasses for device-specific state."""
        state = {
            "protocol": "matter",
            "available": available,
            "node_id": node_id,
        }

        # On/Off
        on_off = self.find_attr(attributes, MatterClusters.ON_OFF, 0)
        if on_off is not None:
            state["state"] = "ON" if on_off else "OFF"
            state["on"] = bool(on_off)

        # Level Control
        level = self.find_attr(attributes, MatterClusters.LEVEL_CONTROL, 0)
        if level is not None:
            state["brightness"] = int(level)
            state["level"] = int(level / 2.54) if level > 0 else 0

        # Color Temperature (mireds)
        color_temp = self.find_attr(attributes, MatterClusters.COLOR_CONTROL, 7)
        if color_temp is not None and color_temp > 0:
            state["color_temp"] = int(color_temp)

        # Color XY
        color_x = self.find_attr(attributes, MatterClusters.COLOR_CONTROL, 3)
        color_y = self.find_attr(attributes, MatterClusters.COLOR_CONTROL, 4)
        if color_x is not None and color_y is not None:
            state["color_x"] = round(color_x / 65535, 4)
            state["color_y"] = round(color_y / 65535, 4)

        # Temperature
        temp = self.find_attr(attributes, MatterClusters.TEMPERATURE_MEASUREMENT, 0)
        if temp is not None:
            state["temperature"] = round(temp / 100.0, 1)

        # Humidity
        humidity = self.find_attr(attributes, MatterClusters.HUMIDITY_MEASUREMENT, 0)
        if humidity is not None:
            state["humidity"] = round(humidity / 100.0, 1)

        # Occupancy
        occupancy = self.find_attr(attributes, MatterClusters.OCCUPANCY_SENSING, 0)
        if occupancy is not None:
            state["occupancy"] = bool(occupancy & 0x01)

        # Illuminance
        illuminance = self.find_attr(attributes, MatterClusters.ILLUMINANCE_MEASUREMENT, 0)
        if illuminance is not None:
            state["illuminance"] = int(illuminance)

        # Boolean State (contact sensor)
        contact = self.find_attr(attributes, MatterClusters.BOOLEAN_STATE, 0)
        if contact is not None:
            state["contact"] = bool(contact)

        # Window Covering
        position = self.find_attr(attributes, MatterClusters.WINDOW_COVERING, 14)
        if position is not None:
            state["position"] = 100 - position  # Matter is inverted

        # Thermostat
        local_temp = self.find_attr(attributes, MatterClusters.THERMOSTAT, 0)
        if local_temp is not None:
            state["temperature"] = round(local_temp / 100.0, 1)
        heat_sp = self.find_attr(attributes, MatterClusters.THERMOSTAT, 17)
        if heat_sp is not None:
            state["heating_setpoint"] = round(heat_sp / 100.0, 1)
        cool_sp = self.find_attr(attributes, MatterClusters.THERMOSTAT, 18)
        if cool_sp is not None:
            state["cooling_setpoint"] = round(cool_sp / 100.0, 1)
        sys_mode = self.find_attr(attributes, MatterClusters.THERMOSTAT, 27)
        if sys_mode is not None:
            state["system_mode"] = sys_mode

        # Door Lock
        lock_state = self.find_attr(attributes, MatterClusters.DOOR_LOCK, 0)
        if lock_state is not None:
            state["locked"] = lock_state == 1

        # Power Source (cluster 47)
        bat_percent = self.find_attr(attributes, 47, 12)  # BatPercentRemaining
        if bat_percent is not None:
            state["battery"] = bat_percent // 2  # Matter reports 0-200

        return state

    # ── Commands ────────────────────────────────────────────────────

    def get_commands(self, attributes: dict) -> List[dict]:
        """Get available commands based on clusters present."""
        commands = []
        eps = self.get_all_endpoints(attributes)

        for ep in eps:
            clusters = self.get_clusters_for_endpoint(attributes, ep)

            if MatterClusters.ON_OFF in clusters:
                commands.extend([
                    {"command": "on", "label": "On", "endpoint_id": ep, "cluster_id": 6},
                    {"command": "off", "label": "Off", "endpoint_id": ep, "cluster_id": 6},
                    {"command": "toggle", "label": "Toggle", "endpoint_id": ep, "cluster_id": 6},
                ])

            if MatterClusters.LEVEL_CONTROL in clusters:
                commands.append({
                    "command": "brightness", "label": "Brightness",
                    "type": "slider", "min": 0, "max": 100,
                    "endpoint_id": ep, "cluster_id": 8,
                })

            if MatterClusters.COLOR_CONTROL in clusters:
                commands.append({
                    "command": "color_temp", "label": "Color Temp",
                    "type": "slider", "min": 2000, "max": 6500,
                    "endpoint_id": ep, "cluster_id": 768,
                })

            if MatterClusters.WINDOW_COVERING in clusters:
                commands.extend([
                    {"command": "open", "label": "Open", "endpoint_id": ep, "cluster_id": 258},
                    {"command": "close", "label": "Close", "endpoint_id": ep, "cluster_id": 258},
                    {"command": "position", "label": "Position", "type": "slider",
                     "min": 0, "max": 100, "endpoint_id": ep, "cluster_id": 258},
                ])

            if MatterClusters.DOOR_LOCK in clusters:
                commands.extend([
                    {"command": "lock", "label": "Lock", "endpoint_id": ep, "cluster_id": 257},
                    {"command": "unlock", "label": "Unlock", "endpoint_id": ep, "cluster_id": 257},
                ])

            if MatterClusters.THERMOSTAT in clusters:
                commands.extend([
                    {"command": "heating_setpoint", "label": "Heat Setpoint",
                     "type": "slider", "min": 5, "max": 30, "step": 0.5,
                     "endpoint_id": ep, "cluster_id": 513},
                    {"command": "cooling_setpoint", "label": "Cool Setpoint",
                     "type": "slider", "min": 16, "max": 32, "step": 0.5,
                     "endpoint_id": ep, "cluster_id": 513},
                ])

        return commands

    # ── Capabilities ────────────────────────────────────────────────

    def get_capabilities(self, attributes: dict) -> List[str]:
        """Build capability list from clusters present across all endpoints."""
        caps = ["matter"]
        all_clusters = set()

        for ep in self.get_all_endpoints(attributes):
            all_clusters.update(self.get_clusters_for_endpoint(attributes, ep))

        cluster_cap_map = {
            MatterClusters.ON_OFF: "switch",
            MatterClusters.LEVEL_CONTROL: "level_control",
            MatterClusters.COLOR_CONTROL: "color_control",
            MatterClusters.TEMPERATURE_MEASUREMENT: "temperature_sensor",
            MatterClusters.HUMIDITY_MEASUREMENT: "humidity_sensor",
            MatterClusters.OCCUPANCY_SENSING: "motion_sensor",
            MatterClusters.ILLUMINANCE_MEASUREMENT: "illuminance_sensor",
            MatterClusters.BOOLEAN_STATE: "contact_sensor",
            MatterClusters.WINDOW_COVERING: "cover",
            MatterClusters.DOOR_LOCK: "lock",
            MatterClusters.THERMOSTAT: "thermostat",
            MatterClusters.SWITCH: "button",
        }

        for cluster_id, cap in cluster_cap_map.items():
            if cluster_id in all_clusters:
                caps.append(cap)

        # Light detection (OnOff + Level or Color)
        if MatterClusters.ON_OFF in all_clusters and (
                MatterClusters.LEVEL_CONTROL in all_clusters or
                MatterClusters.COLOR_CONTROL in all_clusters
        ):
            caps.append("light")
            if "switch" in caps:
                caps.remove("switch")

        return caps

    # ── Device Type Detection ───────────────────────────────────────

    def get_device_type(self, attributes: dict) -> str:
        """Determine high-level device type from device type list and cluster presence."""

        # Collect device types from ALL endpoints
        all_device_types = set()
        for ep in self.get_all_endpoints(attributes):
            type_list = self.find_attr(attributes, MatterClusters.DESCRIPTOR,
                                       DescriptorAttrs.DEVICE_TYPE_LIST, endpoint=ep)
            if type_list and isinstance(type_list, list):
                for entry in type_list:
                    if isinstance(entry, dict):
                        dt = entry.get("0", entry.get(0, 0))
                        all_device_types.add(dt)

        # Collect clusters from non-zero endpoints (EP0 is infrastructure only)
        all_clusters = set()
        for ep in self.get_all_endpoints(attributes):
            if ep == 0:
                continue
            all_clusters.update(self.get_clusters_for_endpoint(attributes, ep))

        # ── Priority 1: Input devices (remotes, buttons, dials) ─────
        # These CONTROL other devices — even if the descriptor says "light",
        # a remote that controls lights is still a button.
        INPUT_DEVICE_TYPES = {
            15,    # Generic Switch (buttons, remotes, dials)
            2080,  # Generic Switch (alternate)
            259,   # On/Off Light Switch
            260,   # Dimmer Switch
            261,   # Color Dimmer Switch
            2128,  # On/Off Sensor (occupancy-based switch)
        }
        if all_device_types & INPUT_DEVICE_TYPES:
            return "Button"
        if MatterClusters.SWITCH in all_clusters:
            return "Button"

        # ── Priority 2: Sensors (report data, no actuation) ─────────
        SENSOR_DEVICE_TYPES = {
            770,   # Temperature Sensor
            771,   # Pressure Sensor
            772,   # Flow Sensor
            773,   # Humidity Sensor
            2112,  # Contact Sensor
            2128,  # Occupancy Sensor
            263,   # Occupancy Sensor (legacy)
            775,   # Dimmable Sensor
            774,   # On/Off Sensor
            44,    # Air Quality Sensor
            2144,  # Rain Sensor
            2145,  # Water Freeze Detector
            2146,  # Water Leak Detector
            2147,  # Smoke CO Alarm
        }
        if all_device_types & SENSOR_DEVICE_TYPES:
            return "Sensor"
        SENSOR_CLUSTERS = {
            MatterClusters.TEMPERATURE_MEASUREMENT,
            MatterClusters.HUMIDITY_MEASUREMENT,
            MatterClusters.PRESSURE_MEASUREMENT,
            MatterClusters.ILLUMINANCE_MEASUREMENT,
            MatterClusters.FLOW_MEASUREMENT,
            MatterClusters.OCCUPANCY_SENSING,
            MatterClusters.BOOLEAN_STATE,
        }
        # Only classify as sensor if it has sensor clusters but NO actuator clusters
        if all_clusters & SENSOR_CLUSTERS and not all_clusters & {
            MatterClusters.ON_OFF, MatterClusters.LEVEL_CONTROL,
            MatterClusters.WINDOW_COVERING, MatterClusters.DOOR_LOCK,
            MatterClusters.THERMOSTAT,
        }:
            return "Sensor"

        # ── Priority 3: HVAC ────────────────────────────────────────
        HVAC_DEVICE_TYPES = {
            769,   # Thermostat
            43,    # Fan
            45,    # Air Purifier
            46,    # Room Air Conditioner
        }
        if all_device_types & HVAC_DEVICE_TYPES:
            return "Thermostat"
        if MatterClusters.THERMOSTAT in all_clusters:
            return "Thermostat"
        if MatterClusters.FAN_CONTROL in all_clusters:
            return "Fan"

        # ── Priority 4: Covers (blinds, shades, awnings) ───────────
        COVER_DEVICE_TYPES = {
            514,   # Window Covering
            515,   # Window Covering Controller
        }
        if all_device_types & COVER_DEVICE_TYPES:
            return "Cover"
        if MatterClusters.WINDOW_COVERING in all_clusters:
            return "Cover"

        # ── Priority 5: Locks ───────────────────────────────────────
        LOCK_DEVICE_TYPES = {
            10,    # Door Lock
            11,    # Door Lock Controller
        }
        if all_device_types & LOCK_DEVICE_TYPES:
            return "Lock"
        if MatterClusters.DOOR_LOCK in all_clusters:
            return "Lock"

        # ── Priority 6: Lights ──────────────────────────────────────
        LIGHT_DEVICE_TYPES = {
            256,   # On/Off Light
            257,   # Dimmable Light
            268,   # Color Temperature Light
            269,   # Extended Color Light
            17,    # Dimmable Light (legacy)
            18,    # On/Off Light (legacy)
            19,    # Dimmable Plug-In Unit
        }
        if all_device_types & LIGHT_DEVICE_TYPES:
            if MatterClusters.ON_OFF in all_clusters:
                return "Light"

        # ── Priority 7: Switches / Plugs / Outlets ──────────────────
        SWITCH_DEVICE_TYPES = {
            266,   # On/Off Plug-In Unit
            267,   # Dimmable Plug-In Unit
            16,    # Power Source (smart plug)
        }
        if all_device_types & SWITCH_DEVICE_TYPES:
            return "Switch"
        if MatterClusters.ON_OFF in all_clusters:
            if MatterClusters.LEVEL_CONTROL in all_clusters:
                return "Light"
            return "Switch"

        # ── Priority 8: Media / Speakers ────────────────────────────
        MEDIA_DEVICE_TYPES = {
            34,    # Speaker
            35,    # Cast Video Player
            36,    # Cast Video Client
            40,    # Basic Video Player
            41,    # Casting Video Client
            43,    # Media Player
        }
        if all_device_types & MEDIA_DEVICE_TYPES:
            return "Media"

        # ── Priority 9: Bridge / Infrastructure ─────────────────────
        INFRA_DEVICE_TYPES = {
            14,    # Aggregator
            17,    # Bridge
            22,    # Root Node
        }
        if all_device_types & INFRA_DEVICE_TYPES:
            # Don't return "Bridge" — these are infrastructure endpoints,
            # the actual device type should be detected from other endpoints
            pass

        return "Matter"

    # ── Device Event Detection ───────────────────────────────────────

    def parse_event(self, event_name: str, endpoint_id: int,
                    cluster_id: int, event_data: dict) -> str:
        """Parse a Matter cluster event into a human-readable action string.
        Override in subclasses for device-specific event handling."""

        # Dispatch to cluster-specific parsers
        parser_method = self.EVENT_PARSERS.get(cluster_id)
        if parser_method:
            return parser_method(self, event_name, endpoint_id, event_data)

        # General fallback
        return f"cluster_{cluster_id}_{event_name.lower()}_ep{endpoint_id}"

    # Cluster event parsers — subclasses can extend this dict
    EVENT_PARSERS = {}


    # ── Switch cluster (59) ───────────────────────────────────────────

    @staticmethod
    def _parse_switch_event(self, event_name, endpoint_id, event_data):
        action = event_name.lower().replace(" ", "_")
        event_map = {
            "initialpress": "press", "initial_press": "press",
            "longpress": "hold", "long_press": "hold",
            "shortrelease": "single", "short_release": "single",
            "longrelease": "release", "long_release": "release",
            "multipressongoing": "multi_press",
            "multipresscomplete": "multi", "multi_press_complete": "multi",
        }
        action = event_map.get(action, action)
        if "multi" in action:
            count = event_data.get("totalNumberOfPressesCounted",
                                   event_data.get("total_number_of_presses_counted", 0))
            if count == 2: action = "double"
            elif count == 3: action = "triple"
            elif count > 3: action = f"multi_{count}"
        return f"button_{endpoint_id}_{action}"

    # ── Door Lock cluster (257) ───────────────────────────────────────

    @staticmethod
    def _parse_lock_event(self, event_name, endpoint_id, event_data):
        lock_events = {
            "doorlockalarm": "alarm", "doorstatechange": "state_change",
            "lockoperation": "operation", "lockoperationerror": "error",
        }
        return f"lock_{lock_events.get(event_name.lower(), event_name.lower())}"

    # ── Boolean State cluster (69) ────────────────────────────────────

    @staticmethod
    def _parse_boolean_event(self, event_name, endpoint_id, event_data):
        val = event_data.get("stateValue", event_data.get("state_value"))
        return "contact_open" if val else "contact_closed"

    # ── Smoke CO Alarm cluster (92) ───────────────────────────────────

    @staticmethod
    def _parse_alarm_event(self, event_name, endpoint_id, event_data):
        return f"alarm_{event_name.lower()}"

    # Register all cluster event parsers
    EVENT_PARSERS = {
        59: _parse_switch_event,
        69: _parse_boolean_event,
        92: _parse_alarm_event,
        257: _parse_lock_event,
    }


# =============================================================================
# SWITCH PARSER (buttons, remotes, dials)
# =============================================================================

class SwitchParser(BaseMatterParser):
    """Parser for Matter Switch devices (buttons, remotes, scroll wheels)."""

    def __init__(self):
        super().__init__()
        self.device_type = "Button"

    def build_state(self, attributes: dict, node_id: int, available: bool) -> dict:
        state = super().build_state(attributes, node_id, available)

        # Collect switch info from all endpoints
        endpoints_with_switch = []
        for ep in self.get_all_endpoints(attributes):
            clusters = self.get_clusters_for_endpoint(attributes, ep)
            if MatterClusters.SWITCH in clusters:
                positions = self.find_attr(attributes, MatterClusters.SWITCH,
                                           SwitchAttrs.NUMBER_OF_POSITIONS, 2, endpoint=ep)
                current = self.find_attr(attributes, MatterClusters.SWITCH,
                                         SwitchAttrs.CURRENT_POSITION, 0, endpoint=ep)
                multi_press = self.find_attr(attributes, MatterClusters.SWITCH,
                                             SwitchAttrs.MULTI_PRESS_MAX, 0, endpoint=ep)
                endpoints_with_switch.append({
                    "endpoint": ep,
                    "positions": positions,
                    "current_position": current,
                    "multi_press_max": multi_press,
                })

        if endpoints_with_switch:
            state["switch_endpoints"] = endpoints_with_switch
            state["button_count"] = len(endpoints_with_switch)
            # Report first button's position
            state["current_position"] = endpoints_with_switch[0]["current_position"]

        # Battery from Power Source cluster
        bat = self.find_attr(attributes, 47, 12)  # BatPercentRemaining
        if bat is not None:
            state["battery"] = bat // 2

        return state

    def get_commands(self, attributes: dict) -> List[dict]:
        """Switch devices don't accept commands — they emit events."""
        return [
            {"command": "identify", "label": "Identify", "endpoint_id": 1, "cluster_id": 3},
        ]


# =============================================================================
# LIGHT PARSER
# =============================================================================

class LightParser(BaseMatterParser):
    """Parser for Matter Light devices."""

    def __init__(self):
        super().__init__()
        self.device_type = "Light"


# =============================================================================
# SENSOR PARSER
# =============================================================================

class SensorParser(BaseMatterParser):
    """Parser for Matter Sensor devices (temperature, humidity, occupancy, etc.)."""

    def __init__(self):
        super().__init__()
        self.device_type = "Sensor"


# =============================================================================
# IKEA QUIRK PARSER
# =============================================================================

class IkeaParser(BaseMatterParser):
    """Quirk parser for IKEA Matter devices."""

    def get_friendly_name(self, attributes: dict) -> str:
        """IKEA devices often have empty NodeLabel — use ProductName."""
        label = self.find_attr(attributes, 40, BasicInfoAttrs.NODE_LABEL, "")
        if label:
            return str(label)
        product = self.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_NAME, "")
        if product:
            return str(product)
        part = self.find_attr(attributes, 40, BasicInfoAttrs.PART_NUMBER, "")
        if part:
            return f"IKEA {part}"
        return "IKEA Device"


class IkeaSwitchParser(IkeaParser, SwitchParser):
    """IKEA buttons — override event parsing for IKEA-specific behaviour."""

    def parse_event(self, event_name, endpoint_id, cluster_id, event_data):
        action = super().parse_event(event_name, endpoint_id, cluster_id, event_data)
        # IKEA BILRESA EP2 is the rotary — rename accordingly
        if endpoint_id == 2 and "button_2" in action:
            action = action.replace("button_2", "dial")
        return action


# =============================================================================
# PARSER REGISTRY & AUTO-DETECTION
# =============================================================================

# Vendor ID → quirk parser mapping
VENDOR_QUIRKS = {
    4476: IkeaParser,      # IKEA
    # 4874: EveParser,     # Eve Systems (future)
    # 4999: NanoleafParser, # Nanoleaf (future)
}

# Vendor + device type → specific quirk parser
VENDOR_DEVICE_QUIRKS = {
    (4476, "Button"): IkeaSwitchParser,
    # (4476, "Light"): IkeaLightParser,  # future
}


def get_parser_for_node(attributes: dict) -> BaseMatterParser:
    """
    Auto-detect the best parser for a Matter node based on its attributes.

    Priority:
      1. Vendor + device type specific quirk
      2. Vendor quirk
      3. Device type parser
      4. Base parser (fallback)
    """
    base = BaseMatterParser()

    # Detect vendor
    vendor_id = base.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_ID, 0)

    # Detect device type
    device_type = base.get_device_type(attributes)

    # 1. Vendor + device type specific
    quirk_key = (vendor_id, device_type)
    if quirk_key in VENDOR_DEVICE_QUIRKS:
        parser = VENDOR_DEVICE_QUIRKS[quirk_key]()
        logger.info(f"Using vendor+type parser: {parser.__class__.__name__} "
                    f"(vendor={vendor_id}, type={device_type})")
        return parser

    # 2. Vendor quirk (creates appropriate device type parser with vendor mixin)
    if vendor_id in VENDOR_QUIRKS:
        vendor_parser = VENDOR_QUIRKS[vendor_id]
        # Combine vendor quirk with device type parser
        type_parsers = {
            "Button": SwitchParser,
            "Light": LightParser,
            "Sensor": SensorParser,
        }
        type_parser = type_parsers.get(device_type, BaseMatterParser)

        # Dynamic mixin: VendorQuirk + TypeParser
        combined = type(
            f"{vendor_parser.__name__}_{type_parser.__name__}",
            (vendor_parser, type_parser),
            {}
        )
        parser = combined()
        logger.info(f"Using combined parser: {parser.__class__.__name__} "
                    f"(vendor={vendor_id}, type={device_type})")
        return parser

    # 3. Device type parser
    type_parsers = {
        "Button": SwitchParser,
        "Light": LightParser,
        "Sensor": SensorParser,
    }
    if device_type in type_parsers:
        parser = type_parsers[device_type]()
        logger.info(f"Using type parser: {parser.__class__.__name__} (type={device_type})")
        return parser

    # 4. Fallback
    logger.info(f"Using base parser (vendor={vendor_id}, type={device_type})")
    return base