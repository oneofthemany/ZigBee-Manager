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

    # Minimal always-supported report config
    REPORT_CONFIG = [
        ("local_temperature", 60, 300, 25),
        ("occupied_heating_setpoint", 0, 300, 10),
        ("pi_heating_demand", 60, 300, 10),
        ("system_mode", 10, 300, 1),
        ("running_state", 10, 300, 1),
        ("running_mode", 10, 300, 1),
    ]

    async def configure(self):
        # Extend with cooling attrs only for mains-powered multi-mode devices
        model = str(self.device.zigpy_dev.model or "").upper()
        is_heat_only = any(m in model for m in ("AGL001", "SRTS", "TRV"))
        if not is_heat_only:
            self.REPORT_CONFIG = self.REPORT_CONFIG + [
                ("occupied_cooling_setpoint", 0, 300, 10),
                ("unoccupied_heating_setpoint", 0, 300, 10),
                ("pi_cooling_demand", 60, 300, 10),
                ("occupancy", 10, 300, 1),
            ]
        return await super().configure()

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
            logger.info(
                f"[{self.device.ieee}] Detected as Hive Receiver (SLR). "
                f"Atomic write protocol enabled."
            )

    # ============================================================
    # HIVE PAIRING HELPERS
    # ============================================================
    def _find_paired_thermostat(self):
        try:
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

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to find paired thermostat: {e}")
            return None

    def _fetch_thermostat_temperature(self):
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
    # CONFIGURE
    # ============================================================
    async def configure(self):
        await super().configure()

        init_attrs = [
            self.ATTR_ABS_MIN_HEAT_SETPOINT_LIMIT,
            self.ATTR_ABS_MAX_HEAT_SETPOINT_LIMIT,
            self.ATTR_MAX_HEAT_SETPOINT_LIMIT,
            self.ATTR_MIN_HEAT_SETPOINT_LIMIT,
            self.ATTR_LOCAL_TEMP_CALIBRATION,
            self.ATTR_CTRL_SEQUENCE_OF_OPER,
            self.ATTR_SYSTEM_MODE,
            self.ATTR_OCCUPIED_HEATING_SETPOINT,
        ]

        if self.is_receiver:
            init_attrs.append(self.ATTR_INTERNAL_TEMP)
            init_attrs.append(self.ATTR_TEMP_SETPOINT_HOLD)
            init_attrs.append(self.ATTR_TEMP_SETPOINT_HOLD_DURATION)
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

    # ============================================================
    # ATTRIBUTE HANDLING
    # ============================================================
    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None:
            return
        if hasattr(value, 'value'):
            value = value.value

        parsed_value = self.parse_value(attrid, value)

        if attrid in [self.ATTR_LOCAL_TEMP, self.ATTR_INTERNAL_TEMP] and parsed_value == 0:
            return

        updates = {}

        if attrid == self.ATTR_LOCAL_TEMP:
            if self.is_receiver:
                # Receiver gets temperature via binding from thermostat
                updates["internal_temperature"] = parsed_value
                updates["current_temperature"] = parsed_value  # HA climate needs this
                updates["temperature"] = parsed_value
                updates["local_temperature"] = parsed_value
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

        elif attrid == self.ATTR_TEMP_SETPOINT_HOLD:
            hold = bool(value) if isinstance(value, (int, bool)) else False
            updates["temperature_setpoint_hold"] = hold

        elif attrid == self.ATTR_TEMP_SETPOINT_HOLD_DURATION:
            updates["temperature_setpoint_hold_duration"] = int(value) if value is not None else 0

        if updates:
            self.device.update_state(updates)
            # Update derived HVAC action (if not already handled in running_state)
            if "hvac_action" not in updates:
                self._update_hvac_action()

    # ============================================================
    # WRITES — atomic for Hive receivers
    # ============================================================
    async def _write_atomic(self, attrs_by_name: Dict[str, Any],
                            attrs_by_id: Dict[int, Any]) -> bool:
        """
        Write a dict of attributes atomically. Tries named-string form first
        (which matches zigpy's attribute definitions), falls back to attr IDs
        if the named form fails (e.g. if zigpy doesn't know 'temp_setpoint_hold'
        by that exact name in older versions).
        """
        try:
            result = await self.cluster.write_attributes(attrs_by_name)
            logger.info(f"[{self.device.ieee}] Atomic write result: {result}")
            return True
        except Exception as e:
            logger.warning(
                f"[{self.device.ieee}] Named atomic write failed ({e}), "
                f"retrying with attribute IDs"
            )
        try:
            result = await self.cluster.write_attributes(attrs_by_id)
            logger.info(f"[{self.device.ieee}] Atomic write (by ID) result: {result}")
            return True
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Atomic write failed: {e}")
            return False

    async def set_target_temperature(self, temperature: float):
        """
        Set target heating setpoint in °C.

        For Hive receivers this sends a single atomic multi-attribute write
        per z2m's documented protocol:

            system_mode = heat
            temperature_setpoint_hold = 1
            occupied_heating_setpoint = <centidegrees>

        Writing these separately does NOT work — the receiver reverts the
        setpoint to its frost floor (1°C) between writes.
        """
        temperature = max(self._min_heat, min(self._max_heat, float(temperature)))
        value_cd = int(temperature * 100)

        if self.is_receiver:
            logger.info(
                f"[{self.device.ieee}] Hive atomic write: "
                f"system_mode=heat, hold=1, setpoint={temperature}°C ({value_cd} cd)"
            )
            ok = await self._write_atomic(
                attrs_by_name={
                    "system_mode": 0x04,
                    "temp_setpoint_hold": 1,
                    "occupied_heating_setpoint": value_cd,
                },
                attrs_by_id={
                    self.ATTR_SYSTEM_MODE: 0x04,
                    self.ATTR_TEMP_SETPOINT_HOLD: 1,
                    self.ATTR_OCCUPIED_HEATING_SETPOINT: value_cd,
                },
            )
            if not ok:
                return

            self.device.update_state({
                "system_mode": "heat",
                "system_mode_raw": 0x04,
                "temperature_setpoint_hold": True,
                "heating_setpoint": temperature,
                "occupied_heating_setpoint": temperature,
                "target_temp": temperature,
            })
            self._fetch_thermostat_temperature()
            return

        # --- Non-Hive thermostats: plain setpoint write ---
        logger.info(
            f"[{self.device.ieee}] Writing occupied_heating_setpoint: "
            f"{temperature}°C ({value_cd} centidegrees)"
        )
        await self.cluster.write_attributes({"occupied_heating_setpoint": value_cd})
        self.device.update_state({
            "heating_setpoint": temperature,
            "occupied_heating_setpoint": temperature,
            "target_temp": temperature,
        })

    async def set_hvac_mode(self, mode: str):
        """
        Set HVAC system mode.

        For Hive receivers, atomically writes system_mode + hold=false so
        the device is in a clean "unheld" state after the mode change.

        Note: when mode='off', the device automatically clamps setpoint to 1°C
        and duration to 0. This is documented upstream behaviour; we mirror
        those values in our cached state so downstream code (e.g. heating
        controller) doesn't see a stale setpoint and try to "correct" it.
        """
        mode_map = {"off": 0x00, "auto": 0x01, "heat": 0x04}
        mode = mode.lower()

        if mode not in mode_map:
            logger.warning(f"[{self.device.ieee}] Unsupported HVAC mode: {mode}")
            return

        mode_val = mode_map[mode]

        # --- HIVE PATH: SLT is master ---
        if self.is_receiver:
            logger.info(
                f"[{self.device.ieee}] Hive atomic mode write: "
                f"system_mode={mode}, hold=0"
            )
            ok = await self._write_atomic(
                attrs_by_name={
                    "system_mode": mode_val,
                    "temp_setpoint_hold": 0,
                },
                attrs_by_id={
                    self.ATTR_SYSTEM_MODE: mode_val,
                    self.ATTR_TEMP_SETPOINT_HOLD: 0,
                },
            )
            if not ok:
                return

            updates = {
                "system_mode": mode,
                "system_mode_raw": mode_val,
                "temperature_setpoint_hold": False,
            }
            if mode == "off":
                # Device will auto-clamp these; mirror locally to avoid
                # heating_controller seeing a stale setpoint and hammering
                # the device to "fix" it.
                updates.update({
                    "occupied_heating_setpoint": 1.0,
                    "heating_setpoint": 1.0,
                    "target_temp": 1.0,
                    "temperature_setpoint_hold_duration": 0,
                })
            self.device.update_state(updates)
            self._fetch_thermostat_temperature()
            return

        # --- Non-Hive thermostats: plain mode write ---
        logger.info(f"[{self.device.ieee}] Writing system_mode: {mode} ({mode_val})")
        await self.cluster.write_attributes({"system_mode": mode_val})
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
            is_heating = isinstance(run_state, int) and bool(run_state & 0x01)
            action = "heating" if is_heating else "idle"

        self.device.update_state({"hvac_action": action})

    def process_command(self, command: str, value: Any):
        if command in ("temperature", "set_temperature"):
            asyncio.create_task(self.set_target_temperature(float(value)))
        elif command in ("system_mode", "set_mode"):
            asyncio.create_task(self.set_hvac_mode(str(value).lower()))

    async def handle_command(self, command: str, data: Any):
        if command == "system_mode":
            await self.set_system_mode(data)
        elif command in ["temperature", "temperature_setpoint"]:
            try:
                temp = float(data)
                await self.set_heating_setpoint(temp)
            except ValueError:
                logger.error(f"[{self.device.ieee}] Invalid temperature value: {data}")
        elif command == "set_schedule":
            await self.set_weekly_schedule(data)

    async def set_weekly_schedule(self, schedule_data: Dict):
        """
        Send SetWeeklySchedule command to device.

        For Hive receivers, route the schedule to the paired SLT master.
        The SLR doesn't own a schedule — the SLT does.

        Expected data format:
        {
            "day_of_week": 1, # Bitmask
            "transitions": [
                {"time": 360, "heat": 20.0},
                ...
            ]
        }
        """
        # 1. Parse Day of Week (Bitmask)
        day_bitmap = schedule_data.get("day_of_week", 0xFF)

        # 2. Parse Transitions
        raw_transitions = schedule_data.get("transitions", [])
        payload = []

        for t in raw_transitions:
            payload.append(int(t["time"]))
            payload.append(int(float(t["heat"]) * 100))

        try:
            logger.info(
                f"[{self.device.ieee}] Sending Schedule: day={day_bitmap} "
                f"-> {len(raw_transitions)} transitions"
            )
            await self.cluster.set_weekly_schedule(
                len(raw_transitions), day_bitmap, 1, payload
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
        if attrid == self.ATTR_OCCUPIED_HEATING_SETPOINT:
            return "occupied_heating_setpoint"
        if attrid == self.ATTR_SYSTEM_MODE:
            return "system_mode"
        if attrid == self.ATTR_PI_HEATING_DEMAND:
            return "heating_demand"
        if attrid == self.ATTR_RUNNING_STATE:
            return "running_state"
        if attrid == self.ATTR_TEMP_SETPOINT_HOLD:
            return "temperature_setpoint_hold"
        if attrid == self.ATTR_TEMP_SETPOINT_HOLD_DURATION:
            return "temperature_setpoint_hold_duration"
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
            attrs[self.ATTR_TEMP_SETPOINT_HOLD] = "temperature_setpoint_hold"
            attrs[self.ATTR_TEMP_SETPOINT_HOLD_DURATION] = "temperature_setpoint_hold_duration"
        else:
            attrs[self.ATTR_INTERNAL_TEMP] = "internal_temperature"
        return attrs

    # --- COMMANDS ---
    async def set_heating_setpoint(self, temperature: float):
        await self.set_target_temperature(temperature)

    async def set_system_mode(self, mode: str):
        if isinstance(mode, int):
            mode = self.SYSTEM_MODES.get(mode, "off")
        if isinstance(mode, str):
            await self.set_hvac_mode(mode.lower())

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
# USER INTERFACE CLUSTER (0x0204)
# ============================================================
@register_handler(0x0204)
class UserInterfaceHandler(ClusterHandler):
    CLUSTER_ID = 0x0204
    ATTR_KEYPAD_LOCKOUT = 0x0001
    LOCKOUT_MODES = {
        0x00: "No Lockout", 0x01: "Level 1", 0x02: "Level 2",
        0x03: "Level 3", 0x04: "Level 4", 0x05: "Level 5",
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_KEYPAD_LOCKOUT:
            if hasattr(value, 'value'):
                value = value.value
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
    FAN_MODES = {
        0x00: "off", 0x01: "low", 0x02: "medium", 0x03: "high",
        0x04: "on", 0x05: "auto", 0x06: "smart",
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_FAN_MODE:
            mode = self.FAN_MODES.get(value, f"unknown_{value}")
            self.device.update_state({"fan_mode": mode})

    async def set_fan_mode(self, mode: str):
        mode_map = {v: k for k, v in self.FAN_MODES.items()}
        if mode.lower() in mode_map:
            await self.cluster.write_attributes({"fan_mode": mode_map[mode.lower()]})