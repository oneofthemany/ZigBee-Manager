"""
Matter Bridge — Proxies python-matter-server into ZigBee-Manager's
unified device list and WebSocket event stream.

Requires: python-matter-server running (e.g. ws://localhost:5580/ws)
Install:  pip install aiohttp

This module is entirely optional. If matter.server_url is not set in
config.yaml, the bridge is never instantiated and has zero impact.
"""

import asyncio
import json
import time
import logging
from typing import Dict, Optional, Callable, Any, List

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = logging.getLogger("matter_bridge")


# =============================================================================
# MATTER DEVICE WRAPPER
# =============================================================================

class MatterDevice:
    """
    Lightweight wrapper around a matter-server node.
    Mimics the interface that automation engine expects from ZigManDevice.
    """

    def __init__(self, node: dict):
        self.node = node
        self.node_id = node.get("node_id", 0)
        self.ieee = f"matter_{self.node_id}"
        self.state: Dict[str, Any] = {}
        self.last_seen = time.time()
        self._available = node.get("available", False)

        # Extract basic info from Matter Basic Information cluster (0/40/*)
        attributes = node.get("attributes", {})
        self.manufacturer = self._find_attr(attributes, 40, 2, "Unknown")   # VendorName
        self.model = self._find_attr(attributes, 40, 4, "") or \
                     self._find_attr(attributes, 40, 3, "Unknown")          # ProductName
        self.friendly_name = self._find_attr(attributes, 40, 5, "") or \
                             self.model or f"Matter {self.node_id}"         # NodeLabel

        # Build initial state
        self._build_state(attributes)

    @staticmethod
    def _find_attr(attributes: dict, cluster: int, attr: int, default=None):
        """
        Search for a Matter attribute across endpoints.
        Keys in matter-server are formatted as "endpoint/cluster/attribute".
        """
        # Try endpoint 0 first (Basic Information is always on EP 0)
        for ep in [0, 1, 2]:
            key = f"{ep}/{cluster}/{attr}"
            if key in attributes:
                return attributes[key]
        return default

    def _build_state(self, attributes: dict):
        """Build normalised state dict from Matter attributes."""
        self.state = {
            "protocol": "matter",
            "available": self._available,
            "node_id": self.node_id,
        }

        # On/Off (cluster 6, attr 0)
        on_off = self._find_attr(attributes, 6, 0)
        if on_off is not None:
            self.state["state"] = "ON" if on_off else "OFF"
            self.state["on"] = bool(on_off)

        # Level Control (cluster 8, attr 0) — 0-254
        level = self._find_attr(attributes, 8, 0)
        if level is not None:
            self.state["brightness"] = int(level)
            self.state["level"] = int(level / 2.54) if level > 0 else 0

        # Color Temperature (cluster 768, attr 7) — mireds
        color_temp = self._find_attr(attributes, 768, 7)
        if color_temp is not None and color_temp > 0:
            self.state["color_temp"] = int(color_temp)

        # Color XY (cluster 768, attr 3 & 4)
        color_x = self._find_attr(attributes, 768, 3)
        color_y = self._find_attr(attributes, 768, 4)
        if color_x is not None and color_y is not None:
            # Matter uses 0-65535, normalise to 0-1
            self.state["color_x"] = round(color_x / 65535, 4)
            self.state["color_y"] = round(color_y / 65535, 4)

        # Temperature Measurement (cluster 1026, attr 0) — centidegrees
        temp = self._find_attr(attributes, 1026, 0)
        if temp is not None:
            self.state["temperature"] = round(temp / 100.0, 1)

        # Humidity (cluster 1029, attr 0)
        humidity = self._find_attr(attributes, 1029, 0)
        if humidity is not None:
            self.state["humidity"] = round(humidity / 100.0, 1)

        # Occupancy (cluster 1030, attr 0)
        occupancy = self._find_attr(attributes, 1030, 0)
        if occupancy is not None:
            self.state["occupancy"] = bool(occupancy & 0x01)

        # Illuminance (cluster 1024, attr 0)
        illuminance = self._find_attr(attributes, 1024, 0)
        if illuminance is not None:
            self.state["illuminance"] = int(illuminance)

        # Contact/Door (cluster 69, attr 0) — BooleanState
        contact = self._find_attr(attributes, 69, 0)
        if contact is not None:
            self.state["contact"] = bool(contact)

    def update_from_node(self, node: dict):
        """Update device from a new node snapshot."""
        self.node = node
        self._available = node.get("available", self._available)
        self.last_seen = time.time()
        attributes = node.get("attributes", {})
        self._build_state(attributes)

        # Re-read labels in case they changed
        new_label = self._find_attr(attributes, 40, 5, "")
        if new_label:
            self.friendly_name = new_label

    def is_available(self) -> bool:
        return self._available

    def get_role(self) -> str:
        """Return device role for the device list."""
        return "Matter"

    def get_type(self) -> str:
        """Determine device type from state keys."""
        if "state" in self.state:
            if "brightness" in self.state or "color_temp" in self.state:
                return "Light"
            return "Switch"
        if "occupancy" in self.state:
            return "Sensor"
        if "temperature" in self.state:
            return "Sensor"
        if "contact" in self.state:
            return "Sensor"
        return "Matter"

    def get_control_commands(self) -> List[Dict[str, Any]]:
        """Return available commands based on state capabilities."""
        commands = []

        if "state" in self.state:
            commands.extend([
                {"command": "on", "label": "On", "endpoint_id": 1},
                {"command": "off", "label": "Off", "endpoint_id": 1},
                {"command": "toggle", "label": "Toggle", "endpoint_id": 1},
            ])

        if "brightness" in self.state:
            commands.append({
                "command": "brightness", "label": "Brightness",
                "type": "slider", "min": 0, "max": 100, "endpoint_id": 1
            })

        if "color_temp" in self.state:
            commands.append({
                "command": "color_temp", "label": "Color Temp",
                "type": "slider", "min": 2000, "max": 6500, "endpoint_id": 1
            })

        return commands

    def to_device_list_entry(self) -> dict:
        """Return dict matching ZigbeeService.get_device_list() format."""
        return {
            "ieee": self.ieee,
            "nwk": f"0x{self.node_id:04x}",
            "friendly_name": self.friendly_name,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "lqi": None,
            "last_seen_ts": self.last_seen,
            "state": self.state.copy(),
            "type": self.get_type(),
            "protocol": "matter",
            "quirk": None,
            "capabilities": self._get_capabilities(),
            "settings": {},
            "available": self._available,
            "config_schema": [],
            "polling_interval": 0,
        }

    def _get_capabilities(self) -> list:
        """Build capability list from state."""
        caps = ["matter"]
        if "state" in self.state:
            if "brightness" in self.state or "color_temp" in self.state:
                caps.append("light")
            else:
                caps.append("switch")
        if "brightness" in self.state:
            caps.append("level_control")
        if "color_temp" in self.state:
            caps.append("color_temperature")
        if "temperature" in self.state:
            caps.append("temperature_sensor")
        if "humidity" in self.state:
            caps.append("humidity_sensor")
        if "occupancy" in self.state:
            caps.append("motion_sensor")
        if "contact" in self.state:
            caps.append("contact_sensor")
        if "illuminance" in self.state:
            caps.append("illuminance_sensor")
        return caps


