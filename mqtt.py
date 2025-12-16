"""
MQTT Client Wrapper for Zigbee Service
Handles connection, publishing, and Home Assistant integration (Birth Messages).
"""
import json
import asyncio
import logging
from typing import Optional, Callable, Dict, Any
from contextlib import suppress

from mqtt_queue import MQTTPublishQueue

logger = logging.getLogger("mqtt")


class MQTTService:
    """
    MQTT Service with robust reconnection and HA command handling.
    Based on ZHA MQTT patterns for reliable Home Assistant integration.
    """

    def __init__(
        self,
        broker_host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_topic: str = "zigbee",
        qos: int = 0,
        log_callback: Optional[Callable] = None,
        command_callback: Optional[Callable] = None,
        group_command_callback: Optional[Callable] = None,
    ):
        self.broker = broker_host
        self.port = port
        self.username = username
        self.password = password
        self.base_topic = base_topic
        self.default_qos = qos
        self.log = log_callback
        self.command_callback = command_callback
        self.group_command_callback: Optional[Callable] = None

        # Callback for when HA comes online (birth message)
        self.ha_status_callback: Optional[Callable] = None

        # Callback for bridge status changes (for frontend notification)
        self.status_change_callback: Optional[Callable] = None

        # Client management
        self.client = None
        self._connected = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._message_handler_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # Reconnection settings
        self._reconnect_interval = 5  # Start with 5 seconds
        self._max_reconnect_interval = 300  # Max 5 minutes
        self._reconnect_attempts = 0

        # Topic subscriptions
        self._subscribed_topics: set = set()

        # Bridge/Gateway LWT (Last Will and Testament) topic
        self.bridge_status_topic = f"{self.base_topic}/bridge/state"

        # Bridge/Gateway LWT (Last Will and Testament) topic
        self.bridge_status_topic = f"{self.base_topic}/bridge/state"

        # Fast-path publish queue
        self._publish_queue: Optional[MQTTPublishQueue] = None

    @property
    def connected(self) -> bool:
        return self._connected and self.client is not None

    async def _log(self, level: str, msg: str, ieee: Optional[str] = None):
        if self.log:
            await self.log(level, f"[MQTT] {msg}", ieee=ieee)
        logger.log(getattr(logging, level, logging.INFO), msg)

    async def start(self):
        """Start MQTT service with automatic reconnection."""
        self._shutdown = False
        await self._connect()

        if self._connected:
            self._publish_queue = MQTTPublishQueue(
                self,
                max_queue_size=1000,
                batch_window_ms=10
            )
            await self._publish_queue.start()
            logger.info("MQTT fast-path queue started")



    async def _connect(self):
        """Establish connection to MQTT broker."""
        try:
            from aiomqtt import Client, MqttError

            logger.info(f"Connecting to MQTT broker at {self.broker}:{self.port}...")

            self.client = Client(
                hostname=self.broker,
                port=self.port,
                username=self.username,
                password=self.password,
                clean_session=True,
                keepalive=60
            )

            await self.client.__aenter__()
            self._connected = True
            self._reconnect_attempts = 0
            self._reconnect_interval = 5  # Reset backoff

            logger.info(f"✓ Connected to MQTT Broker at {self.broker}:{self.port}")
            await self._log("INFO", f"Connected to MQTT Broker at {self.broker}:{self.port}")

            # Publish bridge online status (retained, so HA always knows bridge state)
            await self.client.publish(self.bridge_status_topic, "online", qos=1, retain=True)
            logger.info(f"📡 Published bridge status: {self.bridge_status_topic} = online")

            # Notify about status change (for frontend)
            if self.status_change_callback:
                try:
                    await self.status_change_callback("online")
                except Exception as e:
                    logger.debug(f"Status callback error: {e}")


            # ---------------------------------------------------------
            # Call the method to subscribe to command topics!
            # ---------------------------------------------------------
            await self._subscribe_to_topics()
            # ---------------------------------------------------------

            # Start message handler
            self._message_handler_task = asyncio.create_task(self._handle_messages())

        except Exception as e:
            self._connected = False
            logger.error(f"MQTT connection failed: {e}")
            await self._log("ERROR", f"Connection failed: {e}")

            # Schedule reconnection
            if not self._shutdown:
                self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt with exponential backoff."""
        if self._shutdown:
            return

        self._reconnect_attempts += 1
        # Exponential backoff: 5, 10, 20, 40, 80, 160, 300 (max)
        self._reconnect_interval = min(
            self._reconnect_interval * 2,
            self._max_reconnect_interval
        )

        logger.warning(
            f"Scheduling MQTT reconnection attempt {self._reconnect_attempts} "
            f"in {self._reconnect_interval}s"
        )

        if self._reconnect_task:
            self._reconnect_task.cancel()

        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Reconnection loop with backoff."""
        while not self._shutdown and not self._connected:
            await asyncio.sleep(self._reconnect_interval)

            if self._shutdown:
                break

            logger.info(f"Attempting MQTT reconnection (attempt {self._reconnect_attempts})...")
            await self._log("INFO", f"Reconnecting to MQTT (attempt {self._reconnect_attempts})...")

            await self._connect()

            if self._connected:
                logger.info("✓ MQTT reconnection successful")
                await self._log("INFO", "MQTT reconnection successful")
                break

    async def _subscribe_to_topics(self):
        """Subscribe to all necessary topics."""
        if not self.client or not self._connected:
            return

        try:
            # 1. Subscribe to Zigbee Device Commands
            command_pattern = f"{self.base_topic}/+/set"
            await self.client.subscribe(command_pattern, qos=1)
            self._subscribed_topics.add(command_pattern)

            # 2. Subscribe to Group Commands ---
            group_pattern = f"{self.base_topic}/group/+/set"
            await self.client.subscribe(group_pattern, qos=1)
            self._subscribed_topics.add(group_pattern)

            # 3. Subscribe to HA Commands (Component based)
            ha_command_pattern = "homeassistant/+/+/+/set"
            await self.client.subscribe(ha_command_pattern, qos=1)
            self._subscribed_topics.add(ha_command_pattern)

            # 4. Subscribe to Home Assistant Status (Birth Message)
            await self.client.subscribe("homeassistant/status", qos=1)
            self._subscribed_topics.add("homeassistant/status")
            logger.info("✓ Subscribed to HA Status (Birth Message)")

        except Exception as e:
            logger.error(f"Failed to subscribe to topics: {e}")

    async def _handle_messages(self):
        """Handle incoming MQTT messages."""
        if not self.client:
            return

        try:
            async for message in self.client.messages:
                try:
                    topic = str(message.topic)
                    payload = message.payload.decode('utf-8') if message.payload else ""

                    logger.debug(f"MQTT RX: {topic} = {payload}")

                    # --- CASE 1: Home Assistant Birth Message ---
                    if topic == "homeassistant/status":
                        logger.info(f"Home Assistant Status Change: {payload}")
                        if payload.lower() == "online" and self.ha_status_callback:
                            logger.info("📢 HA Online detected - Triggering device republish...")
                            # Run the callback (core.republish_all_devices)
                            asyncio.create_task(self.ha_status_callback())
                        continue

                    # --- CASE 2: Device Command ---
                    await self._route_command(topic, payload)

                except Exception as e:
                    logger.error(f"Error processing MQTT message: {e}")

        except asyncio.CancelledError:
            logger.debug("Message handler cancelled")
        except Exception as e:
            logger.error(f"MQTT message handler error: {e}")
            self._connected = False

            # Trigger reconnection
            if not self._shutdown:
                self._schedule_reconnect()

    async def _route_command(self, topic: str, payload: str):
        """Route incoming command to the appropriate device handler."""
        parts = topic.split('/')

        try:
            # Parse payload as JSON
            try:
                data = json.loads(payload) if payload.startswith('{') else {"state": payload}
            except json.JSONDecodeError:
                data = {"state": payload}

            # --- Check for Group Command (zigbee/group/name/set) ---
            if parts[0] == self.base_topic and len(parts) == 4 and parts[1] == 'group' and parts[-1] == "set":
                group_name = parts[2]
                logger.info(f"📥 Group Command for '{group_name}': {data}")

                # handle group commands.
                if self.group_command_callback:
                    await self.group_command_callback(group_name, data)
                return

            # Format 1: {base_topic}/{device_name}/set
            if parts[0] == self.base_topic and parts[-1] == "set":
                device_name = parts[1]
                logger.info(f"📥 Command for device '{device_name}': {data}")

                if self.command_callback:
                    await self.command_callback(device_name, data)

            # Format 2: homeassistant/{component}/{node_id}/{object_id}/set
            elif parts[0] == "homeassistant" and parts[-1] == "set":
                component = parts[1]
                node_id = parts[2]
                object_id = parts[3] if len(parts) > 3 else ""

                logger.info(f"📥 HA Command: {component}/{node_id}/{object_id} = {data}")

                if self.command_callback:
                    # Convert node_id (without colons) back to IEEE format
                    # node_id is IEEE without colons, need to look it up
                    await self.command_callback(node_id, data, component=component, object_id=object_id)

        except Exception as e:
            logger.error(f"Failed to route command: {e}")

    async def stop(self):
        """Stop MQTT service gracefully."""
        logger.info("Stopping MQTT service...")
        self._shutdown = True

        if self._publish_queue:
            await self._publish_queue.stop()
            self._publish_queue = None

        # Publish bridge offline status before disconnecting
        if self.client and self._connected:
            try:
                await self.client.publish(self.bridge_status_topic, "offline", qos=1, retain=True)
                logger.info(f"📴 Published bridge status: offline")

                # Notify about status change (for frontend)
                if self.status_change_callback:
                    try:
                        await self.status_change_callback("offline")
                    except Exception as e:
                        logger.debug(f"Status callback error: {e}")
            except Exception as e:
                logger.debug(f"Could not publish offline status: {e}")

        self._connected = False

        # Cancel reconnection task
        if self._reconnect_task:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task

        # Cancel message handler
        if self._message_handler_task:
            self._message_handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._message_handler_task

        # Disconnect client
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error during MQTT disconnect: {e}")

        self.client = None
        logger.info("MQTT service stopped")
        await self._log("INFO", "MQTT Disconnected")

    async def publish(self, subtopic: str, payload: str, ieee: Optional[str] = None, qos: Optional[int] = None, retain: bool = True):
        """
        Publish message to MQTT topic.
        Topic format: {base_topic}/{subtopic}
        """
        if not self._connected or not self.client:
            logger.debug(f"Cannot publish, not connected. Topic: {subtopic}")
            return False

        if qos is None:
            qos = self.default_qos

        # Construct full topic
        if subtopic.startswith(self.base_topic):
            full_topic = subtopic
        else:
            full_topic = f"{self.base_topic}/{subtopic}"

        try:
            await self.client.publish(full_topic, payload, retain=retain, qos=qos)
            logger.debug(f"PUB [{full_topic}] (QoS {qos}): {payload[:100]}...")
            return True

        except Exception as e:
            logger.error(f"Publish failed: {e}")
            await self._log("ERROR", f"Publish failed: {e}", ieee=ieee)

            # Check if we need to reconnect
            if not self._connected:
                self._schedule_reconnect()

            return False

    async def publish_discovery(self, device_info: Dict[str, Any], configs: list):
        """
        Publish Home Assistant discovery configurations.
        Uses safe templates and ensures attribute presence.
        """
        if not self._connected or not self.client:
            logger.warning("Cannot publish discovery, not connected")
            return

        ieee = str(device_info['ieee'])
        node_id = ieee.replace(":", "")
        safe_name = device_info.get('safe_name', ieee)

        for entity in configs:
            component = entity['component']
            object_id = entity['object_id']

            # Discovery topic
            topic = f"homeassistant/{component}/{node_id}/{object_id}/config"

            # Build payload
            payload = entity['config'].copy()

            # Device info block (shared across all entities)
            payload['device'] = {
                "identifiers": [node_id],
                "name": device_info.get('friendly_name', ieee),
                "model": device_info.get('model', 'Unknown'),
                "manufacturer": device_info.get('manufacturer', 'Unknown'),
                "via_device": self.base_topic
            }

            # Unique ID
            payload['unique_id'] = f"{node_id}_{object_id}"

            # State topic - where device publishes its state
            state_topic = f"{self.base_topic}/{safe_name}"
            payload['state_topic'] = state_topic

            # Replace placeholder for brightness state topic
            if payload.get('brightness_state_topic') == "STATE_TOPIC_PLACEHOLDER":
                payload['brightness_state_topic'] = state_topic

            # Replace placeholder for color temp state topic
            if payload.get('color_temp_state_topic') == "STATE_TOPIC_PLACEHOLDER":
                payload['color_temp_state_topic'] = state_topic

            # ===================================================================
            # 1. SAFE AVAILABILITY CONFIGURATION
            # ===================================================================
            # Don't crash if 'available' key is missing from value_json
            payload['availability'] = [
                {
                    # Bridge availability (gateway LWT)
                    "topic": self.bridge_status_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline"
                },
                {
                    # Device availability - USE .get() WITH DEFAULT
                    "topic": state_topic,
                    "value_template": "{{ 'online' if value_json.get('available', false) else 'offline' }}",
                    "payload_available": "online",
                    "payload_not_available": "offline"
                }
            ]
            payload['availability_mode'] = 'all'

            # ===================================================================
            # 2: SAFE VALUE TEMPLATES
            # ===================================================================
            # Rewrite any value_template that uses direct attribute access to use .get()
            if 'value_template' in payload:
                template = payload['value_template']

                # Replace patterns like {{ value_json.brightness_1 }} with safe versions
                # This regex finds attribute access and wraps it in .get() with defaults
                import re

                # Pattern: value_json.attribute_name
                # Replace with: value_json.get('attribute_name', DEFAULT)
                def make_safe_template(match):
                    attr = match.group(1)

                    # Determine appropriate default based on attribute name
                    if any(x in attr for x in ['brightness', 'level']):
                        default = "0"
                    elif any(x in attr for x in ['voltage', 'power', 'current', 'energy']):
                        default = "0"
                    elif any(x in attr for x in ['temperature', 'humidity', 'pressure']):
                        default = "0"
                    elif any(x in attr for x in ['state', 'system_mode']):
                        default = "'OFF'"
                    elif any(x in attr for x in ['is_open', 'is_closed', 'contact', 'motion', 'occupancy', 'presence']):
                        default = "false"
                    elif 'color_temp' in attr:
                        default = "250"
                    else:
                        default = "none"

                    return f"value_json.get('{attr}', {default})"

                # Apply the transformation
                safe_template = re.sub(r'value_json\.([a-z_0-9]+)', make_safe_template, template)
                payload['value_template'] = safe_template
                logger.debug(f"Converted template: {template} -> {safe_template}")



            # templates to use .get() for safety
            if 'state_value_template' in payload:
                template = payload['state_value_template']
                # Convert simple attribute access to .get()
                if 'value_json.' in template and '.get' not in template:
                    try:
                        attr = template.split('value_json.')[1].split(' ')[0].split('}')[0]
                        safe_template = f"{{{{ value_json.get('{attr}', 'OFF') }}}}"
                        payload['state_value_template'] = safe_template
                    except IndexError:
                        pass

            # brightness_value_template
            if 'brightness_value_template' in payload:
                template = payload['brightness_value_template']
                # Only apply fix if .get is NOT already in the template
                if 'value_json.' in template and '.get' not in template:
                    try:
                        attr = template.split('value_json.')[1].split(' ')[0].split('}')[0]
                        suffix = ""
                        if "|" in template:
                            suffix = " | " + template.split("|")[1].split("}")[0].strip()

                        safe_template = f"{{{{ value_json.get('{attr}', 0){suffix} }}}}"
                        payload['brightness_value_template'] = safe_template
                    except IndexError:
                        pass

            # color_temp_value_template
            if 'color_temp_value_template' in payload:
                template = payload['color_temp_value_template']
                # Only apply fix if .get is NOT already in the template
                if 'value_json.' in template and '.get' not in template:
                    import re
                    # This regex would match "value_json.get" if we didn't have the check above
                    safe_template = re.sub(r'value_json\.([a-z_0-9]+)', lambda m: f"value_json.get('{m.group(1)}', 250)", template)
                    payload['color_temp_value_template'] = safe_template

            # ===================================================================
            # 3: SET UP COMMAND TOPICS
            # ===================================================================
            command_topic = f"{self.base_topic}/{safe_name}/set"

            if component == "switch" or component == "light":
                payload['command_topic'] = command_topic

                if 'payload_on' not in payload:
                    payload['payload_on'] = json.dumps({"command": "on", "state": "ON"})
                if 'payload_off' not in payload:
                    payload['payload_off'] = json.dumps({"command": "off", "state": "OFF"})

                if component == "light":
                    # Brightness command template if supported
                    if 'brightness' in payload:
                        payload['brightness_command_topic'] = command_topic
                        payload['brightness_command_template'] = '{"command": "brightness", "value": {{ value }}}'
                    # Color temp if supported
                    if 'color_mode' in payload:
                        payload['color_temp_command_topic'] = command_topic
                        payload['color_temp_command_template'] = '{"command": "color_temp", "value": {{ value }}}'

            elif component == "cover":
                payload['command_topic'] = command_topic
                payload['payload_open'] = json.dumps({"command": "open"})
                payload['payload_close'] = json.dumps({"command": "close"})
                payload['payload_stop'] = json.dumps({"command": "stop"})

            elif component == "climate":
                payload['temperature_command_topic'] = command_topic
                payload['mode_command_topic'] = command_topic

            elif component == "number":
                payload['command_topic'] = command_topic

            # Clean placeholder
            if payload.get('command_topic') == "CMD_TOPIC_PLACEHOLDER":
                payload['command_topic'] = command_topic

            # Publish discovery config
            try:
                await self.client.publish(topic, json.dumps(payload), retain=True, qos=1)
                logger.debug(f"Discovery: {topic}")
            except Exception as e:
                logger.error(f"Failed to publish discovery for {object_id}: {e}")

        logger.info(f"[{ieee}] Published HA discovery for {len(configs)} entities")



    def publish_state_update_with_required_attributes(device, mqtt_service):
        """
        When publishing state updates, ensure ALL attributes referenced in
        discovery configs are present, even if null/default.

        This is CRITICAL - it's not enough to just fix templates, you must
        also ensure the state payload always contains the expected keys.
        """
        state_payload = device.state.copy()

        # ALWAYS include 'available' key (used in availability template)
        if 'available' not in state_payload:
            state_payload['available'] = device.is_available()

        # Get device capabilities to know what attributes should exist
        caps = device.capabilities

        # For lights, always include state
        if caps.is_light or caps.is_switch:
            for ep_id in device.endpoints.keys():
                if ep_id == 0:
                    continue
                state_key = f"state_{ep_id}"
                if state_key not in state_payload:
                    state_payload[state_key] = "OFF"

                # If light supports brightness, always include it
                if caps.supports_brightness:
                    bright_key = f"brightness_{ep_id}"
                    if bright_key not in state_payload:
                        state_payload[bright_key] = 0

                # If light supports color temp, always include it
                if caps.supports_color_temp:
                    temp_key = f"color_temp_mireds"
                    if temp_key not in state_payload:
                        state_payload[temp_key] = 250

        # For sensors, always include sensor values
        if caps.is_temperature_sensor and 'temperature' not in state_payload:
            state_payload['temperature'] = None

        if caps.is_humidity_sensor and 'humidity' not in state_payload:
            state_payload['humidity'] = None

        if caps.is_contact_sensor:
            for ep_id in device.endpoints.keys():
                if ep_id == 0:
                    continue
                contact_key = f"is_open_{ep_id}"
                if contact_key not in state_payload:
                    state_payload[contact_key] = False

        # For power monitoring devices
        if caps.supports_power_monitoring:
            for ep_id in device.endpoints.keys():
                if ep_id == 0:
                    continue
                for attr in ['power', 'voltage', 'current']:
                    key = f"{attr}_{ep_id}"
                    if key not in state_payload:
                        state_payload[key] = 0

        # Now publish with all required attributes
        mqtt_service.publish(
            device.safe_name,
            json.dumps(state_payload),
            qos=0
        )

    async def remove_discovery(self, ieee: str, configs: list):
        """Remove HA discovery configs when device is removed."""
        if not self._connected or not self.client:
            return

        node_id = ieee.replace(":", "")

        for entity in configs:
            component = entity['component']
            object_id = entity['object_id']
            topic = f"homeassistant/{component}/{node_id}/{object_id}/config"

            try:
                # Publish empty payload to remove
                await self.client.publish(topic, "", retain=True, qos=1)
            except Exception as e:
                logger.error(f"Failed to remove discovery: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get MQTT connection status."""
        return {
            "connected": self._connected,
            "broker": f"{self.broker}:{self.port}",
            "base_topic": self.base_topic,
            "reconnect_attempts": self._reconnect_attempts,
            "subscribed_topics": list(self._subscribed_topics)
        }

    def publish_fast(self, subtopic: str, payload: str, qos: int = 0, retain: bool = True) -> bool:
        """
        Fast non-blocking publish for time-sensitive data.

        Uses the publish queue for immediate return (< 1ms).
        Suitable for motion sensors, radar, and other real-time events.

        Args:
            subtopic: Topic relative to base_topic
            payload: Message payload (usually JSON)
            qos: Quality of Service (default 0 for sensors)
            retain: Whether to retain message

        Returns:
            True if queued, False if queue unavailable
        """
        if not self._publish_queue:
            # Fallback to synchronous publish
            import asyncio
            try:
                asyncio.create_task(self.publish(subtopic, payload, ieee=None, qos=qos, retain=retain))
                return True
            except Exception:
                return False

        # Construct full topic
        if subtopic.startswith(self.base_topic):
            full_topic = subtopic
        else:
            full_topic = f"{self.base_topic}/{subtopic}"

        return self._publish_queue.publish_nowait(full_topic, payload, qos, retain)

    def get_queue_stats(self) -> dict:
        """Get MQTT publish queue statistics."""
        if self._publish_queue:
            return self._publish_queue.get_stats()
        return {'error': 'Queue not initialized'}