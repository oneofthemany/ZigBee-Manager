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
    Analyzes Zigbee clusters and applies manufacturer quirks contextually to determine
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
        'fading_time', 'self_test', 'target_distance', 'illuminance_threshold'
    }

    # Fields that are ALWAYS allowed regardless of capability
    UNIVERSAL_FIELDS = {
        'last_seen', 'power_source', 'manufacturer', 'model',
        'available', 'lqi', 'rssi', 'sw_version', 'date_code',
        'application_version', 'stack_version', 'hw_version',
        'manufacturer_id', 'power_source_raw', 'device_type',
        'linkquality', 'update_available', 'update_state', 'action',
        'ieee', 'nwk', 'friendly_name', 'device_type',
        'multistate_value', 'on_with_timed_off'
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

    def __init__(self, zha_device):
        """
        Initialize capabilities for a ZHA device wrapper.
        We access the underlying zigpy device via zha_device.zigpy_dev
        """
        self.device = zha_device
        self.zigpy_dev = zha_device.zigpy_dev
        self._capabilities: Set[str] = set()
        self._cluster_ids: Set[int] = set()
        self._detect_capabilities()

    def _detect_capabilities(self):
        """
        Smart Capability Detection.
        Phase 1: Fact Gathering (Endpoints & Clusters)
        Phase 2: Standard Capability mapping
        Phase 3: Context-Aware Quirk Application
        """
        self._capabilities.clear()
        self._cluster_ids.clear()

        # --- PHASE 1: Data Gathering ---
        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        # Flatten all clusters from all endpoints for "Big Picture" analysis
        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0: continue # Skip ZDO

            # Collect both In and Out clusters
            for c in ep.in_clusters.values(): self._cluster_ids.add(c.cluster_id)
            for c in ep.out_clusters.values(): self._cluster_ids.add(c.cluster_id)


        # --- PHASE 2: Standard Capability Detection ---

        # 1. Closures (Blinds/Covers) - Strong Signal
        if self.WINDOW_COVERING in self._cluster_ids:
            self._capabilities.add('window_covering')
            self._capabilities.add('cover')

        # 2. HVAC - Strong Signal
        if self.THERMOSTAT in self._cluster_ids:
            self._capabilities.add('thermostat')
            self._capabilities.add('hvac')
        if self.FAN_CONTROL in self._cluster_ids:
            self._capabilities.add('fan_control')
            self._capabilities.add('hvac')

        # 3. Lighting (Standard)
        if self.COLOR_CONTROL in self._cluster_ids:
            self._capabilities.add('color_control')
            self._capabilities.add('light')
        if self.LEVEL_CONTROL in self._cluster_ids:
            self._capabilities.add('level_control')
            # Level control usually implies light unless it's a cover (handled above)
            if 'cover' not in self._capabilities:
                self._capabilities.add('light')

        if self.ON_OFF in self._cluster_ids:
            self._capabilities.add('on_off')
            # Determine if it's a switch or light if not explicit
            # If we already have light (from color/level), we are good.
            # If not, and it's not a cover, it's likely a switch or basic light.
            if not ('light' in self._capabilities or 'cover' in self._capabilities):
                self._capabilities.add('switch')

        # 4. Standard Sensors
        if self.OCCUPANCY_SENSING in self._cluster_ids:
            self._capabilities.add('occupancy_sensing')
            self._capabilities.add('motion_sensor')

        if self.IAS_ZONE in self._cluster_ids:
            self._capabilities.add('ias_zone')
            self._capabilities.add('motion_sensor') # Default assumption
            self._capabilities.add('contact_sensor') # Default assumption

        if self.TEMPERATURE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('temperature_sensor')
            self._capabilities.add('environmental_sensor')

        if self.RELATIVE_HUMIDITY in self._cluster_ids:
            self._capabilities.add('humidity_sensor')
            self._capabilities.add('environmental_sensor')

        if self.PRESSURE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('pressure_sensor')
            self._capabilities.add('environmental_sensor')

        if self.ILLUMINANCE_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('illuminance_sensor')
            self._capabilities.add('environmental_sensor')

        # 5. Power
        if self.POWER_CONFIGURATION in self._cluster_ids:
            self._capabilities.add('battery')
        if self.METERING in self._cluster_ids or self.ELECTRICAL_MEASUREMENT in self._cluster_ids:
            self._capabilities.add('metering')
            self._capabilities.add('power_monitoring')


        # --- PHASE 3: Context-Aware Quirks ---

        # PHILIPS HUE / SIGNIFY
        # Quirk: SML models use OnOff (0x0006) for Motion, but they are sensors, not switches.
        if "philips" in manufacturer or "signify" in manufacturer:
            if "sml" in model and self.ON_OFF in self._cluster_ids:
                self._capabilities.add('motion_sensor')
                # Remove switch capability if it was added in Phase 2
                self._capabilities.discard('switch')

        # TUYA / SMART LIFE
        if self.TUYA_MANUFACTURER in self._cluster_ids:
            self._capabilities.add('tuya')

            # Quirk: Tuya devices use TS0601 / _TZE... for EVERYTHING.
            # We must NOT blindly assume TS0601 is a Radar/Presence sensor.
            # We only assume it is a wrapper sensor if it lacks standard functional clusters.

            # Check what we found in Phase 2
            is_functional_device = (
                    'window_covering' in self._capabilities or
                    'thermostat' in self._capabilities or
                    'light' in self._capabilities or
                    'switch' in self._capabilities
            )

            if '_tze' in manufacturer or 'ts0601' in model:
                if is_functional_device:
                    # Case: _TZE200... with WindowCovering (0x0102).
                    # Logic: It IS a blind. It is NOT a presence sensor.
                    LOGGER.debug(f"Tuya device {model} identified as functional device, skipping presence quirk.")
                else:
                    # Case: _TZE200... with ONLY Basic + Tuya(0xEF00).
                    # Logic: It is likely a complex sensor (mmWave, Presence) that uses 0xEF00 tunneling.
                    self._capabilities.add('presence_sensor')
                    self._capabilities.add('radar_sensor')
                    self._capabilities.add('occupancy_sensing')

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