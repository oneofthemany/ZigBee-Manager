"""
General cluster handlers for Zigbee devices.
Handles: On/Off, Level Control, Color, Scenes, Groups, Basic
"""
import logging
from typing import Any, Dict, List, Optional
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
        """Check if this endpoint is a light (not a controller)."""
        ep = self.endpoint

        # Must have OnOff in INPUTS to be controllable
        if 0x0006 not in ep.in_clusters:
            return False

        has_level = 0x0008 in ep.in_clusters  # Not out_clusters!
        has_color = 0x0300 in ep.in_clusters
        has_lightlink = 0x1000 in ep.in_clusters
        has_opple = 0xFCC0 in ep.in_clusters
        has_electrical = 0x0B04 in ep.in_clusters

        has_lighting_cluster = (has_level or has_color or has_lightlink or has_opple)

        return has_lighting_cluster and not has_electrical


    def _handle_on_with_timed_off(self, args):
        """Handle on_with_timed_off command from motion sensors (Philips Hue)."""
        try:
            import time
            now = time.time()
            last_trigger = getattr(self.device, '_last_motion_trigger', 0)

            if now - last_trigger < 1.0:
                logger.debug(f"[{self.device.ieee}] Ignoring duplicate motion trigger "
                             f"({now - last_trigger:.3f}s since last)")
                return

            self.device._last_motion_trigger = now

            # Extract timeout
            on_time_from_cmd = 3000  # Default 300 seconds
            if args and len(args) >= 2 and hasattr(args[1], 'value'):
                on_time_from_cmd = args[1].value
            on_time_seconds = on_time_from_cmd / 10 if on_time_from_cmd else 0

            # PREFER user-configured timeout
            configured_timeout = self.device.state.get('motion_timeout')
            if configured_timeout is not None and configured_timeout > 0:
                timeout = configured_timeout
                logger.debug(f"[{self.device.ieee}] Using configured timeout: {timeout}s")
            else:
                timeout = on_time_seconds
                self.device.update_state({"motion_timeout": timeout})

            # Update state
            self.device.update_state({
                "occupancy": True,
                "motion": True,
                "presence": True,
                "motion_on_time": on_time_seconds,
                "state": "ON",
                "on": True
            })

            # Fast-path MQTT publish
            if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                import json
                safe_name = self.device.service.get_safe_name(self.device.ieee)
                payload = json.dumps({'occupancy': True, 'motion': True, 'presence': True})
                self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

            # Schedule auto-clear
            if timeout > 0:
                import asyncio
                if hasattr(self.device, '_motion_clear_task') and self.device._motion_clear_task:
                    self.device._motion_clear_task.cancel()
                    logger.debug(f"[{self.device.ieee}] Cancelled previous motion timer")

                self.device._motion_clear_task = asyncio.create_task(
                    self._clear_motion_after(timeout)
                )
                logger.info(f"[{self.device.ieee}] Motion detected, will auto-clear in {timeout}s")

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

            # Fast-path MQTT publish
            if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                import json
                safe_name = self.device.service.get_safe_name(self.device.ieee)
                payload = json.dumps({'occupancy': False, 'motion': False, 'presence': False})
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

                self.device.update_state(updates, endpoint_id=self.endpoint.endpoint_id)

                # Only publish if NO IAS Zone handler (avoid duplicate publishes)
                if 0x0500 not in self.endpoint.in_clusters:
                    # Contact sensors MUST publish immediately via the fast path
                    if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                        import json
                        safe_name = self.device.service.get_safe_name(self.device.ieee)

                        # Build minimal payload - only contact sensor fields + metadata
                        payload = {
                            'available': True,
                            'linkquality': self.device.state.get('lqi', 0),
                            'last_seen': self.device.last_seen,
                            f'contact_{self.endpoint.endpoint_id}': updates[f'contact_{self.endpoint.endpoint_id}'],
                            f'is_open_{self.endpoint.endpoint_id}': updates[f'is_open_{self.endpoint.endpoint_id}'],
                            f'is_closed_{self.endpoint.endpoint_id}': updates[f'is_closed_{self.endpoint.endpoint_id}'],
                            f'state_{self.endpoint.endpoint_id}': updates[f'state_{self.endpoint.endpoint_id}'],
                        }

                        if self.endpoint.endpoint_id == 1:
                            payload.update({
                                'contact': updates['contact'],
                                'is_open': updates['is_open'],
                                'is_closed': updates['is_closed'],
                                'state': updates['state'],
                            })

                        self.device.service.mqtt.publish_fast(f"{safe_name}", json.dumps(payload), qos=1, retain=True)
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
        """Detect if this OnOff cluster is from a contact sensor."""
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


    async def apply_configuration(self, updates: Dict[str, Any]):
        """Apply OnOff cluster configuration (startup behavior)."""
        ep_id = self.endpoint.endpoint_id
        key = f"startup_behavior_{ep_id}"

        if key in updates:
            try:
                value = int(updates[key])
                await self.cluster.write_attributes({self.ATTR_STARTUP_ON_OFF: value})
                logger.info(f"[{self.device.ieee}] Set EP{ep_id} startup behavior: {value}")
            except Exception as e:
                logger.warning(f"[{self.device.ieee}] Failed to set startup behavior EP{ep_id}: {e}")


    # --- HA Discovery ---
    def get_discovery_configs(self) -> List[Dict]:
        ep = self.endpoint.endpoint_id


        # ===== STEP 1: Check if this is a sensor endpoint FIRST =====
        is_contact_sensor = self._is_contact_sensor()
        has_only_sensor_clusters = len(self.endpoint.in_clusters) <= 4 and 0x0500 in self.endpoint.in_clusters

        # Contact sensors get binary_sensor discovery regardless of OnOff direction
        if is_contact_sensor or has_only_sensor_clusters:
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

        # ===== STEP 2: Check OnOff direction for NON-SENSOR endpoints =====
        has_onoff_input = 0x0006 in self.endpoint.in_clusters
        has_onoff_output = 0x0006 in self.endpoint.out_clusters

        # If OnOff is OUTPUT-only and NOT a sensor, this is a controller (skip)
        if has_onoff_output and not has_onoff_input:
            logger.debug(f"[{self.device.ieee}] EP{ep} has OnOff in OUTPUT only - controller endpoint, skipping")
            return []

        # If no OnOff in INPUT at all, nothing to control
        if not has_onoff_input:
            logger.debug(f"[{self.device.ieee}] EP{ep} has no OnOff in INPUT - skipping")
            return []

        # ===== STEP 3: Detect capabilities (INPUT clusters only) =====
        has_lightlink = 0x1000 in self.endpoint.in_clusters
        has_opple = 0xFCC0 in self.endpoint.in_clusters
        has_color = 0x0300 in self.endpoint.in_clusters
        has_level = 0x0008 in self.endpoint.in_clusters
        has_electrical = 0x0B04 in self.endpoint.in_clusters
        has_multi_state = 0x0012 in self.endpoint.in_clusters
        has_sonoff = 0xFC11 in self.endpoint.in_clusters

        # Sonoff devices are never contact sensors
        if has_sonoff:
            is_contact_sensor = False  # Already handled above - kept for clarity

        # ===== STEP 4: Light vs Switch detection =====
        if (has_electrical and has_level or has_multi_state or has_sonoff) and not (has_color or has_lightlink):
            is_light = False
            logger.info(f"[{self.device.ieee}] EP{ep} Force SWITCH: Electrical/Multistate/Sonoff present")
        else:
            is_light = has_lightlink or has_opple or has_color or has_level
            logger.info(f"[{self.device.ieee}] EP{ep} OnOff detected as: {'LIGHT' if is_light else 'SWITCH'} "
                        f"(lightlink={has_lightlink}, opple={has_opple}, color={has_color}, level={has_level})")

        component = "light" if is_light else "switch"
        configs = []

        # === LIGHTS: JSON SCHEMA ===
        if is_light:
            config = {
                "name": None,
                "schema": "json",
            }
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
            config["effect"] = True
            config["effect_list"] = ["blink", "breathe", "okay", "channel_change", "finish_effect", "stop_effect"]

            configs.append({
                "component": component,
                "object_id": f"{component}_{ep}" if ep > 1 else component,
                "config": config
            })

        # === SWITCHES: TEMPLATE SCHEMA ===
        else:
            config = {
                "name": f"Switch {ep}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "value_template": f"{{{{ value_json.state_{ep} }}}}",
                "command_topic": "CMD_TOPIC_PLACEHOLDER",
                "command_template": f'{{"command": "{{{{ value }}}}", "endpoint": {ep}}}'
            }

            configs.append({"component": component, "object_id": f"{component}_{ep}", "config": config})

            # Add LED brightness control for sockets
            if not is_light and has_level and has_electrical:
                configs.append({
                    "component": "number",
                    "object_id": f"led_brightness_{ep}",
                    "config": {
                        "name": f"LED Brightness {ep}",
                        "entity_category": "diagnostic",
                        "min": 0,
                        "max": 100,
                        "value_template": f"{{{{ value_json.brightness_{ep} }}}}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": f'{{"command": "brightness", "value": {{{{ value }}}}, "endpoint": {ep}}}'
                    }
                })

            # Add Sonoff Specific Configuration Entities if supported
            if has_sonoff:
                # Example: Work Mode (0=Switch, 1=Turbo/Router) or Detach Relay
                configs.append({
                    "component": "select",
                    "object_id": f"start_up_on_off_{ep}",
                    "config": {
                        "name": f"Start Up Behavior {ep}",
                        "entity_category": "config",
                        "options": ["OFF", "ON", "TOGGLE", "PREVIOUS"],
                        "value_template": f"{{{{ value_json.startup_behavior_{ep} }}}}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": f'{{"command": "startup", "value": "{{{{ value }}}}", "endpoint": {ep}}}'
                    }
                })

        return configs

    # --- OPTIMISTIC UPDATES ADDED HERE ---
    async def turn_on(self):
        try:
            # Force use of INPUT cluster, not output
            in_cluster = self.endpoint.in_clusters.get(0x0006)
            if not in_cluster:
                logger.error(f"[{self.device.ieee}] No OnOff INPUT cluster!")
                return

            result = await in_cluster.on()
            logger.info(f"[{self.device.ieee}] ON result: {result}")

            success = False
            # Check if result is a list/tuple
            if result and isinstance(result, (list, tuple)):
                if hasattr(result[0], 'status') and result[0].status == 0:
                    success = True
                elif result[0] == 0:
                    success = True
            # Check if result is a direct Default_Response object
            elif hasattr(result, 'status') and result.status == 0:
                success = True

            if success:
                self._update_state(True)
            else:
                logger.error(f"[{self.device.ieee}] ON FAILED/Unexpected: {result}")

        except Exception as e:
            logger.error(f"[{self.device.ieee}] ON exception: {e}", exc_info=True)


    async def turn_off(self, transition_time: Optional[int] = None):
        """Turn off, optionally with transition via LevelControl."""
        try:
            in_cluster = self.endpoint.in_clusters.get(0x0006)
            if not in_cluster:
                logger.error(f"[{self.device.ieee}] No OnOff INPUT cluster!")
                return

            # If transition requested and LevelControl available, use it
            if transition_time is not None and 0x0008 in self.endpoint.in_clusters:
                level_cluster = self.endpoint.in_clusters[0x0008]
                result = await level_cluster.move_to_level_with_on_off(0, transition_time)
                logger.info(f"[{self.device.ieee}] OFF with transition: {transition_time/10}s")
            else:
                result = await in_cluster.off()
                logger.info(f"[{self.device.ieee}] OFF (instant)")

            # Standard success check...
            if result and isinstance(result, (list, tuple)):
                success = hasattr(result[0], 'status') and result[0].status == 0 or result[0] == 0
            elif hasattr(result, 'status'):
                success = result.status == 0
            else:
                success = True

            if success:
                self._update_state(False)
        except Exception as e:
            logger.error(f"[{self.device.ieee}] OFF exception: {e}", exc_info=True)

    async def toggle(self):
        try:
            # Force use of INPUT cluster
            in_cluster = self.endpoint.in_clusters.get(0x0006)
            if not in_cluster:
                logger.error(f"[{self.device.ieee}] No OnOff INPUT cluster!")
                return

            result = await in_cluster.toggle()
            logger.info(f"[{self.device.ieee}] TOGGLE result: {result}")

            success = False
            # Check if result is a list/tuple
            if result and isinstance(result, (list, tuple)):
                if hasattr(result[0], 'status') and result[0].status == 0:
                    success = True
                elif result[0] == 0:
                    success = True
            # Check if result is a direct Default_Response object
            elif hasattr(result, 'status') and result.status == 0:
                success = True

            if success:
                # Calculate the new state based on the current known state
                key = f"on_{self.endpoint.endpoint_id}"
                # Fallback to general "on" state if endpoint specific state isn't found
                current = self.device.state.get(key, self.device.state.get("on", False))
                self._update_state(not current)
            else:
                logger.error(f"[{self.device.ieee}] TOGGLE FAILED/Unexpected: {result}")

        except Exception as e:
            logger.error(f"[{self.device.ieee}] TOGGLE exception: {e}", exc_info=True)


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