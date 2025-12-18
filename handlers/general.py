"""
General cluster handlers for Zigbee devices.
Handles: On/Off, Level Control, Color, Scenes, Groups, Basic
"""
import logging
from typing import Any, Dict, List
import time

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.general")

# ============================================================
# ON/OFF CLUSTER (0x0006)
# ============================================================
@register_handler(0x0006)
class OnOffHandler(ClusterHandler):
    """
    Handles OnOff cluster (0x0006) for ALL device types.

    This cluster is used by:
    - Lights (bulbs, LED strips, etc.)
    - Switches (wall switches, smart plugs, etc.)
    - Buttons (as output cluster)
    - Sockets

    Device type detection is done intelligently based on what OTHER
    clusters are present on the same endpoint.
    """
    CLUSTER_ID = 0x0006
    REPORT_CONFIG = [("on_off", 0, 300, 1)]

    ATTR_ON_OFF = 0x0000
    ATTR_STARTUP_ON_OFF = 0x4003

    CMD_OFF = 0x00
    CMD_ON = 0x01
    CMD_TOGGLE = 0x02
    CMD_ON_WITH_TIMED_OFF = 0x42

    def cluster_command(self, tsn: int, command_id: int, args):
        logger.info(f"[{self.device.ieee}] On/Off cmd: 0x{command_id:02X}")
        if command_id == self.CMD_ON_WITH_TIMED_OFF:
            self._handle_on_with_timed_off(args)
        elif command_id == self.CMD_ON:
            self._update_state(True)
        elif command_id == self.CMD_OFF:
            self._update_state(False)
        elif command_id == self.CMD_TOGGLE:
            key = f"on_{self.endpoint.endpoint_id}"
            # Use endpoint state if avail, else global state
            current = self.device.state.get(key, self.device.state.get("on", False))
            self._update_state(not current)


    def _is_light_endpoint(self) -> bool:
        ep = self.endpoint

        has_level = 0x0008 in ep.in_clusters
        has_color = 0x0300 in ep.in_clusters
        has_lightlink = 0x1000 in ep.in_clusters
        has_opple = 0xFCC0 in ep.in_clusters
        has_electrical = 0x0B04 in ep.in_clusters

        has_lighting_cluster = (
                has_level
                or has_color
                or has_lightlink
                or has_opple
        )

        # ⚠️ Electrical Measurement present → treat as socket, not light
        return has_lighting_cluster and not has_electrical

    def _handle_on_with_timed_off(self, args):
        """
        Handle on_with_timed_off command from motion sensors (Philips Hue).

        Payload format: [on_off_control, on_time (deciseconds), off_wait_time]
        - on_time: How long to stay on (in 1/10 second units)

        IMPORTANT: Philips sensors send their configured PIR timeout in this command.
        This schedules auto-clear after the timeout since Philips never sends clear.
        """
        try:
            # Extract on_time from command arguments
            on_time_from_cmd = 3000  # Default 300 seconds
            if args and len(args) >= 2 and hasattr(args[1], 'value'):
                on_time_from_cmd = args[1].value
            on_time_seconds = on_time_from_cmd / 10 if on_time_from_cmd else 0

            # PREFER user-configured timeout over command timeout
            # This allows users to override sensor's hardware timeout
            configured_timeout = self.device.state.get('motion_timeout')
            if configured_timeout is not None and configured_timeout > 0:
                timeout = configured_timeout
                logger.debug(f"[{self.device.ieee}] Using configured timeout: {timeout}s "
                             f"(sensor sent: {on_time_seconds}s)")
            else:
                timeout = on_time_seconds
                # Store the sensor's timeout for future use
                self.device.update_state({"motion_timeout": timeout})

            # Update state to show motion detected
            self.device.update_state({
                "occupancy": True,
                "motion": True,
                "presence": True,
                "motion_on_time": on_time_seconds,  # Original from sensor
                "state": "ON",
                "on": True
            })

            # Fast-path MQTT publish for immediate update
            if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                import json
                safe_name = self.device.service.get_safe_name(self.device.ieee)
                payload = json.dumps({
                    'occupancy': True,
                    'motion': True,
                    'presence': True
                })
                self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

            # Schedule auto-clear after timeout
            if timeout > 0:
                import asyncio
                # Cancel any existing clear task for this handler
                if hasattr(self, '_clear_task') and self._clear_task:
                    self._clear_task.cancel()
                # Schedule new clear
                self._clear_task = asyncio.create_task(self._clear_motion_after(timeout))
                logger.info(f"[{self.device.ieee}] Motion detected via on_with_timed_off: "
                            f"will auto-clear in {timeout}s")

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error in on_with_timed_off: {e}")

    async def _clear_motion_after(self, seconds: float):
        """Clear motion after timeout expires."""
        try:
            import asyncio
            await asyncio.sleep(seconds)

            # Clear motion state
            self.device.update_state({
                "occupancy": False,
                "motion": False,
                "presence": False,
                "state": "OFF",
                "on": False
            })

            # Fast-path MQTT publish for clear
            if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                import json
                safe_name = self.device.service.get_safe_name(self.device.ieee)
                payload = json.dumps({
                    'occupancy': False,
                    'motion': False,
                    'presence': False
                })
                self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

            logger.info(f"[{self.device.ieee}] Motion auto-cleared after {seconds}s")

        except asyncio.CancelledError:
            logger.debug(f"[{self.device.ieee}] Motion clear cancelled (re-triggered)")
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error clearing motion: {e}")


    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_ON_OFF:
            # Determine if this is a contact sensor or a light/switch
            # Contact sensors have NO output clusters and NO lighting clusters
            is_contact_sensor = self._is_contact_sensor()

            if is_contact_sensor:
                # For contact sensors: true = OPEN, false = CLOSED
                is_open = bool(value)
                updates = {
                    f"contact_{self.endpoint.endpoint_id}": not is_open,  # contact: true = closed, false = open
                    f"is_open_{self.endpoint.endpoint_id}": is_open,
                    f"is_closed_{self.endpoint.endpoint_id}": not is_open,
                    f"state_{self.endpoint.endpoint_id}": "OPEN" if is_open else "CLOSED"
                }

                # Use EP1 keys as global keys for single-endpoint devices
                if self.endpoint.endpoint_id == 1:
                    updates.update({
                        "contact": not is_open,
                        "is_open": is_open,
                        "is_closed": not is_open,
                        "state": "OPEN" if is_open else "CLOSED"
                    })

                # Update state and trigger fast MQTT publish
                self.device.update_state(updates, endpoint_id=self.endpoint.endpoint_id)

                # Contact sensors MUST publish immediately via the fast path
                if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                    import json
                    safe_name = self.device.service.get_safe_name(self.device.ieee)

                    # The payload should contain the full device state
                    # for HA to read the value_template like `value_json.is_open_1`
                    payload = json.dumps(self.device.state)

                    # Publish to the main device state topic (not subtopic)
                    self.device.service.mqtt.publish_fast(f"{safe_name}", payload, qos=1, retain=False)
                    logger.debug(f"[{self.device.ieee}] Contact sensor fast-published state: {'OPEN' if is_open else 'CLOSED'}")

                logger.info(f"[{self.device.ieee}] Contact sensor: {'OPEN' if is_open else 'CLOSED'}")
            else:
                # For lights/switches: normal on/off handling
                self._update_state(bool(value))

        elif attrid == self.ATTR_STARTUP_ON_OFF:
            val = value.value if hasattr(value, 'value') else value
            self.device.update_state({
                f"startup_behavior_{self.endpoint.endpoint_id}": int(val)
            }, endpoint_id=self.endpoint.endpoint_id) # Ensure EP ID is passed up

    def _is_contact_sensor(self) -> bool:
        """
        Detect if this OnOff cluster is from a contact sensor.

        CRITICAL FIX: Overriding previous heuristic for multi-endpoint devices.
        If a device has more than one functional endpoint (EPs > 1), we assume
        it's a multi-gang switch/socket and MUST NOT treat any EP as a contact sensor,
        unless it only has the IAS Zone cluster (0x0500).
        """
        ep = self.endpoint

        # Count non-ZDO endpoints
        functional_endpoints = [e for e_id, e in self.device.zigpy_dev.endpoints.items() if e_id != 0]

        # Exclude multi-endpoint devices from the contact sensor heuristic
        if len(functional_endpoints) > 1:
            # But check if this EP is explicitly dedicated to a sensor cluster
            if 0x0500 in ep.in_clusters:
                logger.debug(f"[{self.device.ieee}] EP{ep.endpoint_id} is IAS Zone (sensor)")
                return True # It is a sensor, let IAS handler pick it up

            # Otherwise, for multi-gang, treat it as a switch/light
            logger.debug(f"[{self.device.ieee}] EP{ep.endpoint_id} Excluded from contact sensor check (Multi-endpoint device)")
            return False

        # Apply standard detection only for single-endpoint devices (like real contact sensors)
        # 1. Check if OnOff is in output clusters (contact sensor pattern)
        onoff_is_output = 0x0006 in [c.cluster_id for c in ep.out_clusters.values()]

        # 2. Check for lighting/level control clusters (if present, it's a light/switch)
        has_level_control = 0x0008 in [c.cluster_id for c in ep.in_clusters.values()]
        has_color_control = 0x0300 in [c.cluster_id for c in ep.in_clusters.values()]
        has_lightlink = 0x1000 in [c.cluster_id for c in ep.in_clusters.values()]

        is_light = has_level_control or has_color_control or has_lightlink

        # 3. Count input clusters (contact sensors typically have very few)
        input_cluster_count = len(ep.in_clusters)

        # Contact sensor if:
        # - OnOff is in outputs AND not a light OR
        # - No lighting clusters AND minimal overall clusters
        if onoff_is_output and not is_light:
            logger.debug(f"[{self.device.ieee}] EP{ep.endpoint_id} Detected as contact sensor (OnOff in outputs)")
            return True

        if not is_light and input_cluster_count <= 6:
            logger.debug(f"[{self.device.ieee}] EP{ep.endpoint_id} Detected as contact sensor (minimal clusters, no lighting)")
            return True

        return False

    def _update_state(self, is_on: bool):
        """Helper to update state with endpoint awareness."""
        ep_id = self.endpoint.endpoint_id

        updates = {
            f"state_{ep_id}": "ON" if is_on else "OFF",
            f"on_{ep_id}": is_on
        }

        # Update global state only if EP1 or global missing
        if ep_id == 1 or "on" not in self.device.state:
            updates["state"] = "ON" if is_on else "OFF"
            updates["on"] = is_on

        # Mark this update as requiring retained MQTT publish IF this is a light
        if self._is_light_endpoint():
            updates["_retain"] = True

        self.device.update_state(updates, endpoint_id=ep_id)



    def parse_value(self, attrid: int, value: Any) -> Any:
        """Convert OnOff attribute values to proper format."""
        if attrid == self.ATTR_ON_OFF:
            # Convert numeric/boolean to "ON"/"OFF" string
            if isinstance(value, (bool, int)):
                return "ON" if bool(value) else "OFF"
        return value


    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_ON_OFF: return "state"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_ON_OFF: "state",
            self.ATTR_STARTUP_ON_OFF: f"startup_behavior_{self.endpoint.endpoint_id}"
        }

    def get_configuration_options(self) -> List[Dict]:
        return [{
            "name": f"startup_behavior_{self.endpoint.endpoint_id}",
            "label": f"Power On Behavior (EP{self.endpoint.endpoint_id})",
            "type": "select",
            "options": [
                {"value": 0, "label": "Off"}, {"value": 1, "label": "On"},
                {"value": 2, "label": "Toggle"}, {"value": 255, "label": "Previous"}
            ],
            "description": "State after power loss",
            "attribute_id": self.ATTR_STARTUP_ON_OFF
        }]

    # --- HA Discovery ---
    def get_discovery_configs(self) -> List[Dict]:
        ep = self.endpoint.endpoint_id

        # Check if this is a contact sensor (keep existing logic)
        is_contact_sensor = self._is_contact_sensor()
        has_only_sensor_clusters = len(self.endpoint.in_clusters) <= 4 and 0x0500 in self.endpoint.in_clusters

        if is_contact_sensor or has_only_sensor_clusters:
            # Binary sensors stay template-based
            return [{
                "component": "binary_sensor",
                "object_id": f"contact_{ep}",
                "config": {
                    "name": f"Contact Sensor {ep}",
                    "device_class": "door",
                    "value_template": f"{{{{ value_json.contact_{ep} }}}}",
                    "payload_on": True,
                    "payload_off": False
                }
            }]

        # Determine capabilities
        has_lightlink = 0x1000 in self.endpoint.in_clusters or 0x1000 in self.endpoint.out_clusters
        has_opple = 0xFCC0 in self.endpoint.in_clusters or 0xFCC0 in self.endpoint.out_clusters
        has_color = 0x0300 in self.endpoint.in_clusters
        has_level = 0x0008 in self.endpoint.in_clusters
        has_electrical = 0x0B04 in self.endpoint.in_clusters

        if has_electrical and has_level and not (has_color or has_lightlink):
            is_light = False
        else:
            is_light = has_lightlink or has_opple or has_color or has_level

        component = "light" if is_light else "switch"
        configs = []

        # === JSON SCHEMA CONFIG) ===
        config = {
            "name": None,  # Use device name only
            "schema": "json",
            # state_topic and command_topic are injected by mqtt.py
        }

        if is_light:
            # Determine supported_color_modes
            color_modes = []

            if has_color:
                color_modes.extend(["color_temp", "xy"])
                config["min_mireds"] = 153
                config["max_mireds"] = 500
            elif has_level:
                color_modes.append("brightness")
            else:
                color_modes.append("onoff")

            config["supported_color_modes"] = color_modes
            config["brightness_scale"] = 254 if has_level else 100

            # Effects
            config["effect"] = True
            config["effect_list"] = ["blink", "breathe", "okay", "channel_change", "finish_effect", "stop_effect"]

        configs.append({
            "component": component,
            "object_id": f"{component}_{ep}" if ep > 1 else component,
            "config": config
        })

        return configs

    # --- OPTIMISTIC UPDATES ADDED HERE ---
    async def turn_on(self):
        try:
            logger.debug(f"[{self.device.ieee}] Calling cluster.on() on EP{self.endpoint.endpoint_id}")
            result = await self.cluster.on()
            logger.debug(f"[{self.device.ieee}] cluster.on() result: {result}")
            self._update_state(True)
            logger.info(f"[{self.device.ieee}] Sent ON command (Optimistic)")
        except Exception as e:
            logger.error(f"[{self.device.ieee}] cluster.on() FAILED: {type(e).__name__}: {e}")
            raise

    async def turn_off(self):
        await self.cluster.off()
        self._update_state(False) # Optimistic update
        logger.info(f"[{self.device.ieee}] Sent OFF command (Optimistic)")

    async def toggle(self):
        await self.cluster.toggle()
        # Optimistic toggle is harder, we assume flip based on current knowledge
        key = f"on_{self.endpoint.endpoint_id}"
        current = self.device.state.get(key, self.device.state.get("on", False))
        self._update_state(not current)
        logger.info(f"[{self.device.ieee}] Sent TOGGLE command (Optimistic)")


