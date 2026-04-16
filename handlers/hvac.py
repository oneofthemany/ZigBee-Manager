"""
HVAC cluster handlers for Zigbee devices.
Handles: Thermostats, TRVs (Thermostatic Radiator Valves), HVAC systems
Compatible with: Hive Smart Heating (SLR1c, SLR1b), Aqara TRVs, generic thermostats
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

    MQTT State Keys (Home Assistant compatible):
    - current_temperature: Current room temperature (for HA climate)
    - temperature: Alias for current_temperature
    - local_temperature: Raw local temp from device
    - occupied_heating_setpoint: Target setpoint in °C
    - target_temp: Alias for occupied_heating_setpoint (for scheduler)
    - system_mode: "off", "heat", "auto", etc.
    - hvac_action: "heating", "idle", "off"
    - heating_demand: PI demand percentage (0-100)
    """
    CLUSTER_ID = 0x0201

    # Reporting configuration
    REPORT_CONFIG = [
        # Local Temperature: Min 30s, Max 300s, Change 0.25°C (25)
        ("local_temperature", 60, 300, 25),

        # Setpoints: Min 10s, Max 300s, Change 0.1°C (10)
        ("occupied_heating_setpoint", 0, 300, 10),   # min=0 for instant updates
        ("occupied_cooling_setpoint", 0, 300, 10),
        ("unoccupied_heating_setpoint", 0, 300, 10),

        # PI Demand: Min 60s, Max 300s, Change 10% (10)
        ("pi_heating_demand", 60, 300, 10),
        ("pi_cooling_demand", 60, 300, 10),

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
    ATTR_HVAC_SYSTEM_TYPE = 0x0009
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
    ATTR_START_OF_WEEK = 0x0020
    ATTR_NUMBER_OF_WEEKLY_TRANSITIONS = 0x0021
    ATTR_NUMBER_OF_DAILY_TRANSITIONS = 0x0022
    ATTR_TEMP_SETPOINT_HOLD = 0x0023
    ATTR_TEMP_SETPOINT_HOLD_DURATION = 0x0024
    ATTR_PROG_OPERATION_MODE = 0x0025
    ATTR_RUNNING_STATE = 0x0029
    ATTR_SETPOINT_CHANGE_SOURCE = 0x0030
    ATTR_INTERNAL_TEMP = 0x4000  # Often used by SLR1/SLR1c instead of 0x0000


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

        if self.is_receiver:
            init_attrs.append(self.ATTR_INTERNAL_TEMP)
        else:
            init_attrs.append(self.ATTR_LOCAL_TEMP)

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

        # Ignore 0 degrees as it's likely invalid for a thermostat/receiver
        if attrid in [self.ATTR_LOCAL_TEMP, self.ATTR_INTERNAL_TEMP] and parsed_value == 0:
            logger.debug(f"[{self.device.ieee}] Ignoring invalid 0 temperature reading.")
            return

        updates = {}

        if attrid == self.ATTR_LOCAL_TEMP:
            if self.is_receiver:
                # Receiver gets temperature via binding from thermostat
                updates["internal_temperature"] = parsed_value
                updates["current_temperature"] = parsed_value  # HA climate needs this
                updates["temperature"] = parsed_value
            else:
                updates["local_temperature"] = parsed_value
                updates["current_temperature"] = parsed_value
                updates["temperature"] = parsed_value

        elif attrid == self.ATTR_OCCUPIED_HEATING_SETPOINT:
            updates["occupied_heating_setpoint"] = parsed_value
            updates["heating_setpoint"] = parsed_value
            updates["target_temp"] = parsed_value

        elif attrid == self.ATTR_SYSTEM_MODE:
            updates["system_mode"] = parsed_value

        elif attrid == self.ATTR_RUNNING_STATE:
            # parsed_value is the raw bitmap
            is_heating = bool(value & ThermostatRunningState.HEAT)
            action = "heating" if is_heating else "idle"
            updates["running_state"] = value
            updates["hvac_action"] = action

        elif attrid == self.ATTR_INTERNAL_TEMP:
            updates["internal_temperature"] = parsed_value
            updates["local_temperature"] = parsed_value
            updates["current_temperature"] = parsed_value  # HA climate key
            updates["temperature"] = parsed_value

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
            # Update derived HVAC action (if not already handled in running_state)
            if "hvac_action" not in updates:
                self._update_hvac_action()


    async def set_target_temperature(self, temperature: float):
        """Set TRV target temperature (°C). Zigbee requires centidegrees."""
        temperature = max(self._min_heat, min(self._max_heat, float(temperature)))
        value = int(temperature * 100)

        logger.info(f"[{self.device.ieee}] Writing occupied_heating_setpoint: {temperature}°C ({value} centidegrees)")

        await self.cluster.write_attributes({
            "occupied_heating_setpoint": value  # Use string name, not ID
        })

        self.device.update_state({
            "heating_setpoint": temperature,
            "occupied_heating_setpoint": temperature,
            "target_temp": temperature,  # Scheduler compatibility
        })


    async def set_hvac_mode(self, mode: str):
        """Set HVAC system mode."""
        mode_map = {"off": 0x00, "auto": 0x01, "heat": 0x04}

        if mode not in mode_map:
            logger.warning(f"[{self.device.ieee}] Unsupported HVAC mode: {mode}")
            return

        logger.info(f"[{self.device.ieee}] Writing system_mode: {mode} ({mode_map[mode]})")

        await self.cluster.write_attributes({
            "system_mode": mode_map[mode]  # Use string name, not ID
        })

        self.device.update_state({"system_mode": mode})


    def _update_hvac_action(self):
        """Derive hvac_action (heating, idle, off) from system_mode and running_state."""
        state = self.device.state
        mode = state.get("system_mode", "off")
        run_state = state.get("running_state", 0)

        action = "idle"

        if mode == "off":
            action = "off"
        elif mode == "heat":
            # Running state bit 0 usually means Heat State On
            # Check if running_state is non-zero or specific bit is set
            is_heating = False
            if isinstance(run_state, int):
                if run_state & 0x01: is_heating = True

            if is_heating:
                action = "heating"
            else:
                action = "idle"

        self.device.update_state({"hvac_action": action})


    def process_command(self, command: str, value: Any):
        import asyncio
        if command in ("temperature", "set_temperature"):
            asyncio.create_task(
                self.set_target_temperature(float(value))
            )

        elif command in ("system_mode", "set_mode"):
            asyncio.create_task(
                self.set_hvac_mode(str(value).lower())
            )


    async def handle_command(self, command: str, data: Any):
        """Handle MQTT commands to set state."""
        if command == "system_mode":
            await self.set_system_mode(data)

        elif command in ["temperature", "temperature_setpoint"]:
            try:
                temp = float(data)
                await self.set_heating_setpoint(temp)
            except ValueError:
                logger.error(f"[{self.device.ieee}] Invalid temperature value: {data}")

        elif command == "set_schedule":
            # data should be a dict containing the schedule details
            await self.set_weekly_schedule(data)


    async def set_weekly_schedule(self, schedule_data: Dict):
        """
        Send SetWeeklySchedule command to device.
        Expected data format:
        {
            "day_of_week": 1, # Bitmask: 1=Sun, 2=Mon, 4=Tue... 127=All
            "transitions": [
                {"time": 360, "heat": 20.0}, # 06:00, 20°C
                {"time": 540, "heat": 22.0}, # 09:00, 22°C
                ...
            ]
        }
        """
        # 1. Parse Day of Week (Bitmask)
        # Mon=1, Tue=2, ... Sun=64, All=127 (Standard ZCL usually)
        # Note: Some devices use different bitmasks, check spec. Standard is:
        # 0x01=Sun, 0x02=Mon, etc.
        day_bitmap = schedule_data.get("day_of_week", 0xFF)

        # 2. Parse Transitions
        raw_transitions = schedule_data.get("transitions", [])
        payload = []

        for t in raw_transitions:
            # Time is minutes since midnight (e.g., 6:00 AM = 360)
            transition_time = int(t["time"])
            # Heat Setpoint in Centidegrees (20.0 -> 2000)
            heat_setpoint = int(float(t["heat"]) * 100)

            # Using zigpy's transition struct helper if available, or raw values
            # Structure: TransitionTime (16bit), HeatSetpoint (16bit)
            # Some devices also expect CoolSetpoint if in Auto mode
            payload.append(transition_time)
            payload.append(heat_setpoint)

            # 3. Send Command (Command ID 0x01 = SetWeeklySchedule)
        # Arguments:
        # - Number of Transitions for Sequence
        # - Day of Week for Sequence
        # - Mode for Sequence (1=Heat, 2=Cool, 3=Both)
        # - Payload (The transitions)
        try:
            logger.info(f"[{self.device.ieee}] Sending Schedule: {day_bitmap} -> {len(raw_transitions)} transitions")

            # Note: You might need to adjust 'mode_for_sequence' (1 for Heat)
            await self.cluster.set_weekly_schedule(
                len(raw_transitions),
                day_bitmap,
                1, # 1 = Heat Mode Schedule
                payload
            )
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Failed to set schedule: {e}")

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
            self.ATTR_OCCUPIED_HEATING_SETPOINT: "occupied_heating_setpoint",
            self.ATTR_SYSTEM_MODE: "system_mode",
            self.ATTR_PI_HEATING_DEMAND: "heating_demand",
            self.ATTR_RUNNING_STATE: "running_state",
        }
        # Only poll local temp if it's a receiver
        if self.is_receiver:
            attrs[self.ATTR_LOCAL_TEMP] = "local_temperature"
        else:
            attrs[self.ATTR_INTERNAL_TEMP] = "internal_temperature"
        return attrs

    # --- COMMANDS ---
    async def set_heating_setpoint(self, temperature: float):
        """Set heating setpoint in degrees Celsius."""
        temperature = max(self._min_heat, min(self._max_heat, float(temperature)))
        value = int(temperature * 100)

        logger.info(f"[{self.device.ieee}] Writing occupied_heating_setpoint: {temperature}°C ({value} centidegrees)")

        try:
            result = await self.cluster.write_attributes({"occupied_heating_setpoint": value})
            logger.error(f"[{self.device.ieee}] Write result: {result}")

            # Optimistic update
            self.device.update_state({
                "heating_setpoint": temperature,
                "occupied_heating_setpoint": temperature,
                "target_temp": temperature,
            })
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Write failed: {e}")
            import traceback
            traceback.print_exc()

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
        base_topic = self.device.service.mqtt.base_topic

        # 1. Base entities (Climate + Sensor)
        configs = [
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

        # 2. Add Schedule Input Entity (Text)
        configs.append({
            "component": "text",
            "object_id": "schedule_json",
            "config": {
                "name": f"Thermostat{name_suffix} Schedule",
                "command_topic": "CMD_TOPIC_PLACEHOLDER",
                "command_template": '{"command": "set_schedule", "value": {{ value }} }',
                "icon": "mdi:calendar-clock",
                "entity_category": "config"
            }
        })

        return configs

# ============================================================
# HIVE RECEIVER QUIRK (SLR1c, SLR1b)
# ============================================================
class HiveReceiverHandler(ThermostatHandler):
    """
    Hive-specific thermostat handler for SLR1c/SLR1b receivers.

    The Hive system splits HVAC across two devices:
    - SLT (Thermostat): Battery device, measures temperature, sends via binding
    - SLR (Receiver): Mains powered, controls boiler relay, receives temperature

    Pairing sequence: permit join > join heatlink > permit join via heatlink > join thermostat.
    The thermostat binds its output 0x0201 to the receiver's input 0x0201 during join.

    Key differences from generic ThermostatHandler:
    - system_mode=heat means relay is closed = boiler IS firing
    - local_temperature (0x0000) received via binding from SLT is often 0
    - internal_temperature (0x4000) is Hive manufacturer-specific (often unsupported)
    - Temperature is cross-referenced from paired SLT thermostat state cache
    """

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        logger.info(f"[{self.device.ieee}] Hive Receiver quirk active (HiveReceiverHandler)")

    # --- CONFIGURE: fetch initial thermostat temperature ---

    async def configure(self):
        """Configure + fetch initial temperature from paired thermostat."""
        result = await super().configure()
        self._fetch_thermostat_temperature()
        return result

    # --- COMMANDS: all control via thermostat setpoint ---
    # The Hive thermostat is fully autonomous. It compares measured temperature
    # against setpoint and pushes system_mode + running_state + setpoint to the
    # receiver via binding. No mode writes needed — just set the setpoint.
    # setpoint > current_temp → heating starts
    # setpoint < current_temp → heating stops

    async def set_target_temperature(self, temperature: float):
        """Write setpoint via the paired thermostat."""
        thermostat = self._find_paired_thermostat()
        if thermostat:
            th = thermostat.handlers.get(0x0201)
            if th:
                await th.set_target_temperature(temperature)
                logger.info(
                    f"[{self.device.ieee}] Setpoint {temperature}°C "
                    f"written via thermostat [{thermostat.ieee}]"
                )
                self.device.update_state({
                    "occupied_heating_setpoint": temperature,
                    "heating_setpoint": temperature,
                    "target_temp": temperature,
                })
                return

        logger.warning(f"[{self.device.ieee}] No paired thermostat for setpoint write")
        await super().set_target_temperature(temperature)

    async def set_hvac_mode(self, mode: str):
        """Mode is controlled by the thermostat — route setpoint-based commands."""
        mode = mode.lower()

        thermostat = self._find_paired_thermostat()
        if not thermostat:
            logger.warning(f"[{self.device.ieee}] No paired thermostat for mode change")
            return

        th = thermostat.handlers.get(0x0201)
        if not th:
            return

        if mode == "off":
            # Set thermostat setpoint to frost protection — thermostat stops heating
            await th.set_target_temperature(7.0)
            logger.info(f"[{self.device.ieee}] Heating OFF — setpoint 7°C via thermostat")
        elif mode == "boost":
            # Set thermostat setpoint high — thermostat triggers heating
            current_temp = thermostat.state.get("temperature", 20.0) or 20.0
            boost_temp = max(float(current_temp) + 3.0, 22.0)
            await th.set_target_temperature(boost_temp)
            logger.info(f"[{self.device.ieee}] BOOST — setpoint {boost_temp}°C via thermostat")
        else:
            # For heat/auto — just pass through to thermostat
            await th.set_hvac_mode(mode)
            logger.info(f"[{self.device.ieee}] Mode '{mode}' via thermostat")

        self._update_hvac_action()

    # --- HVAC ACTION: relay-based ---

    def _update_hvac_action(self):
        """For receivers, system_mode=heat means relay is closed = actively heating."""
        state = self.device.state
        mode = state.get("system_mode", "off")

        if mode == "off":
            action = "off"
        elif mode in ("heat", "auto", "emergency_heat"):
            # Receiver relay is closed when mode is heat — boiler IS firing
            action = "heating"
        elif mode == "cool":
            action = "cooling"
        else:
            action = "idle"

        self.device.update_state({"hvac_action": action})

    # --- ATTRIBUTE UPDATES: set all temperature keys ---

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        """Receiver sets all temperature aliases for any temperature attribute."""
        if value is None:
            return

        if hasattr(value, 'value'):
            value = value.value

        # For temperature attributes, set all 4 keys
        if attrid in (self.ATTR_LOCAL_TEMP, self.ATTR_INTERNAL_TEMP):
            parsed = self.parse_value(attrid, value)
            if parsed == 0:
                logger.debug(f"[{self.device.ieee}] Ignoring invalid 0 temperature reading.")
                return
            self.device.update_state({
                "local_temperature": parsed,
                "internal_temperature": parsed,
                "current_temperature": parsed,
                "temperature": parsed,
            })
            self._update_hvac_action()
            return

        # Everything else: delegate to generic handler
        super().attribute_updated(attrid, value, timestamp)

    # --- POLLING: dual temp attrs + cross-reference ---

    def get_pollable_attributes(self) -> Dict[int, str]:
        attrs = super().get_pollable_attributes()
        # Also poll 0x4000 (Hive manufacturer-specific)
        attrs[self.ATTR_INTERNAL_TEMP] = "internal_temperature"
        return attrs

    async def poll(self) -> Dict[str, Any]:
        """After polling, cross-reference thermostat's temperature and setpoint."""
        results = await super().poll()

        thermostat = self._find_paired_thermostat()
        if thermostat:
            # Temperature: always prefer thermostat's fresh value
            temp = thermostat.state.get("temperature")
            if temp is not None and temp != 0:
                results["local_temperature"] = temp
                self.device.update_state({
                    "local_temperature": temp,
                    "internal_temperature": temp,
                    "current_temperature": temp,
                    "temperature": temp,
                })

            # Setpoint: receiver EP5 always reads 1.0 (firmware-controlled)
            # Thermostat holds the real setpoint
            setpoint = thermostat.state.get("occupied_heating_setpoint")
            if setpoint is not None and setpoint > 1.0:
                results["occupied_heating_setpoint"] = setpoint
                self.device.update_state({
                    "occupied_heating_setpoint": setpoint,
                    "heating_setpoint": setpoint,
                    "target_temp": setpoint,
                })
        else:
            # No paired thermostat — strip zero temps
            temp_keys = ["local_temperature", "internal_temperature", "temperature"]
            has_valid_temp = any(
                k in results and results[k] is not None and results[k] != 0
                for k in temp_keys
            )
            if not has_valid_temp:
                for k in temp_keys:
                    results.pop(k, None)

        return results

    # --- HA DISCOVERY: receiver-specific name ---

    def get_discovery_configs(self) -> List[Dict]:
        configs = super().get_discovery_configs()
        for cfg in configs:
            if cfg.get("component") == "climate":
                cfg["config"]["name"] = "Thermostat Receiver"
            elif cfg.get("object_id") == "schedule_json":
                cfg["config"]["name"] = "Thermostat Receiver Schedule"
        return configs

    # --- HIVE PAIRING HELPERS ---

    def _find_paired_thermostat(self):
        """Find the paired Hive SLT thermostat from device_settings."""
        try:
            # Read explicit pairing from device_settings (set during join sequence)
            settings = self.device.service.device_settings.get(self.device.ieee, {})
            paired_ieee = settings.get("paired_thermostat")

            if paired_ieee and paired_ieee in self.device.service.devices:
                return self.device.service.devices[paired_ieee]

            if paired_ieee:
                logger.warning(
                    f"[{self.device.ieee}] Paired thermostat [{paired_ieee}] "
                    f"not found in device list"
                )
                return None

            # No pairing stored — log warning, don't guess
            logger.warning(
                f"[{self.device.ieee}] No paired_thermostat in device_settings. "
                f"Re-pair thermostat via 'Permit Join via Heatlink' to establish pairing."
            )
            return None

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to find paired thermostat: {e}")
        return None

    def _fetch_thermostat_temperature(self):
        """Cross-reference paired thermostat's last known temperature."""
        thermostat = self._find_paired_thermostat()
        if not thermostat:
            return

        temp = thermostat.state.get("temperature")
        if temp is not None and temp != 0:
            self.device.update_state({
                "local_temperature": temp,
                "internal_temperature": temp,
                "current_temperature": temp,
                "temperature": temp,
            })
            logger.info(
                f"[{self.device.ieee}] Cross-referenced temperature "
                f"{temp}°C from thermostat [{thermostat.ieee}]"
            )


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