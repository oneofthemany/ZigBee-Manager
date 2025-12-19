"""
HVAC cluster handlers for Zigbee devices.
Handles: Thermostats, TRVs (Thermostatic Radiator Valves), HVAC systems
Compatible with: Hive Smart Heating, generic TRVs, AC units
"""
import logging
from typing import Any, Dict, Optional, List
from enum import IntEnum
import asyncio

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.hvac")

class ThermostatRunningState(IntEnum):
    """Thermostat running state bits."""
    HEAT = 0x0001
    COOL = 0x0002
    FAN = 0x0004
    HEAT_STAGE_2 = 0x0008
    COOL_STAGE_2 = 0x0010
    FAN_STAGE_2 = 0x0020
    FAN_STAGE_3 = 0x0040

class ThermostatSystemMode(IntEnum):
    """Thermostat system modes."""
    OFF = 0x00
    AUTO = 0x01
    COOL = 0x03
    HEAT = 0x04
    EMERGENCY_HEAT = 0x05
    PRECOOLING = 0x06
    FAN_ONLY = 0x07
    DRY = 0x08
    SLEEP = 0x09

# ============================================================
# THERMOSTAT CLUSTER (0x0201)
# ============================================================
@register_handler(0x0201)
class ThermostatHandler(ClusterHandler):
    """
    Handles Thermostat cluster (0x0201).
    Adopted from ZHA implementation for robust Hive support.
    """
    CLUSTER_ID = 0x0201
    
    # ZHA-aligned reporting configuration
    REPORT_CONFIG = [
        # Local Temperature: Min 30s, Max 300s, Change 0.25°C (25)
        ("local_temperature", 30, 300, 25),
        
        # Setpoints: Min 10s, Max 300s, Change 0.5°C (50)
        ("occupied_heating_setpoint", 10, 300, 50),
        ("occupied_cooling_setpoint", 10, 300, 50),
        ("unoccupied_heating_setpoint", 10, 300, 50),
        
        # PI Demand: Min 30s, Max 300s, Change 5% (5)
        ("pi_heating_demand", 30, 300, 5),
        ("pi_cooling_demand", 30, 300, 5),
        
        # States: Min 10s, Max 300s, Change 1 (Discrete)
        ("system_mode", 10, 300, 1),
        ("running_state", 10, 300, 1),
        ("running_mode", 10, 300, 1),
        ("occupancy", 10, 300, 1),
    ]

    # Attribute IDs
    ATTR_LOCAL_TEMP = 0x0000
    ATTR_OUTDOOR_TEMP = 0x0001
    ATTR_OCCUPANCY = 0x0002
    ATTR_ABS_MIN_HEAT_SETPOINT_LIMIT = 0x0003
    ATTR_ABS_MAX_HEAT_SETPOINT_LIMIT = 0x0004
    ATTR_ABS_MIN_COOL_SETPOINT_LIMIT = 0x0005
    ATTR_ABS_MAX_COOL_SETPOINT_LIMIT = 0x0006
    ATTR_PI_COOLING_DEMAND = 0x0007
    ATTR_PI_HEATING_DEMAND = 0x0008
    ATTR_LOCAL_TEMP_CALIBRATION = 0x0010
    ATTR_OCCUPIED_COOLING_SETPOINT = 0x0011
    ATTR_OCCUPIED_HEATING_SETPOINT = 0x0012
    ATTR_UNOCCUPIED_COOLING_SETPOINT = 0x0013
    ATTR_UNOCCUPIED_HEATING_SETPOINT = 0x0014
    ATTR_MIN_HEAT_SETPOINT_LIMIT = 0x0015
    ATTR_MAX_HEAT_SETPOINT_LIMIT = 0x0016
    ATTR_MIN_COOL_SETPOINT_LIMIT = 0x0017
    ATTR_MAX_COOL_SETPOINT_LIMIT = 0x0018
    ATTR_MIN_SETPOINT_DEAD_BAND = 0x0019
    ATTR_REMOTE_SENSING = 0x001A
    ATTR_CTRL_SEQUENCE_OF_OPER = 0x001B
    ATTR_SYSTEM_MODE = 0x001C
    ATTR_ALARM_MASK = 0x001D
    ATTR_RUNNING_MODE = 0x001E
    ATTR_RUNNING_STATE = 0x0029
    ATTR_SETPOINT_CHANGE_SOURCE = 0x0030

    SYSTEM_MODES = {
        0x00: "off", 0x01: "auto", 0x03: "cool", 0x04: "heat",
        0x05: "emergency_heat", 0x07: "fan_only", 0x08: "dry", 0x09: "sleep",
    }

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self.is_receiver = False
        # Default limits (safe defaults)
        self._min_heat = 5.0
        self._max_heat = 32.0
        
        # detect if this is a Hive Receiver (SLR1c, SLR1b, etc.)
        model = str(device.zigpy_dev.model or "").upper()
        if "SLR" in model or "RECEIVER" in model:
            self.is_receiver = True
            logger.info(f"[{self.device.ieee}] Detected as Receiver (SLR). Special handling enabled.")

    async def configure(self):
        """
        Configure cluster, matching ZHA's initialization logic.
        Reads limits and capabilities on startup and stores them.
        """
        # 1. Standard Binding & Reporting
        await super().configure()

        # 2. ZHA Initialization Attributes
        init_attrs = [
            self.ATTR_ABS_MIN_HEAT_SETPOINT_LIMIT,
            self.ATTR_ABS_MAX_HEAT_SETPOINT_LIMIT,
            self.ATTR_MAX_HEAT_SETPOINT_LIMIT,
            self.ATTR_MIN_HEAT_SETPOINT_LIMIT,
            self.ATTR_LOCAL_TEMP_CALIBRATION,
            self.ATTR_CTRL_SEQUENCE_OF_OPER,
            self.ATTR_SYSTEM_MODE,
            self.ATTR_OCCUPIED_HEATING_SETPOINT
        ]
        
        logger.info(f"[{self.device.ieee}] Reading ZHA initialization attributes...")
        try:
            # Read attributes
            async with asyncio.timeout(10.0):
                success, failure = await self.cluster.read_attributes(init_attrs)
            
            if success:
                logger.info(f"[{self.device.ieee}] Initialization attributes read successfully")
                
                # Update limits if present
                if self.ATTR_MIN_HEAT_SETPOINT_LIMIT in success:
                    val = success[self.ATTR_MIN_HEAT_SETPOINT_LIMIT]
                    if isinstance(val, (int, float)):
                        self._min_heat = round(float(val) / 100, 1)
                        logger.info(f"[{self.device.ieee}] Min Heat Limit: {self._min_heat}°C")
                        self.device.update_state({"min_temp": self._min_heat})

                if self.ATTR_MAX_HEAT_SETPOINT_LIMIT in success:
                    val = success[self.ATTR_MAX_HEAT_SETPOINT_LIMIT]
                    if isinstance(val, (int, float)):
                        self._max_heat = round(float(val) / 100, 1)
                        logger.info(f"[{self.device.ieee}] Max Heat Limit: {self._max_heat}°C")
                        self.device.update_state({"max_temp": self._max_heat})

                # Process other attributes immediately
                for attr_id, value in success.items():
                    self.attribute_updated(attr_id, value)
                    
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to read init attributes: {e}")

        return True

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None: return

        if hasattr(value, 'value'): value = value.value

        # Always parse the value first using the centralized logic
        parsed_value = self.parse_value(attrid, value)

        updates = {}

        if attrid == self.ATTR_LOCAL_TEMP:
            # If it's a receiver, this might be internal temp, not room temp.
            if self.is_receiver:
                updates["internal_temperature"] = parsed_value
            else:
                updates["local_temperature"] = parsed_value
                updates["temperature"] = parsed_value

        elif attrid == self.ATTR_OCCUPIED_HEATING_SETPOINT:
            updates["occupied_heating_setpoint"] = parsed_value
            updates["heating_setpoint"] = parsed_value

        elif attrid == self.ATTR_SYSTEM_MODE:
            updates["system_mode"] = parsed_value

        elif attrid == self.ATTR_RUNNING_STATE:
            # parsed_value is the raw bitmap
            is_heating = bool(value & ThermostatRunningState.HEAT)
            action = "heating" if is_heating else "idle"
            updates["running_state"] = value
            updates["hvac_action"] = action

        elif attrid == self.ATTR_PI_HEATING_DEMAND:
            updates["heating_demand"] = value
            
        elif attrid == self.ATTR_OCCUPANCY:
            updates["occupancy"] = bool(value)
            
        elif attrid == self.ATTR_MIN_HEAT_SETPOINT_LIMIT:
             self._min_heat = parsed_value
             updates["min_temp"] = parsed_value
             
        elif attrid == self.ATTR_MAX_HEAT_SETPOINT_LIMIT:
             self._max_heat = parsed_value
             updates["max_temp"] = parsed_value

        if updates:
            self.device.update_state(updates)

    def parse_value(self, attrid: int, value: Any) -> Any:
        """
        Centralized parsing logic for BOTH polling and attribute reports.
        """
        if value is None: return None
        if hasattr(value, 'value'): value = value.value

        # 1. Temperature Parsing (Centidegrees -> Degrees)
        if attrid in [self.ATTR_LOCAL_TEMP, self.ATTR_OCCUPIED_HEATING_SETPOINT, 
                     self.ATTR_OCCUPIED_COOLING_SETPOINT, self.ATTR_MIN_HEAT_SETPOINT_LIMIT,
                     self.ATTR_MAX_HEAT_SETPOINT_LIMIT]:
            if isinstance(value, (int, float)) and value != 0x8000:
                # Zigbee standard is ALWAYS centidegrees (0.01 C)
                # We simply divide by 100.
                return round(float(value) / 100, 1)
        
        # 2. System Mode Parsing (Enum -> String)
        if attrid == self.ATTR_SYSTEM_MODE:
             # If it's already a string, return it
             if isinstance(value, str): return value
             # Otherwise map int to string
             return self.SYSTEM_MODES.get(value, value)

        return value

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_LOCAL_TEMP: 
            return "internal_temperature" if self.is_receiver else "local_temperature"
        if attrid == self.ATTR_OCCUPIED_HEATING_SETPOINT: return "occupied_heating_setpoint"
        if attrid == self.ATTR_SYSTEM_MODE: return "system_mode"
        if attrid == self.ATTR_PI_HEATING_DEMAND: return "heating_demand"
        if attrid == self.ATTR_RUNNING_STATE: return "running_state"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        attrs = {
            self.ATTR_OCCUPIED_HEATING_SETPOINT: "heating_setpoint",
            self.ATTR_SYSTEM_MODE: "system_mode",
            self.ATTR_PI_HEATING_DEMAND: "heating_demand",
            self.ATTR_RUNNING_STATE: "running_state",
        }
        # Only poll local temp if it's NOT a receiver (or poll as internal)
        if self.is_receiver:
             attrs[self.ATTR_LOCAL_TEMP] = "internal_temperature"
        else:
             attrs[self.ATTR_LOCAL_TEMP] = "local_temperature"
        return attrs

    # --- COMMANDS ---
    async def set_heating_setpoint(self, temperature: float):
        """Set heating setpoint in degrees Celsius."""
        # 1. Clamp to System Capabilities
        # Only enforce min if we aren't setting to "Frost Protect" levels (some use low values like 1.0)
        # But generally, respect the capabilities read from the device.
        temperature = max(self._min_heat, min(self._max_heat, float(temperature)))
        
        logger.info(f"[{self.device.ieee}] Setting setpoint to {temperature}°C (Limits: {self._min_heat}-{self._max_heat})")

        # 2. Convert to Zigbee Centidegrees (REQUIRED)
        value = int(temperature * 100)
        
        await self.cluster.write_attributes({"occupied_heating_setpoint": value})
        
        # Optimistic update
        self.device.update_state({"heating_setpoint": temperature, "occupied_heating_setpoint": temperature})

    async def set_system_mode(self, mode: str):
        """Set system mode (off, auto, heat)."""
        # Inverse mapping: string -> int
        mode_map = {v: k for k, v in self.SYSTEM_MODES.items()}
        
        # Handle string input (from UI usually)
        if isinstance(mode, str):
            mode_key = mode.lower()
            if mode_key in mode_map:
                mode_val = mode_map[mode_key]
                await self.cluster.write_attributes({"system_mode": mode_val})
                self.device.update_state({"system_mode": mode_key})
                logger.info(f"[{self.device.ieee}] Set system mode to {mode_key} ({mode_val})")
            else:
                logger.warning(f"[{self.device.ieee}] Invalid mode string: {mode}")
        
        # Handle integer input (sometimes passed directly)
        elif isinstance(mode, int):
            if mode in self.SYSTEM_MODES:
                await self.cluster.write_attributes({"system_mode": mode})
                mode_str = self.SYSTEM_MODES[mode]
                self.device.update_state({"system_mode": mode_str})
                logger.info(f"[{self.device.ieee}] Set system mode to {mode_str} ({mode})")
            else:
                logger.warning(f"[{self.device.ieee}] Invalid mode int: {mode}")

    # Convenience method to toggle heat/off
    async def turn_on(self):
        """Turn heating on (set to Heat mode)."""
        await self.set_system_mode("heat")

    async def turn_off(self):
        """Turn heating off (set to Off mode)."""
        await self.set_system_mode("off")

    # --- HA DISCOVERY ---
    def get_discovery_configs(self) -> List[Dict]:
        """Generate Home Assistant discovery configs."""
        
        name_suffix = " Receiver" if self.is_receiver else ""
        base_topic = "zigbee_ha"
        return [
            {
                "component": "climate",
                "object_id": "thermostat",
                "config": {
                    "name": f"Thermostat{name_suffix}",
                    "modes": ["off", "heat", "auto"],
                    "temperature_unit": "C",
                    "min_temp": self._min_heat,
                    "max_temp": self._max_heat,
                    "temp_step": 0.5,
                    "current_temperature_topic": f"{base_topic}/{self.device.service.get_safe_name(self.device.ieee)}",
                    "current_temperature_template": "{{ value_json.local_temperature }}",
                    "temperature_state_topic": f"{base_topic}/{self.device.service.get_safe_name(self.device.ieee)}",
                    "temperature_state_template": "{{ value_json.occupied_heating_setpoint }}",
                    "mode_state_topic": f"{base_topic}/{self.device.service.get_safe_name(self.device.ieee)}",
                    "mode_state_template": "{{ value_json.system_mode }}",
                    "action_topic": f"{base_topic}/{self.device.service.get_safe_name(self.device.ieee)}",
                    "action_template": "{{ value_json.hvac_action }}",
                    "temperature_command_topic": "CMD_TOPIC_PLACEHOLDER",
                    "mode_command_topic": "CMD_TOPIC_PLACEHOLDER",
                    "command_template_temp": '{"command": "temperature", "value": "{{ value }}"}',
                    "command_template_mode": '{"command": "system_mode", "value": "{{ value }}"}'
                }
            },
            {
                "component": "sensor",
                "object_id": "heating_demand",
                "config": {
                    "name": "Heating Demand",
                    "device_class": "power_factor",
                    "unit_of_measurement": "%",
                    "value_template": "{{ value_json.heating_demand }}"
                }
            }
        ]