# ============================================================
# LEVEL CONTROL CLUSTER (0x0008)
# ============================================================
@register_handler(0x0008)
class LevelControlHandler(ClusterHandler):
    CLUSTER_ID = 0x0008
    REPORT_CONFIG = [("current_level", 1, 300, 5)]
    ATTR_CURRENT_LEVEL = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_CURRENT_LEVEL:
            if value is not None and value != 0xFF:
                self._update_level(value)

    def _update_level(self, level):
        pct = round((level / 254) * 100)
        ep_id = self.endpoint.endpoint_id
        updates = {
            f"brightness_{ep_id}": pct,
            f"level_{ep_id}": level
        }
        if ep_id == 1:
            updates["brightness"] = pct
            updates["level"] = level
        self.device.update_state(updates, endpoint_id=ep_id)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_CURRENT_LEVEL: "brightness"}

    async def set_level(self, level: int, transition_time: int = 10):
        await self.cluster.move_to_level(level, transition_time)
        self._update_level(level) # Optimistic

    async def set_brightness_pct(self, percent: int, transition_time: int = 10):
        level = round((percent / 100) * 254)
        await self.set_level(level, transition_time)

    def get_discovery_configs(self) -> List[Dict]:
        # OnOff handler detects LevelControl and adds brightness to light entity
        return []


@register_handler(0x0004)
class GroupsHandler(ClusterHandler):
    CLUSTER_ID = 0x0004

    async def add_to_group(self, gid, name=""): await self.cluster.add(gid, name)

    async def remove_from_group(self, gid): await self.cluster.remove(gid)

    async def get_groups(self):
        res = await self.cluster.get_membership([])
        return res[1] if res else []


@register_handler(0x0005)
class ScenesHandler(ClusterHandler):
    CLUSTER_ID = 0x0005
    ATTR_SCENE_COUNT = 0x0000
    ATTR_CURRENT_SCENE = 0x0001

    async def recall_scene(self, gid, sid): await self.cluster.recall(gid, sid)

    async def store_scene(self, gid, sid): await self.cluster.store(gid, sid)