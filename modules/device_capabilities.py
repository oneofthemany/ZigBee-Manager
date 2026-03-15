import logging
from typing import Set, Dict, Any, Optional

# Zigpy Imports for standard Cluster IDs
from zigpy.zcl.clusters.general import (
    Basic, PowerConfiguration, DeviceTemperature, Identify,
    Groups, Scenes, OnOff, LevelControl, Alarms, Time, AnalogInput,
    BinaryInput, MultistateInput
)
from zigpy.zcl.clusters.closures import WindowCovering
from zigpy.zcl.clusters.hvac import Thermostat, Fan, Dehumidification, UserInterface
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.clusters.measurement import (
    IlluminanceMeasurement, IlluminanceLevelSensing, TemperatureMeasurement,
    PressureMeasurement, FlowMeasurement, RelativeHumidity, OccupancySensing,
    LeafWetness, SoilMoisture
)
from zigpy.zcl.clusters.security import IasZone
from zigpy.zcl.clusters.smartenergy import Metering
from zigpy.zcl.clusters.homeautomation import ElectricalMeasurement

LOGGER = logging.getLogger(__name__)

class DeviceCapabilities:
    """
    Production-grade capability detection and state filtering.
    analyses Zigbee clusters and applies manufacturer quirks contextually to determine
    supported features, ensuring 'generic' quirks don't override specific hardware clusters.
    """

    # === STATE FIELD CATEGORIES ===
    MOTION_FIELDS = {
        'motion', 'occupancy', 'presence',
        'motion_on_time', 'motion_timeout', 'sensitivity',
        'pir_o_to_u_delay', 'pir_u_to_o_delay', 'pir_u_to_o_threshold'
    }

    CONTACT_FIELDS = {
        'contact', 'is_open', 'is_closed', 'alarm_1', 'alarm_2'
    }

    IAS_ZONE_FIELDS = {
        'zone_status', 'tamper', 'battery_low', 'trouble',
        'water_leak', 'smoke', 'co_detected', 'vibration', 'alarm'
    }

    LIGHTING_FIELDS = {
        'state', 'on', 'brightness', 'level',
        'color_temp', 'color_temp_kelvin', 'color_temp_startup',
        'color_x', 'color_y', 'hue', 'saturation',
        'color_mode', 'enhanced_hue', 'color_loop_active',
        'color_loop_direction', 'color_loop_time', 'transition_time'
    }

    HVAC_FIELDS = {
        'local_temperature', 'occupied_heating_setpoint',
        'occupied_cooling_setpoint', 'unoccupied_heating_setpoint',
        'unoccupied_cooling_setpoint', 'min_heat_setpoint_limit',
        'max_heat_setpoint_limit', 'min_cool_setpoint_limit',
        'max_cool_setpoint_limit', 'system_mode', 'running_mode',
        'running_state', 'pi_heating_demand', 'pi_cooling_demand',
        'hvac_system_type', 'thermostat_programming_operation_mode',
        'occupied_setback', 'unoccupied_setback', 'setpoint_change_source',
        'valve_position', 'window_detection', 'child_lock', 'away_mode',
        'preset', 'swing_mode', 'fan_mode'
    }

    POWER_FIELDS = {
        'power', 'voltage', 'current', 'energy',
        'power_factor', 'reactive_power', 'apparent_power',
        'rms_voltage', 'rms_current', 'active_power',
        'ac_frequency', 'power_divisor', 'power_multiplier',
        'daily_energy', 'monthly_energy'
    }

    ENVIRONMENTAL_FIELDS = {
        'temperature', 'humidity', 'pressure',
        'illuminance', 'illuminance_lux', 'co2', 'pm25',
        'voc', 'formaldehyde', 'air_quality', 'soil_moisture'
    }

    BATTERY_FIELDS = {
        'battery', 'battery_voltage', 'battery_percentage_remaining',
        'battery_size', 'battery_quantity', 'battery_alarm_mask'
    }

    COVER_FIELDS = {
        'position', 'tilt', 'lift_percentage', 'tilt_percentage',
        'current_position_lift_percentage', 'current_position_tilt_percentage',
        'cover_position', 'moving'
    }

    # Updated to include 'radar_state' and other radar specific fields
    TUYA_RADAR_FIELDS = {
        'radar_state', 'radar_sensitivity', 'presence_sensitivity', 'keep_time',
        'distance', 'detection_distance_min', 'detection_distance_max',
        'fading_time', 'self_test', 'target_distance', 'illuminance',
        "presence"
    }

    # Fields that are ALWAYS allowed regardless of capability
    UNIVERSAL_FIELDS = {
        'last_seen', 'power_source', 'manufacturer', 'model',
        'available', 'lqi', 'rssi', 'sw_version', 'date_code',
        'application_version', 'stack_version', 'hw_version',
        'manufacturer_id', 'power_source_raw', 'device_type',
        'linkquality', 'update_available', 'update_state', 'action',
        'ieee', 'nwk', 'friendly_name', 'device_type',
        'multistate_value', 'on_with_timed_off', 'device_temperature'
    }

    # === CLUSTER IDs - COMPLETE LIST ===
    BASIC = 0x0000
    POWER_CONFIGURATION = 0x0001
    DEVICE_TEMPERATURE = 0x0002
    IDENTIFY = 0x0003
    GROUPS = 0x0004
    SCENES = 0x0005
    ON_OFF = 0x0006
    ON_OFF_CONFIGURATION = 0x0007
    LEVEL_CONTROL = 0x0008
    ALARMS = 0x0009
    TIME = 0x000A
    ANALOG_INPUT = 0x000C
    ANALOG_OUTPUT = 0x000D
    ANALOG_VALUE = 0x000E
    BINARY_INPUT = 0x000F
    BINARY_OUTPUT = 0x0010
    BINARY_VALUE = 0x0011
    MULTISTATE_INPUT = 0x0012
    MULTISTATE_OUTPUT = 0x0013
    MULTISTATE_VALUE = 0x0014

    # Closures
    SHADE_CONFIGURATION = 0x0100
    DOOR_LOCK = 0x0101
    WINDOW_COVERING = 0x0102

    # HVAC
    THERMOSTAT = 0x0201
    FAN_CONTROL = 0x0202
    DEHUMIDIFICATION_CONTROL = 0x0203
    THERMOSTAT_UI = 0x0204

    # Lighting
    COLOR_CONTROL = 0x0300
    BALLAST_CONFIGURATION = 0x0301

    # Measurement & Sensing
    ILLUMINANCE_MEASUREMENT = 0x0400
    ILLUMINANCE_LEVEL_SENSING = 0x0401
    TEMPERATURE_MEASUREMENT = 0x0402
    PRESSURE_MEASUREMENT = 0x0403
    FLOW_MEASUREMENT = 0x0404
    RELATIVE_HUMIDITY = 0x0405
    OCCUPANCY_SENSING = 0x0406
    LEAF_WETNESS = 0x0407
    SOIL_MOISTURE = 0x0408
    CO2_MEASUREMENT = 0x040D
    PM25_MEASUREMENT = 0x042A

    # Security & Safety
    IAS_ZONE = 0x0500
    IAS_ACE = 0x0501
    IAS_WD = 0x0502

    # Smart Energy
    PRICE = 0x0700
    DEMAND_RESPONSE = 0x0701
    METERING = 0x0702
    MESSAGING = 0x0703
    TUNNELING = 0x0704
    PREPAYMENT = 0x0705
    ENERGY_MANAGEMENT = 0x0706

    # Lighting Link (Touchlink)
    TOUCHLINK = 0x1000

    # Measurement & Sensing (cont.)
    ELECTRICAL_MEASUREMENT = 0x0B04
    DIAGNOSTICS = 0x0B05

    # Manufacturer Specific
    TUYA_MANUFACTURER = 0xEF00
    XIAOMI_MANUFACTURER = 0xFCC0
    XIAOMI_AQARA = 0xFCC0
    PHILIPS_MANUFACTURER = 0xFC00


    # ========================================================================
    # COMPREHENSIVE CLUSTER CONFIGURATION MATRIX
    # ========================================================================

    # Clusters that NEVER support configuration (system/infrastructure)
    NEVER_CONFIGURABLE = {
        0x0000,  # Basic (read-only device info)
        0x0003,  # Identify (UI feedback only)
        0x0004,  # Groups (binding target, not source)
        0x0005,  # Scenes (binding target, not source)
        0x0007,  # OnOff Configuration (config storage, not reporting)
        0x0009,  # Alarms (event-driven)
        0x000A,  # Time (usually OUTPUT only)
        0x0013,  # Multistate Output (command-driven)
        0x0019,  # OTA (firmware updates)
        0x0020,  # Poll Control (deprecated)
        0x0021,  # Green Power (proxy/commissioning only)
        0x0100,  # Shade Configuration (settings, not state)
        0x0101,  # Door Lock (command-driven)
        0x0204,  # Thermostat UI Config (settings, not state)
        0x0301,  # Ballast Configuration (settings, not state)
        0x0401,  # Illuminance Level Sensing (thresholds, not measurement)
        0x0501,  # IAS ACE (command interface)
        0x0502,  # IAS WD (warning device commands)
        0x0B05,  # Diagnostics (read-only stats)
        0x1000,  # Touchlink/LightLink (commissioning)
        # Manufacturer-specific that are typically not configurable
        0xFC00,  # Philips (usually commands/config)
        0xFC11,  # Sonoff (settings storage)
    }

    # Clusters configurable ONLY if in INPUT clusters
    CONFIGURABLE_INPUT_ONLY = {
        # Power & Energy
        0x0001: "Power Configuration",        # Battery voltage/percentage
        0x0702: "Metering",                   # Energy consumption
        0x0B04: "Electrical Measurement",     # Voltage/current/power

        # Environmental Sensors
        0x0002: "Device Temperature",         # Internal temp
        0x0400: "Illuminance Measurement",    # Light level
        0x0402: "Temperature Measurement",    # Ambient temp
        0x0403: "Pressure Measurement",       # Barometric pressure
        0x0404: "Flow Measurement",           # Air/water flow
        0x0405: "Relative Humidity",          # Humidity %
        0x0406: "Occupancy Sensing",          # Motion/presence
        0x0407: "Leaf Wetness",               # Agriculture
        0x0408: "Soil Moisture",              # Agriculture
        0x040D: "CO2 Measurement",            # Air quality
        0x042A: "PM25 Measurement",           # Air quality

        # Actuator State (configurable for feedback)
        0x0006: "OnOff",                      # Switch state (bind only usually)
        0x0008: "Level Control",              # Dimmer position
        0x0102: "Window Covering",            # Blind position
        0x0201: "Thermostat",                 # HVAC state
        0x0202: "Fan Control",                # Fan speed/mode
        0x0203: "Dehumidification Control",   # Dehumidifier
        0x0300: "Color Control",              # Light color/temp

        # Security
        0x0500: "IAS Zone",                   # Alarm state

        # Inputs (sensor-like)
        0x000C: "Analog Input",               # Generic analog value
        0x000F: "Binary Input",               # Generic binary state
        0x0012: "Multistate Input",           # Multi-value sensor (buttons)
    }

    # Manufacturer-specific clusters (configurable if in INPUT)
    MANUFACTURER_SPECIFIC_CONFIGURABLE = {
        0xEF00: "Tuya Manufacturer",          # Tuya DP tunneling
        0xFCC0: "Aqara Manufacturer",         # Aqara extensions
    }

    # Clusters that use OUTPUT for binding (not for state reporting)
    BINDING_OUTPUT_CLUSTERS = {
        0x0006,  # OnOff - Buttons/sensors bind to lights
        0x0008,  # Level Control - Dimmers bind to lights
        0x0300,  # Color Control - Color remotes bind to lights
        0x0004,  # Groups - Group commands
        0x0005,  # Scenes - Scene recall
    }

    def __init__(self, zha_device):
        """
        Initialize capabilities for a ZHA device wrapper.
        We access the underlying zigpy device via zha_device.zigpy_dev
        """
        self.device = zha_device
        self.zigpy_dev = zha_device.zigpy_dev
        self._capabilities: Set[str] = set()
        self._cluster_ids: Set[int] = set()
        self._configurable_endpoints: Dict[int, Dict[str, Any]] = {}  # {ep_id: {...}}
        self._detect_capabilities()

    def _detect_capabilities(self):
        """Smart Capability Detection with comprehensive cluster analysis."""
        self._capabilities.clear()
        self._cluster_ids.clear()
        self._configurable_endpoints.clear()

        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        # --- PHASE 1: Comprehensive Endpoint Analysis ---
        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue

            ep_info = {
                'configurable_clusters': set(),
                'input_clusters': set(),
                'output_clusters': set(),
                'role': 'unknown',
            }

            # INPUT clusters
            for cluster in ep.in_clusters.values():
                cid = cluster.cluster_id
                self._cluster_ids.add(cid)
                ep_info['input_clusters'].add(cid)

                if cid not in self.NEVER_CONFIGURABLE:
                    if cid in self.CONFIGURABLE_INPUT_ONLY or cid in self.MANUFACTURER_SPECIFIC_CONFIGURABLE:
                        ep_info['configurable_clusters'].add(cid)

            # OUTPUT clusters
            for cluster in ep.out_clusters.values():
                cid = cluster.cluster_id
                self._cluster_ids.add(cid)
                ep_info['output_clusters'].add(cid)

            # Determine role
            has_actuator_inputs = bool(ep_info['input_clusters'] & {0x0006, 0x0008, 0x0102, 0x0201, 0x0300})
            has_sensor_inputs = bool(ep_info['input_clusters'] & {0x0400, 0x0402, 0x0405, 0x0406, 0x0500})
            has_control_outputs = bool(ep_info['output_clusters'] & self.BINDING_OUTPUT_CLUSTERS)

            if has_actuator_inputs and not has_control_outputs:
                ep_info['role'] = 'actuator'
            elif has_sensor_inputs and not has_actuator_inputs:
                ep_info['role'] = 'sensor'
            elif has_control_outputs and not has_actuator_inputs:
                ep_info['role'] = 'controller'
            elif has_actuator_inputs and has_sensor_inputs:
                ep_info['role'] = 'mixed'
            else:
                ep_info['role'] = 'passive'

            self._configurable_endpoints[ep_id] = ep_info

            LOGGER.debug(
                f"[{self.device.ieee}] EP{ep_id} role={ep_info['role']}, "
                f"configurable={len(ep_info['configurable_clusters'])}"
            )

        # --- PHASE 2: Standard Capability Detection ---

        # Closures
        if self.WINDOW_COVERING in self._cluster_ids:
            self._capabilities.add('window_covering')
            self._capabilities.add('cover')

        # HVAC
        if self.THERMOSTAT in self._cluster_ids:
            self._capabilities.add('thermostat')
            self._capabilities.add('hvac')
        if self.FAN_CONTROL in self._cluster_ids:
            self._capabilities.add('fan_control')
            self._capabilities.add('hvac')

        # Lighting
        if self.COLOR_CONTROL in self._cluster_ids:
            self._capabilities.add('color_control')
            self._capabilities.add('light')
        if self.LEVEL_CONTROL in self._cluster_ids:
            self._capabilities.add('level_control')
            if 'cover' not in self._capabilities:
                self._capabilities.add('light')

        if self.ON_OFF in self._cluster_ids:
            self._capabilities.add('on_off')
            if not ('light' in self._capabilities or 'cover' in self._capabilities):
                self._capabilities.add('switch')

        # Sensors
        if self.OCCUPANCY_SENSING in self._cluster_ids:
            self._capabilities.add('occupancy_sensing')
            self._capabilities.add('motion_sensor')

        if self.IAS_ZONE in self._cluster_ids:
            self._capabilities.add('ias_zone')
            if 'lumi.sensor_magnet' in model:
                self._capabilities.add('contact_sensor')
            else:
                self._capabilities.add('motion_sensor')

        if self.TEMPERATURE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('temperature_sensor')
            self._capabilities.add('environmental_sensor')

        if self.DEVICE_TEMPERATURE in self._cluster_ids:
            self._capabilities.add('temperature_sensor')

        if self.RELATIVE_HUMIDITY in self._cluster_ids:
            self._capabilities.add('humidity_sensor')
            self._capabilities.add('environmental_sensor')

        if self.PRESSURE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('pressure_sensor')
            self._capabilities.add('environmental_sensor')

        if self.ILLUMINANCE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('illuminance_sensor')
            self._capabilities.add('environmental_sensor')

        # Power
        if self.POWER_CONFIGURATION in self._cluster_ids:
            self._capabilities.add('battery')
        if self.METERING in self._cluster_ids or self.ELECTRICAL_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('metering')
            self._capabilities.add('power_monitoring')

        # --- PHASE 3: Context-Aware Quirks ---

        # XIAOMI / LUMI Specific
        if "lumi.sensor_magnet" in model:
            self._capabilities.add('contact_sensor')
            self._capabilities.add('battery')
            self._capabilities.discard('switch')
            self._capabilities.discard('light')
            self._capabilities.discard('motion_sensor')
            self._capabilities.discard('occupancy_sensing')

        # PHILIPS HUE / SIGNIFY
        if "philips" in manufacturer or "signify" in manufacturer:
            if "sml" in model and self.ON_OFF in self._cluster_ids:
                self._capabilities.add('motion_sensor')
                self._capabilities.discard('switch')
                # Apply EP1 controller quirk
                if 1 in self._configurable_endpoints:
                    self._configurable_endpoints[1]['configurable_clusters'].clear()
                    self._configurable_endpoints[1]['role'] = 'controller'
                    LOGGER.info(f"[{self.device.ieee}] Philips SML quirk: EP1=controller (skip config)")

        # TUYA / SMART LIFE
        if self.TUYA_MANUFACTURER in self._cluster_ids:
            self._capabilities.add('tuya')

            is_functional_device = (
                    'window_covering' in self._capabilities or
                    'thermostat' in self._capabilities or
                    'light' in self._capabilities or
                    'switch' in self._capabilities
            )

            if '_tze' in manufacturer or 'ts0601' in model:
                if is_functional_device:
                    LOGGER.debug(f"Tuya device {model} identified as functional device, skipping presence quirk.")
                else:
                    self._capabilities.add('presence_sensor')
                    self._capabilities.add('radar_sensor')
                    self._capabilities.add('occupancy_sensing')

        # --- PHASE 4: Multi-Endpoint Detection ---
        total_endpoints = len([e for e in self.zigpy_dev.endpoints if e > 0])
        if total_endpoints > 1:
            self._capabilities.add('multi_endpoint')

            actuator_endpoints = [
                ep_id for ep_id, info in self._configurable_endpoints.items()
                if info['role'] in ('actuator', 'mixed')
            ]
            if len(actuator_endpoints) > 1:
                self._capabilities.add('multi_switch')
                LOGGER.info(f"[{self.device.ieee}] Multi-switch device detected: EPs {actuator_endpoints}")


    def is_endpoint_configurable(self, endpoint_id: int) -> bool:
        """Check if an endpoint has any configurable clusters."""
        if endpoint_id not in self._configurable_endpoints:
            return False
        return bool(self._configurable_endpoints[endpoint_id]['configurable_clusters'])

    def is_cluster_configurable(self, cluster_id: int, endpoint_id: int) -> bool:
        """Check if a specific cluster on an endpoint is configurable."""
        if endpoint_id not in self._configurable_endpoints:
            return False
        return cluster_id in self._configurable_endpoints[endpoint_id]['configurable_clusters']

    def get_endpoint_role(self, endpoint_id: int) -> str:
        """Get the role of an endpoint."""
        return self._configurable_endpoints.get(endpoint_id, {}).get('role', 'unknown')

    def get_configurable_clusters(self, endpoint_id: Optional[int] = None) -> Set[int]:
        """Get configurable cluster IDs for an endpoint or all endpoints."""
        if endpoint_id is not None:
            return self._configurable_endpoints.get(endpoint_id, {}).get('configurable_clusters', set())

        # All configurable clusters across all endpoints
        all_clusters = set()
        for ep_info in self._configurable_endpoints.values():
            all_clusters.update(ep_info['configurable_clusters'])
        return all_clusters

    def get_configuration_info(self) -> Dict[str, Any]:
        """Get detailed configuration capability info for API/debugging."""
        return {
            "endpoints": {
                ep_id: {
                    "role": info['role'],
                    "configurable": [f"0x{c:04x}" for c in info['configurable_clusters']],
                    "input_count": len(info['input_clusters']),
                    "output_count": len(info['output_clusters']),
                }
                for ep_id, info in self._configurable_endpoints.items()
            },
            "total_configurable_clusters": len(self.get_configurable_clusters()),
            "is_multi_endpoint": self.has_capability('multi_endpoint'),
        }

    def has_capability(self, capability: str) -> bool:
        return capability in self._capabilities

    def get_capabilities(self) -> Set[str]:
        return self._capabilities

    def get_info(self) -> Dict[str, Any]:
        """
        Get capabilities info for API/debugging.

        Returns:
            Dictionary with capabilities and cluster information
        """
        return {
            "capabilities": sorted(list(self._capabilities)),
            "clusters": [f"0x{cid:04X}" for cid in sorted(self._cluster_ids)]
        }

    # =========================================================================
    # COMPATIBILITY PROPERTIES (Prevents AttributeErrors)
    # =========================================================================
    @property
    def is_light(self):
        return self.has_capability('light')

    @property
    def is_switch(self):
        return self.has_capability('switch')

    @property
    def supports_brightness(self):
        # Level control (0x0008) is the standard for brightness
        return self.has_capability('level_control')

    @property
    def supports_color_temp(self):
        return self.has_capability('color_control')

    @property
    def supports_color_xy(self):
        return self.has_capability('color_control')

    @property
    def is_contact_sensor(self):
        return self.has_capability('contact_sensor') or self.has_capability('ias_zone')

    @property
    def is_motion_sensor(self):
        return self.has_capability('motion_sensor') or self.has_capability('occupancy_sensing')

    @property
    def is_temperature_sensor(self):
        return self.has_capability('temperature_sensor')

    @property
    def is_humidity_sensor(self):
        return self.has_capability('humidity_sensor')

    @property
    def is_illuminance_sensor(self):
        return self.has_capability('illuminance_sensor')

    @property
    def supports_power_monitoring(self):
        return self.has_capability('power_monitoring') or self.has_capability('metering')

    @property
    def is_cover(self):
        return self.has_capability('cover') or self.has_capability('window_covering')

    # =========================================================================

    def allows_field(self, field_name: str) -> bool:
        """
        Check if a state field is allowed for this device based on detected capabilities.
        """
        # 1. Universal Allow List
        if field_name in self.UNIVERSAL_FIELDS: return True
        if field_name.endswith('_raw'): return True
        if field_name.startswith('dp_'): return True
        if field_name.startswith('startup_behavior'): return True

        # 2. Endpoint specific handling
        if '_' in field_name:
            parts = field_name.rsplit('_', 1)
            if len(parts) == 2 and parts[1].isdigit():
                return self.allows_field(parts[0])

        # 3. Capability Checks
        if field_name in self.MOTION_FIELDS:
            return (self.has_capability('motion_sensor') or
                    self.has_capability('occupancy_sensing') or
                    self.has_capability('presence_sensor') or
                    self.has_capability('radar_sensor'))

        if field_name in self.CONTACT_FIELDS:
            return (self.has_capability('contact_sensor') or
                    self.has_capability('ias_zone') or
                    (field_name in {'is_open', 'is_closed'} and self.has_capability('cover')))

        if field_name in self.IAS_ZONE_FIELDS:
            return self.has_capability('ias_zone')

        if field_name in self.LIGHTING_FIELDS:
            return (self.has_capability('on_off') or
                    self.has_capability('level_control') or
                    self.has_capability('color_control') or
                    self.has_capability('light') or
                    self.has_capability('switch'))

        if field_name in self.HVAC_FIELDS:
            return (self.has_capability('thermostat') or
                    self.has_capability('hvac') or
                    self.has_capability('fan_control'))

        if field_name in self.POWER_FIELDS:
            return (self.has_capability('power_monitoring') or
                    self.has_capability('electrical_measurement') or
                    self.has_capability('metering'))

        if field_name in self.ENVIRONMENTAL_FIELDS:
            return (self.has_capability('environmental_sensor') or
                    self.has_capability('temperature_sensor') or
                    self.has_capability('humidity_sensor') or
                    self.has_capability('pressure_sensor') or
                    self.has_capability('illuminance_sensor'))

        if field_name in self.BATTERY_FIELDS:
            return self.has_capability('battery')

        if field_name in self.COVER_FIELDS:
            return (self.has_capability('cover') or
                    self.has_capability('window_covering'))

        # Fix for misidentified Tuya Radar fields
        if field_name in self.TUYA_RADAR_FIELDS:
            # Only allow these fields if it is explicitly a radar/presence sensor
            # This prevents blinds (which are "tuya" but not "radar") from showing them
            return (self.has_capability('radar_sensor') or
                    self.has_capability('presence_sensor') or
                    self.has_capability('occupancy_sensing'))

        return True

    def filter_state_update(self, state_dict: Dict[str, Any]) -> Dict[str, Any]:
        filtered = {}
        for key, value in state_dict.items():
            if self.allows_field(key):
                filtered[key] = value
        return filtered