# =============================================================================
# MATTER BRIDGE
# =============================================================================

class MatterBridge:
    """
    Connects to python-matter-server WebSocket API and normalises
    Matter device data into the same format as ZigbeeService.
    """

    def __init__(self, server_url: str, event_callback: Optional[Callable] = None,
                 mqtt_service=None, base_topic: str = "zigbee_manager"):
        self.server_url = server_url  # e.g. ws://localhost:5580/ws
        self.event_callback = event_callback
        self.mqtt_service = mqtt_service
        self.base_topic = base_topic
        self.devices: Dict[str, MatterDevice] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._connected = False
        self._shutdown = False
        self._msg_id = 0

        # Friendly name overrides (loaded from data/matter_names.json)
        self._friendly_names: Dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    async def start(self):
        """Connect to python-matter-server and start listening."""
        if not HAS_AIOHTTP:
            logger.error("aiohttp not installed — Matter bridge disabled. "
                         "Install with: pip install aiohttp")
            return

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.server_url,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=10)
            )
            self._connected = True
            self._shutdown = False
            logger.info(f"✅ Connected to Matter server: {self.server_url}")

            # Request initial node list
            await self._send_command("get_nodes")

            # Start listener
            self._listen_task = asyncio.create_task(self._listen_loop())

        except Exception as e:
            logger.error(f"Failed to connect to Matter server ({self.server_url}): {e}")
            self._connected = False

            # Clean up the leaked session
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            self._ws = None

            # Schedule reconnect only if one isn't already running
            if not self._shutdown and (not self._reconnect_task or self._reconnect_task.done()):
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self):
        """Disconnect from matter-server."""
        self._shutdown = True
        self._connected = False

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("Matter bridge stopped")

    async def _reconnect_loop(self):
        """Attempt to reconnect to matter-server with backoff."""
        delay = 5
        while not self._shutdown:
            logger.info(f"Matter bridge reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            try:
                # Inline connect attempt — don't call start() to avoid recursive task spawning
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    self.server_url,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                self._connected = True
                logger.info(f"✅ Reconnected to Matter server: {self.server_url}")

                await self._send_command("get_nodes")
                self._listen_task = asyncio.create_task(self._listen_loop())
                return  # Success — exit loop

            except Exception as e:
                logger.warning(f"Matter reconnect failed: {e}")
                self._connected = False
                # Clean up leaked session
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                self._ws = None

            delay = min(delay * 2, 60)

    # =========================================================================
    # WEBSOCKET COMMUNICATION
    # =========================================================================

    async def _send_command(self, command: str, args: dict = None) -> str:
        """Send a command to python-matter-server and return message_id."""
        if not self._ws or self._ws.closed:
            raise ConnectionError("Not connected to matter-server")

        self._msg_id += 1
        msg_id = str(self._msg_id)

        msg = {
            "message_id": msg_id,
            "command": command,
        }
        if args:
            msg["args"] = args

        await self._ws.send_json(msg)
        logger.debug(f"Matter TX: {command} (id={msg_id})")
        return msg_id

    async def _listen_loop(self):
        """Listen for events from matter-server."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from matter-server: {msg.data[:200]}")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"Matter WS error: {self._ws.exception()}")
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Matter listener error: {e}")
        finally:
            self._connected = False
            if not self._shutdown:
                logger.warning("Matter server connection lost — scheduling reconnect")
                if not self._reconnect_task or self._reconnect_task.done():
                    self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _handle_message(self, data: dict):
        """Process a message from matter-server."""
        # Response to a command (e.g. get_nodes)
        if "result" in data and "message_id" in data:
            result = data["result"]
            if isinstance(result, list):
                # Node list response
                for node in result:
                    if isinstance(node, dict) and "node_id" in node:
                        await self._upsert_node(node)
                logger.info(f"Matter: loaded {len(result)} nodes")

                # Publish discovery for all devices after initial load
                await self._publish_all_discovery()
            return

        # Event-based messages
        event = data.get("event")

        if event == "node_added":
            node = data.get("data", {})
            if "node_id" in node:
                await self._upsert_node(node)
                logger.info(f"Matter: node {node['node_id']} added")

        elif event == "node_updated":
            node = data.get("data", {})
            if "node_id" in node:
                await self._upsert_node(node)

        elif event == "node_removed":
            node_id = data.get("data", {}).get("node_id")
            if node_id is not None:
                ieee = f"matter_{node_id}"
                self.devices.pop(ieee, None)
                logger.info(f"Matter: node {node_id} removed")

                # Remove HA discovery
                await self._remove_discovery(ieee)

                if self.event_callback:
                    await self.event_callback("device_left", {"ieee": ieee})

        elif event == "attribute_updated":
            # Granular attribute update
            node_id = data.get("data", {}).get("node_id")
            if node_id is not None:
                ieee = f"matter_{node_id}"
                if ieee in self.devices:
                    dev = self.devices[ieee]
                    # Re-fetch node for full state rebuild
                    # (matter-server sends partial updates, safer to rebuild)
                    attributes = dev.node.get("attributes", {})

                    attr_data = data.get("data", {})
                    attr_path = attr_data.get("attribute_path", "")
                    attr_value = attr_data.get("new_value")

                    if attr_path and attr_value is not None:
                        attributes[attr_path] = attr_value
                        dev.node["attributes"] = attributes
                        dev._build_state(attributes)
                        dev.last_seen = time.time()

                        if self.event_callback:
                            await self.event_callback("device_updated", {
                                "ieee": ieee,
                                "data": dev.state.copy()
                            })

                        # Publish state to MQTT
                        await self._publish_device_state(dev)

    async def _upsert_node(self, node: dict):
        """Insert or update a Matter device."""
        node_id = node["node_id"]
        ieee = f"matter_{node_id}"

        if ieee in self.devices:
            dev = self.devices[ieee]
            old_state = dev.state.copy()
            dev.update_from_node(node)
        else:
            dev = MatterDevice(node)
            self.devices[ieee] = dev
            old_state = {}

        # Apply friendly name override if set
        if ieee in self._friendly_names:
            dev.friendly_name = self._friendly_names[ieee]

        # Emit update if state changed
        if dev.state != old_state and self.event_callback:
            await self.event_callback("device_updated", {
                "ieee": ieee,
                "data": dev.state.copy()
            })

        # Publish state to MQTT
        await self._publish_device_state(dev)

    # =========================================================================
    # DEVICE COMMANDS
    # =========================================================================

    async def send_command(self, node_id: int, command: str, value=None) -> dict:
        """
        Send a command to a Matter device via matter-server.
        Maps normalised commands to Matter cluster operations.
        """
        try:
            if command in ("on", "off", "toggle"):
                command_name = command.capitalize()
                if command == "on":
                    command_name = "On"
                elif command == "off":
                    command_name = "Off"
                elif command == "toggle":
                    command_name = "Toggle"

                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": 1,
                    "cluster_id": 6,  # OnOff
                    "command_name": command_name,
                })

            elif command == "brightness":
                # Convert percentage (0-100) to Matter level (0-254)
                level = int((value or 0) * 2.54)
                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": 1,
                    "cluster_id": 8,  # LevelControl
                    "command_name": "MoveToLevelWithOnOff",
                    "args": {"level": level, "transition_time": 5},
                })

            elif command == "color_temp":
                # Convert Kelvin to mireds
                mireds = int(1000000 / max(value or 4000, 1))
                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": 1,
                    "cluster_id": 768,  # ColorControl
                    "command_name": "MoveToColorTemperature",
                    "args": {"color_temperature_mireds": mireds, "transition_time": 5},
                })

            else:
                return {"success": False, "error": f"Unknown command: {command}"}

            # Optimistic state update
            ieee = f"matter_{node_id}"
            if ieee in self.devices:
                dev = self.devices[ieee]
                if command == "on":
                    dev.state["state"] = "ON"
                    dev.state["on"] = True
                elif command == "off":
                    dev.state["state"] = "OFF"
                    dev.state["on"] = False
                elif command == "brightness" and value is not None:
                    dev.state["brightness"] = int((value or 0) * 2.54)
                    dev.state["level"] = value
                    if value > 0:
                        dev.state["state"] = "ON"
                        dev.state["on"] = True

                if self.event_callback:
                    await self.event_callback("device_updated", {
                        "ieee": ieee,
                        "data": dev.state.copy()
                    })

                await self._publish_device_state(dev)

            return {"success": True}

        except Exception as e:
            logger.error(f"Matter command failed (node {node_id}, {command}): {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # COMMISSIONING / REMOVAL
    # =========================================================================

    async def commission(self, code: str) -> dict:
        """Commission a Matter device using its setup code."""
        try:
            await self._send_command("commission_with_code", {"code": code})
            logger.info(f"Matter: commissioning started with code {code[:8]}...")
            return {"success": True, "protocol": "matter"}
        except Exception as e:
            logger.error(f"Matter commission failed: {e}")
            return {"success": False, "error": str(e)}

    async def remove_node(self, node_id: int) -> dict:
        """Remove a Matter node."""
        try:
            await self._send_command("remove_node", {"node_id": node_id})
            ieee = f"matter_{node_id}"
            self.devices.pop(ieee, None)
            await self._remove_discovery(ieee)
            logger.info(f"Matter: node {node_id} removed")
            return {"success": True}
        except Exception as e:
            logger.error(f"Matter remove failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # FRIENDLY NAMES
    # =========================================================================

    def rename_device(self, ieee: str, name: str):
        """Set a friendly name for a Matter device."""
        self._friendly_names[ieee] = name
        if ieee in self.devices:
            self.devices[ieee].friendly_name = name

    def get_friendly_name(self, ieee: str) -> str:
        """Get friendly name for a Matter device."""
        if ieee in self._friendly_names:
            return self._friendly_names[ieee]
        if ieee in self.devices:
            return self.devices[ieee].friendly_name
        return ieee

    # =========================================================================
    # DEVICE LIST (matches ZigbeeService.get_device_list format)
    # =========================================================================

    def get_device_list(self) -> list:
        """Return all Matter devices in the same format as ZigbeeService."""
        return [dev.to_device_list_entry() for dev in self.devices.values()]

    # =========================================================================
    # MQTT / HOME ASSISTANT DISCOVERY
    # =========================================================================

    async def _publish_device_state(self, dev: MatterDevice):
        """Publish device state to MQTT."""
        if not self.mqtt_service or not self.mqtt_service._connected:
            return

        safe_name = dev.friendly_name.replace(" ", "_").replace("/", "_")
        state_payload = dev.state.copy()
        state_payload["available"] = dev.is_available()
        state_payload["linkquality"] = 0  # Matter doesn't report LQI

        try:
            await self.mqtt_service.publish(
                safe_name,
                json.dumps(state_payload),
                retain=True,
                qos=1
            )
        except Exception as e:
            logger.debug(f"Failed to publish Matter state for {dev.ieee}: {e}")

    async def _publish_all_discovery(self):
        """Publish HA discovery for all Matter devices."""
        for dev in self.devices.values():
            await self._publish_discovery(dev)

    async def _publish_discovery(self, dev: MatterDevice):
        """Publish HA MQTT discovery for a single Matter device."""
        if not self.mqtt_service or not self.mqtt_service._connected:
            return

        node_id = dev.ieee.replace(":", "")
        safe_name = dev.friendly_name.replace(" ", "_").replace("/", "_")
        state_topic = f"{self.base_topic}/{safe_name}"
        command_topic = f"{self.base_topic}/{safe_name}/set"

        device_block = {
            "identifiers": [node_id],
            "name": dev.friendly_name,
            "model": dev.model or "Matter Device",
            "manufacturer": dev.manufacturer or "Unknown",
            "via_device": self.base_topic,
        }

        configs = []

        # Light
        if "state" in dev.state and ("brightness" in dev.state or "color_temp" in dev.state):
            config = {
                "name": dev.friendly_name,
                "unique_id": f"{node_id}_light",
                "state_topic": state_topic,
                "command_topic": command_topic,
                "schema": "json",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_value_template": "{{ value_json.state }}",
                "device": device_block,
                "availability": [
                    {"topic": f"{self.base_topic}/bridge/state"},
                    {"topic": state_topic, "value_template": "{{ 'online' if value_json.available else 'offline' }}"}
                ],
                "availability_mode": "all",
            }

            if "brightness" in dev.state:
                config["brightness"] = True
                config["brightness_scale"] = 254

            if "color_temp" in dev.state:
                config["color_temp"] = True

            configs.append(("light", "light", config))

        # Switch (on/off only, no brightness)
        elif "state" in dev.state:
            config = {
                "name": dev.friendly_name,
                "unique_id": f"{node_id}_switch",
                "state_topic": state_topic,
                "command_topic": command_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "value_template": "{{ value_json.state }}",
                "device": device_block,
                "availability": [
                    {"topic": f"{self.base_topic}/bridge/state"},
                    {"topic": state_topic, "value_template": "{{ 'online' if value_json.available else 'offline' }}"}
                ],
                "availability_mode": "all",
            }
            configs.append(("switch", "switch", config))

        # Temperature sensor
        if "temperature" in dev.state:
            config = {
                "name": f"{dev.friendly_name} Temperature",
                "unique_id": f"{node_id}_temperature",
                "state_topic": state_topic,
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "value_template": "{{ value_json.temperature }}",
                "device": device_block,
            }
            configs.append(("sensor", "temperature", config))

        # Humidity sensor
        if "humidity" in dev.state:
            config = {
                "name": f"{dev.friendly_name} Humidity",
                "unique_id": f"{node_id}_humidity",
                "state_topic": state_topic,
                "device_class": "humidity",
                "unit_of_measurement": "%",
                "value_template": "{{ value_json.humidity }}",
                "device": device_block,
            }
            configs.append(("sensor", "humidity", config))

        # Occupancy sensor
        if "occupancy" in dev.state:
            config = {
                "name": f"{dev.friendly_name} Occupancy",
                "unique_id": f"{node_id}_occupancy",
                "state_topic": state_topic,
                "device_class": "occupancy",
                "payload_on": "true",
                "payload_off": "false",
                "value_template": "{{ value_json.occupancy | lower }}",
                "device": device_block,
            }
            configs.append(("binary_sensor", "occupancy", config))

        # Contact sensor
        if "contact" in dev.state:
            config = {
                "name": f"{dev.friendly_name} Contact",
                "unique_id": f"{node_id}_contact",
                "state_topic": state_topic,
                "device_class": "door",
                "payload_on": "false",   # contact=false means open
                "payload_off": "true",
                "value_template": "{{ value_json.contact | lower }}",
                "device": device_block,
            }
            configs.append(("binary_sensor", "contact", config))

        # Illuminance sensor
        if "illuminance" in dev.state:
            config = {
                "name": f"{dev.friendly_name} Illuminance",
                "unique_id": f"{node_id}_illuminance",
                "state_topic": state_topic,
                "device_class": "illuminance",
                "unit_of_measurement": "lx",
                "value_template": "{{ value_json.illuminance }}",
                "device": device_block,
            }
            configs.append(("sensor", "illuminance", config))

        # Publish all configs
        for component, object_id, config in configs:
            topic = f"homeassistant/{component}/{node_id}/{object_id}/config"
            try:
                await self.mqtt_service.client.publish(
                    topic, json.dumps(config), retain=True, qos=1
                )
                logger.debug(f"Matter discovery: {topic}")
            except Exception as e:
                logger.error(f"Failed to publish Matter discovery: {e}")

        if configs:
            logger.info(f"[{dev.ieee}] Published Matter HA discovery ({len(configs)} entities)")

    async def _remove_discovery(self, ieee: str):
        """Remove HA discovery entries for a Matter device."""
        if not self.mqtt_service or not self.mqtt_service._connected:
            return

        node_id = ieee.replace(":", "")
        for component in ["light", "switch", "sensor", "binary_sensor"]:
            for object_id in ["light", "switch", "temperature", "humidity",
                              "occupancy", "contact", "illuminance"]:
                topic = f"homeassistant/{component}/{node_id}/{object_id}/config"
                try:
                    await self.mqtt_service.client.publish(topic, "", retain=True, qos=1)
                except Exception:
                    pass

    # =========================================================================
    # STATUS
    # =========================================================================

    def get_status(self) -> dict:
        """Return Matter bridge status for API/UI."""
        return {
            "connected": self._connected,
            "server_url": self.server_url,
            "device_count": len(self.devices),
            "devices": [
                {
                    "ieee": dev.ieee,
                    "node_id": dev.node_id,
                    "name": dev.friendly_name,
                    "model": dev.model,
                    "available": dev.is_available(),
                    "type": dev.get_type(),
                }
                for dev in self.devices.values()
            ]
        }