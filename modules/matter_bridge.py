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

from handlers.matter_parsers import get_parser_for_node, BaseMatterParser

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = logging.getLogger("matter_bridge")


# =============================================================================
# MATTER CAPABILITIES
# =============================================================================

class MatterCapabilities:
    """Thin wrapper matching DeviceCapabilities interface for automation engine."""
    def __init__(self, caps_list):
        self._capabilities = set(caps_list or [])

    def has_capability(self, capability: str) -> bool:
        return capability in self._capabilities

    def get_capabilities(self):
        return self._capabilities


# =============================================================================
# MATTER DEVICE WRAPPER
# =============================================================================

class MatterDevice:
    """
    Lightweight wrapper around a matter-server node.
    Delegates attribute parsing to the matter_parsers framework.
    """

    def __init__(self, node: dict, bridge: "MatterBridge" = None):
        self.node = node
        self.node_id = node.get("node_id", 0)
        self.ieee = f"matter_{self.node_id}"
        self.state: Dict[str, Any] = {}
        self.last_seen = time.time()
        self._available = node.get("available", False)
        self._bridge = bridge

        attributes = node.get("attributes", {})

        # Auto-detect parser based on device attributes
        self._parser = get_parser_for_node(attributes)

        # Extract identity using parser
        self.manufacturer = self._parser.get_manufacturer(attributes)
        self.model = self._parser.get_model(attributes)
        self.friendly_name = self._parser.get_friendly_name(attributes)

        # Build initial state
        self.state = self._parser.build_state(attributes, self.node_id, self._available)

    def update_from_node(self, node: dict):
        """Update device from a new node snapshot."""
        self.node = node
        self._available = node.get("available", self._available)
        self.last_seen = time.time()
        attributes = node.get("attributes", {})

        # Rebuild state using parser
        self.state = self._parser.build_state(attributes, self.node_id, self._available)

        # Re-read labels in case they changed
        new_name = self._parser.get_friendly_name(attributes)
        if new_name and new_name != "Matter Device":
            self.friendly_name = new_name

    def is_available(self) -> bool:
        return self._available

    def get_role(self) -> str:
        return "Matter"

    def get_type(self) -> str:
        return self._parser.get_device_type(self.node.get("attributes", {}))

    @property
    def capabilities(self):
        caps = self._parser.get_capabilities(self.node.get("attributes", {}))
        return MatterCapabilities(caps)

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return self._parser.get_commands(self.node.get("attributes", {}))

    def to_device_list_entry(self) -> dict:
        """Return dict matching ZigbeeService.get_device_list() format."""
        attributes = self.node.get("attributes", {})
        basic_info = self._parser.parse_basic_info(attributes)

        return {
            "ieee": self.ieee,
            "nwk": f"0x{self.node_id:04x}",
            "friendly_name": self.friendly_name,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "lqi": 255 if self._available else 0,
            "last_seen_ts": self.last_seen,
            "state": self.state.copy(),
            "type": self.get_type(),
            "protocol": "matter",
            "quirk": self._parser.__class__.__name__,
            "capabilities": self._parser.get_capabilities(attributes),
            "settings": {},
            "available": self._available,
            "config_schema": [],
            "polling_interval": 0,
            "basic_info": basic_info,
        }

    def _get_capabilities(self) -> list:
        return self._parser.get_capabilities(self.node.get("attributes", {}))

    async def send_command(self, command, value=None, endpoint_id=None):
        """Proxy command to MatterBridge.send_command for automation engine compatibility."""
        if not self._bridge:
            return {"success": False, "error": "No bridge reference"}
        return await self._bridge.send_command(self.node_id, command, value)


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
        self._automation_evaluator: Optional[Callable] = None

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

            # Subscribe to events AND get initial node dump
            # start_listening returns server info, then sends node data as events.
            # Unlike get_nodes, it also subscribes to future node_added/node_updated/
            # attribute_updated events — essential for mid-session commissioning.
            await self._send_command("start_listening")

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

                await self._send_command("start_listening")
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
        # Response to a command (e.g. start_listening, get_nodes)
        if "result" in data and "message_id" in data:
            result = data["result"]

            if isinstance(result, list):
                # Node list response (from get_nodes or start_listening node dump)
                node_count = 0
                for node in result:
                    if isinstance(node, dict) and "node_id" in node:
                        await self._upsert_node(node)
                        node_count += 1
                logger.info(f"Matter: loaded {node_count} nodes")

                # Publish discovery for all devices after initial load
                if node_count > 0:
                    await self._publish_all_discovery()

            elif isinstance(result, dict):
                # Server info response from start_listening — log and continue.
                # Node data follows as separate event messages.
                sdk_ver = result.get("sdk_version", "?")
                thread_set = result.get("thread_credentials_set", False)
                bt = result.get("bluetooth_enabled", False)
                logger.info(
                    f"Matter server info: SDK {sdk_ver}, "
                    f"thread_credentials={thread_set}, bluetooth={bt}"
                )
            return

        # Event-based messages
        event = data.get("event")

        # start_listening sends node dump as event with empty event name
        # and result containing the node list
        if "result" in data and "event" in data:
            result = data["result"]
            if isinstance(result, list):
                node_count = 0
                for node in result:
                    if isinstance(node, dict) and "node_id" in node:
                        await self._upsert_node(node)
                        node_count += 1
                if node_count > 0:
                    logger.info(f"Matter: loaded {node_count} nodes from event stream")
                    await self._publish_all_discovery()
                return

        if event == "node_added":
            node = data.get("data", {})
            if "node_id" in node:
                await self._upsert_node(node)
                node_id = node["node_id"]
                ieee = f"matter_{node_id}"
                dev = self.devices.get(ieee)
                logger.info(f"Matter: node {node_id} added")

                await self._emit_debug_packet("node_added", node_id, {
                    "manufacturer": dev.manufacturer if dev else "Unknown",
                    "model": dev.model if dev else "Unknown",
                })

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

                await self._emit_debug_packet("node_removed", node_id, {})

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
                        # Use parser for state building — handles rotary detection etc.
                        dev.state = dev._parser.build_state(
                            attributes, dev.node_id, dev._available
                        )
                        dev.last_seen = time.time()

                        if self.event_callback:
                            await self.event_callback("device_updated", {
                                "ieee": ieee,
                                "data": dev.state.copy()
                            })

                        # Publish state to MQTT
                        await self._publish_device_state(dev)

                        # Trigger automation evaluation for Matter devices
                        if self._automation_evaluator:
                            try:
                                await self._automation_evaluator(ieee, dev.state.copy())
                            except Exception as ae:
                                logger.debug(f"Automation eval error for {ieee}: {ae}")

                        await self._emit_debug_packet("attribute_updated", node_id, {
                            "attribute_path": attr_path,
                            "new_value": attr_value,
                            "endpoint_id": int(attr_path.split("/")[0]) if "/" in attr_path else 0,
                            "cluster_id": int(attr_path.split("/")[1]) if "/" in attr_path else 0,
                        })

        elif event == "node_event":
            # Matter cluster events (button presses, rotary turns, etc.)
            event_data = data.get("data", {})
            node_id = event_data.get("node_id")
            endpoint_id = event_data.get("endpoint_id", 0)
            cluster_id = event_data.get("cluster_id", 0)
            event_name = event_data.get("event_name", "")
            event_data_inner = event_data.get("event_data", {})
            logger.info(f"[DEBUG] Raw node_event data keys: {list(event_data.keys())} full: {event_data}")


            if node_id is not None:
                ieee = f"matter_{node_id}"
                if ieee in self.devices:
                    dev = self.devices[ieee]
                    dev.last_seen = time.time()

                    # Build a human-readable action string
                    action = dev._parser.parse_event(
                        event_name, endpoint_id, cluster_id, event_data_inner
                    )

                    # Update device state with the latest action
                    dev.state["last_action"] = action
                    dev.state["last_action_endpoint"] = endpoint_id
                    dev.state["last_action_time"] = dev.last_seen

                    # Definition-based event → update per-endpoint action state keys
                    if hasattr(dev._parser, 'handle_event'):
                        result = dev._parser.handle_event(endpoint_id, event_name, event_data_inner)
                        if result:
                            state_key, action_value = result
                            dev.state[state_key] = action_value

                    logger.info(
                        f"[{ieee}] Matter event: {action} "
                        f"(EP{endpoint_id}, cluster {cluster_id})"
                    )

                    # Emit to frontend via WebSocket
                    if self.event_callback:
                        await self.event_callback("device_updated", {
                            "ieee": ieee,
                            "data": dev.state.copy(),
                        })
                        # Also emit as a specific button event for automations
                        await self.event_callback("matter_button_event", {
                            "ieee": ieee,
                            "node_id": node_id,
                            "endpoint_id": endpoint_id,
                            "action": action,
                            "event_name": event_name,
                            "event_data": event_data_inner,
                        })

                    # Publish to MQTT
                    await self._publish_device_state(dev)

                    # Trigger automation evaluation for Matter events
                    if self._automation_evaluator:
                        try:
                            await self._automation_evaluator(ieee, dev.state.copy())
                        except Exception as ae:
                            logger.debug(f"Automation eval error for {ieee}: {ae}")

                    # Emit debug packet
                    if hasattr(self, '_emit_debug_packet'):
                        await self._emit_debug_packet("button_event", node_id, {
                            "event_name": event_name,
                            "action": action,
                            "endpoint_id": endpoint_id,
                            "cluster_id": cluster_id,
                            "event_data": event_data_inner,
                        })

    async def _upsert_node(self, node: dict):
        """Insert or update a Matter device."""
        node_id = node["node_id"]
        ieee = f"matter_{node_id}"

        if ieee in self.devices:
            dev = self.devices[ieee]
            old_state = dev.state.copy()
            dev.update_from_node(node)
        else:
            dev = MatterDevice(node, bridge=self)
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

        # Trigger automation evaluation on state change
        if dev.state != old_state and self._automation_evaluator:
            try:
                await self._automation_evaluator(ieee, dev.state.copy())
            except Exception as ae:
                logger.debug(f"Automation eval error for {ieee}: {ae}")

    # =========================================================================
    # DEVICE COMMANDS
    # =========================================================================

    async def send_command(self, node_id: int, command: str, value=None) -> dict:
        """Send a command to a Matter device via matter-server."""
        try:
            cluster_id = 0
            endpoint_id = 1

            if command in ("on", "off", "toggle"):
                cluster_id = 6
                command_name = {"on": "On", "off": "Off", "toggle": "Toggle"}[command]
                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                    "command_name": command_name,
                })

            elif command == "brightness":
                cluster_id = 8
                level = int((value or 0) * 2.54)
                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                    "command_name": "MoveToLevelWithOnOff",
                    "args": {"level": level, "transition_time": 5},
                })

            elif command == "color_temp":
                cluster_id = 768
                mireds = int(1000000 / max(value or 4000, 1))
                await self._send_command("device_command", {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                    "command_name": "MoveToColorTemperature",
                    "args": {"color_temperature_mireds": mireds, "transition_time": 5},
                })

            else:
                return {"success": False, "error": f"Unknown command: {command}"}

            # Emit debug packet for TX direction
            await self._emit_debug_packet("command", node_id, {
                "command_name": command,
                "value": value,
                "endpoint_id": endpoint_id,
                "cluster_id": cluster_id,
                "direction": "TX",
            })

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
            # If OTBR is running, provide Thread credentials before commissioning
            try:
                import subprocess
                result = subprocess.run(
                    ["ot-ctl", "dataset", "active", "-x"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    dataset_hex = result.stdout.strip().split("\n")[0].strip()
                    if dataset_hex and dataset_hex != "Done":
                        await self._send_command("set_thread_dataset", {
                            "dataset": dataset_hex
                        })
                        logger.info(f"Matter: Thread dataset provided for commissioning")
            except Exception as e:
                logger.debug(f"Thread dataset not available (non-fatal): {e}")

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

    # =========================================================================
    # DEBUGGING
    # =========================================================================
    async def _emit_debug_packet(self, event_type: str, node_id: int, data: dict):
        """Emit a Matter event as a debug packet for the live debug stream."""
        if not self.event_callback:
            return

        ieee = f"matter_{node_id}"
        dev = self.devices.get(ieee)
        friendly_name = dev.friendly_name if dev else f"Node {node_id}"

        now = time.time()
        packet = {
            "protocol": "matter",
            "timestamp": now,
            "timestamp_str": time.strftime("%H:%M:%S", time.localtime(now)),
            "ieee": ieee,
            "friendly_name": friendly_name,
            "direction": data.get("direction", "RX"),
            "event": event_type,
            "node_id": node_id,
            "data": data,
            # Fields for unified display with Zigbee packets
            "cluster": data.get("cluster_id", 0),
            "cluster_name": data.get("cluster_name", event_type),
            "endpoint": data.get("endpoint_id", 0),
            "importance": "high" if event_type in (
                "node_added", "node_removed", "command", "button_event"
            ) else "normal",
            "summary": self._build_matter_summary(event_type, data, friendly_name),
            # Fields the Zigbee packet analyser/renderer expects — prevent undefined errors
            "decoded": data,
            "message": None,
            "profile": 0,
            "src_ep": data.get("endpoint_id", 0),
            "dst_ep": 0,
        }

        try:
            await self.event_callback("debug_packet", packet)
        except Exception:
            pass

    @staticmethod
    def _build_matter_summary(event_type: str, data: dict, name: str) -> str:
        """Build a human-readable summary for a Matter debug packet."""
        if event_type == "attribute_updated":
            path = data.get("attribute_path", "?")
            new_val = data.get("new_value", "?")
            return f"{name}: {path} → {new_val}"
        elif event_type == "button_event":
            return f"{name}: {data.get('action', '?')}"
        elif event_type == "node_added":
            return f"Commissioned: {name}"
        elif event_type == "node_removed":
            return f"Removed: {name}"
        elif event_type == "node_updated":
            return f"Updated: {name}"
        elif event_type == "command":
            return f"{name}: {data.get('command_name', '?')}"
        return f"{name}: {event_type}"