# ============================================================
# USER INTERFACE CLUSTER (0x0204)
# ============================================================
@register_handler(0x0204)
class UserInterfaceHandler(ClusterHandler):
    CLUSTER_ID = 0x0204
    ATTR_KEYPAD_LOCKOUT = 0x0001
    LOCKOUT_MODES = {0x00: "No Lockout", 0x01: "Level 1", 0x02: "Level 2", 0x03: "Level 3", 0x04: "Level 4", 0x05: "Level 5"}

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_KEYPAD_LOCKOUT:
            if hasattr(value, 'value'): value = value.value
            mode = self.LOCKOUT_MODES.get(value, f"Unknown ({value})")
            self.device.update_state({"keypad_lockout": mode})

# ============================================================
# FAN CONTROL CLUSTER (0x0202)
# ============================================================
@register_handler(0x0202)
class FanControlHandler(ClusterHandler):
    CLUSTER_ID = 0x0202
    REPORT_CONFIG = [("fan_mode", 0, 300, 1)]
    ATTR_FAN_MODE = 0x0000
    FAN_MODES = {0x00: "off", 0x01: "low", 0x02: "medium", 0x03: "high", 0x04: "on", 0x05: "auto", 0x06: "smart"}

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_FAN_MODE:
            mode = self.FAN_MODES.get(value, f"unknown_{value}")
            self.device.update_state({"fan_mode": mode})

    async def set_fan_mode(self, mode: str):
        mode_map = {v: k for k, v in self.FAN_MODES.items()}
        if mode.lower() in mode_map:
            await self.cluster.write_attributes({"fan_mode": mode_map[mode.lower()]})
