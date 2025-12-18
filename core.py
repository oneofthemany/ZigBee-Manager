"""
Zigbee Service Core - ZHA-inspired architecture
Properly handles device events using zigpy's listener system.
"""
import asyncio
import logging
import json
import time
import os
import re
import traceback
from typing import Dict, Any, Optional
from bellows.zigbee.application import ControllerApplication
from bellows.ash import NcpFailure
import zigpy.types
import zigpy.config
import zigpy.device

# Import ZDO types for binding
import zigpy.zdo.types as zdo_types
from zigpy.zcl.clusters.security import IasZone

# Import Device Wrapper
from device import ZHADevice
from handlers.zigbee_debug import get_debugger
from handlers.fast_path import FastPathProcessor
from handlers.sensors import configure_illuminance_reporting, configure_temperature_reporting


# import services
from json_helpers import prepare_for_json, sanitise_device_state

# Try Loading Quirks
logger = logging.getLogger("core")

# Try Loading Quirks
try:
    import zhaquirks
    # Explicitly import Hive quirks if available
    try:
        import zhaquirks.centralite
        logger.info("Loaded Centralite/Hive quirks")
    except ImportError:
        import zhaquirks.centralite
        pass

    zhaquirks.setup()
    logger.info("ZHA Quirks loaded successfully")
except Exception as e:
    logging.warning(f"Failed to load ZHA Quirks: {e}")


class PollingScheduler:
    """
    Per-device polling scheduler.
    Manages automatic polling of devices at configurable intervals.
    """

    def __init__(self, zigbee_service):
        self.service = zigbee_service
        self._tasks: Dict[str, asyncio.Task] = {}
        self._intervals: Dict[str, int] = {}  # ieee -> seconds
        self._running = False
        self._default_interval = 0  # 0 disables active polling by default

    def start(self):
        """Start the polling scheduler."""
        self._running = True
        logger.info("Polling scheduler started (Active polling disabled by default)")

    def stop(self):
        """Stop all polling tasks."""
        self._running = False
        for ieee, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
        logger.info("Polling scheduler stopped")

    def set_interval(self, ieee: str, interval: int):
        """
        Set polling interval for a device.
        interval=0 disables polling for the device.
        """
        self._intervals[ieee] = interval

        # Cancel existing task if any
        if ieee in self._tasks:
            self._tasks[ieee].cancel()
            del self._tasks[ieee]

        # Start new polling task if interval > 0
        if interval > 0 and self._running:
            self._tasks[ieee] = asyncio.create_task(self._poll_device_loop(ieee, interval))
            logger.info(f"[{ieee}] Polling set to {interval}s")
        elif interval == 0:
            logger.info(f"[{ieee}] Polling disabled")

    def get_interval(self, ieee: str) -> int:
        """Get polling interval for a device."""
        return self._intervals.get(ieee, 0)

    def get_all_intervals(self) -> Dict[str, int]:
        """Get all polling intervals."""
        return self._intervals.copy()

    async def _poll_device_loop(self, ieee: str, interval: int):
        """Polling loop for a single device."""
        while self._running and ieee in self._intervals:
            try:
                await asyncio.sleep(interval)

                if not self._running or ieee not in self._intervals:
                    break

                if ieee in self.service.devices:
                    device = self.service.devices[ieee]

                    # Only poll if device is available
                    if device.is_available():
                        logger.debug(f"[{ieee}] Auto-polling device")
                        try:
                            await device.poll()
                        except Exception as e:
                            logger.warning(f"[{ieee}] Poll failed: {e}")
                    else:
                        logger.debug(f"[{ieee}] Skipping poll - device unavailable")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{ieee}] Polling error: {e}")
                await asyncio.sleep(30)


    async def _availability_watchdog_loop(self):
        """Periodically check for expired devices."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            for ieee, device in self.service.devices.items():
                device.check_availability_change()


    def enable_for_device(self, ieee: str, interval: Optional[int] = None):
        """Enable polling for a device with optional custom interval."""
        if interval is None:
            interval = self._default_interval
        self.set_interval(ieee, interval)

    def disable_for_device(self, ieee: str):
        """Disable polling for a device."""
        self.set_interval(ieee, 0)


class ZigbeeService:
    """
    Core Zigbee service implementing zigpy's listener interface.
    Based on ZHA's gateway architecture with MQTT command handling.
    """

    def __init__(self, port, mqtt_client, config, event_callback=None):
        self.port = port
        self.app = None
        self.mqtt = mqtt_client
        self.callback = event_callback

        # Connect MQTT callbacks
        if self.mqtt:
            self.mqtt.command_callback = self.handle_mqtt_command
            # Connect HA Status callback (Birth Message)
            self.mqtt.ha_status_callback = self.republish_all_devices
            # Connect Bridge Status callback (for frontend notification)
            self.mqtt.status_change_callback = self.handle_bridge_status_change

        self.event_callback = event_callback or self._default_event_callback

        self.devices: Dict[str, ZHADevice] = {}
        self.friendly_names = self._load_json("names.json")
        self.device_settings = self._load_json("device_settings.json")
        self.polling_config = self._load_json("polling_config.json")

        # --- STATE CACHE ---
        self.state_cache = self._load_json("device_state_cache.json")
        self._cache_dirty = False
        # -------------------

        self.join_history = []
        self._config = config

        # Pairing state
        self.pairing_expiration = 0

        # Polling scheduler
        self.polling_scheduler = PollingScheduler(self)

        # Background tasks
        self._save_task = None
        self._watchdog_task = None

        # IEEE lookup by name (for MQTT command routing)
        self._name_to_ieee: Dict[str, str] = {}
        self._node_id_to_ieee: Dict[str, str] = {}  # node_id (no colons) -> ieee

        # Fast-path processor for time-critical events
        self.fast_path = FastPathProcessor(self)
        logger.info("Fast-path processor initialised")

        os.makedirs("logs", exist_ok=True)


    async def _default_event_callback(self, event_type: str, data: dict):
        """Default event callback that does nothing."""
        pass

    def _load_json(self, f):
        if os.path.exists(f):
            try:
                with open(f, 'r') as file:
                    return json.load(file)
            except Exception as e:
                logger.warning(f"Failed to load {f}: {e}")
                return {}
        return {}

    def _save_json(self, f, data):
        """Save JSON data to file with proper serialisation of zigpy types."""
        try:
            # Sanitize data to be JSON-safe
            safe_data = prepare_for_json(data)
            with open(f, 'w') as file:
                json.dump(safe_data, file, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {f}: {e}")

    def _save_state_cache(self):
        """Save current device states to cache file."""
        self._save_json("device_state_cache.json", self.state_cache)

    async def _periodic_save(self):
        """Periodically save cache to disk to prevent I/O blocking."""
        while True:
            try:
                await asyncio.sleep(30) # Save interval (30s)
                if self._cache_dirty:
                    # Run in executor to avoid blocking the loop
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._save_state_cache)
                    self._cache_dirty = False
                    logger.debug("State cache saved to disk")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic save: {e}")

    async def _loop_watchdog(self):
        """Monitor event loop lag."""
        while True:
            start = time.monotonic()
            await asyncio.sleep(1)
            duration = time.monotonic() - start
            if duration > 1.5:
                logger.warning(f" Event loop blocked for {duration:.2f}s (should be ~1.0s)")

    def get_safe_name(self, ieee):
        """Get a safe MQTT-friendly name for a device."""
        name = self.friendly_names.get(ieee, ieee)
        safe_name = re.sub(r'[+#/]', '-', name)
        return safe_name

    def _rebuild_name_maps(self):
        """Rebuild the name -> IEEE mapping for MQTT routing."""
        self._name_to_ieee.clear()
        self._node_id_to_ieee.clear()

        for ieee in self.devices:
            safe_name = self.get_safe_name(ieee)
            self._name_to_ieee[safe_name] = ieee
            self._name_to_ieee[safe_name.lower()] = ieee  # Case-insensitive

            # Also map node_id (IEEE without colons)
            node_id = ieee.replace(":", "")
            self._node_id_to_ieee[node_id] = ieee
            self._node_id_to_ieee[node_id.lower()] = ieee

    async def start(self, network_key=None):
        """Start the Zigbee network with enhanced resilience."""

        # ========================================================================
        # STEP 1: Load enhanced configuration FIRST (before building config)
        # ========================================================================
        from config_enhanced import get_production_config

        # Get device count for configuration optimization
        device_count = len(self.devices) if hasattr(self, 'devices') and self.devices else 0

        # Load enhanced EZSP configuration
        enhanced_config = get_production_config(self._config, device_count)
        logger.info(f"Loaded enhanced EZSP configuration (device count: {device_count})")

        # ========================================================================
        # STEP 2: Build EZSP Config (merge enhanced with user overrides)
        # ========================================================================

        # Start with enhanced configuration as base
        ezsp_conf = enhanced_config.copy()

        # Apply user overrides from config.yaml if present
        # This allows users to override the enhanced defaults
        user_ezsp = self._config.get('ezsp_config', {})

        # OLD MAPPING - Keep for backward compatibility with old config format
        config_map = {
            "packet_buffer_count": "CONFIG_PACKET_BUFFER_COUNT",
            "neighbour_table_size": "CONFIG_NEIGHBOR_TABLE_SIZE",
            "source_route_table_size": "CONFIG_SOURCE_ROUTE_TABLE_SIZE",
            "address_table_size": "CONFIG_ADDRESS_TABLE_SIZE",
            "multicast_table_size": "CONFIG_MULTICAST_TABLE_SIZE",
            "max_hops": "CONFIG_MAX_HOPS",
            "indirect_tx_timeout": "CONFIG_INDIRECT_TRANSMISSION_TIMEOUT",
            "aps_unicast_message_count": "CONFIG_APS_UNICAST_MESSAGE_COUNT"
        }

        # Apply user overrides (supports both old and new format)
        for friendly_key, ezsp_key in config_map.items():
            if friendly_key in user_ezsp:
                val = user_ezsp[friendly_key]
                ezsp_conf[ezsp_key] = val
                logger.info(f"User override: {ezsp_key} = {val}")

        # Also support direct CONFIG_* keys
        for key, val in user_ezsp.items():
            if key.startswith('CONFIG_'):
                ezsp_conf[key] = val
                logger.info(f"User override: {key} = {val}")

        # Build application configuration
        conf = {
            "device": {
                "path": self.port,
                "baudrate": 460800,
                "flow_control": "hardware"
            },
            "database_path": "zigbee.db",
            "ezsp_config": ezsp_conf,  # Now using enhanced + user overrides
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 0)
        }

        # ========================================================================
        # STEP 3: Robust Startup with Retries (your existing code)
        # ========================================================================
        for attempt in range(12):
            try:
                # Create the ControllerApplication
                self.app = await ControllerApplication.new(
                    config=conf,
                    auto_form=True,
                    start_radio=True
                )

                # ================================================================
                # STEP 4: Wrap with resilience system (ADD THIS)
                # ================================================================
                from resilience import wrap_with_resilience

                self.resilience = wrap_with_resilience(
                    self.app,
                    event_callback=self.event_callback
                )

                logger.info("✅ Resilience system enabled")
                # ================================================================

                # Register as listener for application-level events
                self.app.add_listener(self)

                # Load existing devices from database
                for ieee, zigpy_dev in self.app.devices.items():
                    await self._async_device_restored(zigpy_dev)

                # Rebuild name mappings
                self._rebuild_name_maps()

                # Start polling scheduler
                self.polling_scheduler.start()

                # Start background tasks
                self._save_task = asyncio.create_task(self._periodic_save())
                self._watchdog_task = asyncio.create_task(self.polling_scheduler._availability_watchdog_loop())
                #self._watchdog_task = asyncio.create_task(self._loop_watchdog())

                # Load saved polling intervals
                for ieee, interval in self.polling_config.items():
                    if ieee in self.devices:
                        self.polling_scheduler.set_interval(ieee, interval)

                await self._emit("log", {
                    "level": "INFO",
                    "message": f"Zigbee Core Started on {self.port}",
                    "ieee": None
                })
                logger.info(f"Zigbee network started successfully on {self.port}")

                # CRITICAL: Announce all devices to Home Assistant after startup
                # This ensures MQTT is connected and all devices are loaded
                asyncio.create_task(self.announce_all_devices())

                return

            except Exception as e:
                logger.warning(f"Startup Attempt {attempt + 1} failed: {e}")
                if self.app:
                    try:
                        await self.app.shutdown()
                    except:
                        pass
                await asyncio.sleep(2)

        raise RuntimeError("Failed to start Zigbee Radio after 12 attempts. Check hardware.")

    async def stop(self):
        """Shutdown the Zigbee network."""
        self.polling_scheduler.stop()

        if self._save_task: self._save_task.cancel()
        if self._watchdog_task: self._watchdog_task.cancel()

        # Force one last save
        if self._cache_dirty:
            self._save_state_cache()

        if self.app:
            await self.app.shutdown()
            logger.info("Zigbee network stopped")

    # =========================================================================
    # REPUBLISH / BIRTH MESSAGE HANDLER (NEW)
    # =========================================================================

    async def republish_all_devices(self):
        """
        Called when Home Assistant restarts (Birth Message).
        Republishes Discovery Config and Current State for ALL devices.
        """
        logger.info("✅ Home Assistant is ONLINE! Republishing all devices...")
        await self._emit("log", {"level": "INFO", "message": "HA Online - Republishing all devices", "ieee": None})

        # NOTIFY FRONTEND
        await self._emit("ha_status", {"status": "online"})

        # Wait a moment for HA to fully start listeners
        await asyncio.sleep(2)

        for ieee, device in self.devices.items():
            try:
                # 1. Resend Discovery Config
                await self.announce_device(ieee)

                # 2. Resend Current State (Retained)
                # We simply trigger a state update with current cache
                if device.state:
                    self.handle_device_update(device, device.state, full_state=device.state, qos=1)

                await asyncio.sleep(0.1) # Pacing to avoid flooding MQTT
            except Exception as e:
                logger.error(f"Failed to republish {ieee}: {e}")

        logger.info("✅ All devices republished to Home Assistant")

    async def handle_bridge_status_change(self, status: str):
        """
        Handle bridge/gateway status changes.
        Notifies frontend of MQTT bridge status (online/offline).

        Args:
            status: "online" or "offline"
        """
        logger.info(f"🌉 Bridge status changed: {status}")
        await self._emit("ha_status", {"status": status})

    # =========================================================================
    # MQTT COMMAND HANDLER
    # =========================================================================
    async def handle_mqtt_command(
            self,
            device_identifier: str,
            data: Dict[str, Any],
            component: Optional[str] = None,
            object_id: Optional[str] = None
    ):
        """
        Handle incoming MQTT command from Home Assistant.
        Supports JSON Schema with optimistic state updates.
        """
        ieee = self._resolve_device_identifier(device_identifier)
        if not ieee or ieee not in self.devices:
            logger.warning(f"MQTT command for unknown device: {device_identifier}")
            return

        device = self.devices[ieee]
        logger.info(f"[{ieee}] MQTT command: {data}")

        try:
            # Extract endpoint from object_id (e.g., "light_2" -> 2)
            endpoint = None
            if object_id:
                match = re.search(r'_(\d+)$', object_id)
                endpoint = int(match.group(1)) if match else 1

            # Track state changes for optimistic update
            optimistic_state = {}
            command_success = False

            # =========================================================================
            # JSON SCHEMA FORMAT
            # =========================================================================
            state = data.get('state')
            brightness = data.get('brightness')
            color_temp = data.get('color_temp')
            color = data.get('color')
            transition = data.get('transition')

            # Handle State
            if state:
                cmd = 'on' if str(state).upper() == 'ON' else 'off'
                result = await device.send_command(cmd, endpoint_id=endpoint)
                if result:
                    optimistic_state['state'] = state.upper() if isinstance(state, str) else ('ON' if state else 'OFF')
                    optimistic_state['on'] = (cmd == 'on')
                    command_success = True

            # Handle Brightness (HA sends 0-254)
            if brightness is not None:
                pct = int(brightness / 2.54)
                result = await device.send_command('brightness', pct, endpoint_id=endpoint)
                if result:
                    optimistic_state['brightness'] = int(brightness)
                    # If brightness > 0, also set state to ON
                    if brightness > 0:
                        optimistic_state['state'] = 'ON'
                        optimistic_state['on'] = True
                    command_success = True

            # Handle Color Temp (mireds)
            if color_temp is not None:
                try:
                    kelvin = int(1000000 / color_temp)
                    result = await device.send_command('color_temp', kelvin, endpoint_id=endpoint)
                    if result:
                        optimistic_state['color_temp'] = int(color_temp)
                        command_success = True
                except ZeroDivisionError:
                    pass

            # Handle XY Color
            if color and 'x' in color and 'y' in color:
                result = await device.send_command('xy_color', (color['x'], color['y']), endpoint_id=endpoint)
                if result:
                    optimistic_state['color'] = {'x': color['x'], 'y': color['y']}
                    command_success = True

            # =========================================================================
            # LEGACY FORMATS (fallback)
            # =========================================================================
            if not command_success and 'command' in data:
                command = data['command'].lower()
                value = data.get('value')
                ep = data.get('endpoint', endpoint)

                result = await device.send_command(command, value, endpoint_id=ep)
                if result:
                    command_success = True
                    # Build optimistic state for legacy commands
                    if command == 'on':
                        optimistic_state['state'] = 'ON'
                    elif command == 'off':
                        optimistic_state['state'] = 'OFF'
                    elif command == 'brightness':
                        optimistic_state['brightness'] = int(value * 2.54) if value <= 100 else value

            # =========================================================================
            # OPTIMISTIC STATE UPDATE
            # =========================================================================
            if command_success and optimistic_state:
                logger.info(f"[{ieee}] Optimistic update: {optimistic_state}")

                # Merge optimistic changes into device state
                device.update_state(optimistic_state, qos=0, endpoint_id=endpoint)

        except Exception as e:
            logger.error(f"[{ieee}] MQTT command error: {e}")
            traceback.print_exc()


    def _resolve_device_identifier(self, identifier: str) -> Optional[str]:
        """Resolve a device identifier (name, node_id, or IEEE) to IEEE address."""
        # Already an IEEE address?
        if identifier in self.devices:
            return identifier

        # Try name mapping
        if identifier in self._name_to_ieee:
            return self._name_to_ieee[identifier]

        # Try node_id mapping (IEEE without colons)
        if identifier in self._node_id_to_ieee:
            return self._node_id_to_ieee[identifier]

        # Try case-insensitive search
        lower_id = identifier.lower()
        if lower_id in self._name_to_ieee:
            return self._name_to_ieee[lower_id]
        if lower_id in self._node_id_to_ieee:
            return self._node_id_to_ieee[lower_id]

        # Try partial match on friendly names
        for name, ieee in self._name_to_ieee.items():
            if lower_id in name.lower():
                return ieee

        return None

    # =========================================================================
    # ZIGPY APPLICATION LISTENER INTERFACE
    # =========================================================================

    def device_joined(self, device: zigpy.device.Device):
        """Called when a device joins the network."""
        ieee = str(device.ieee)
        logger.info(f"Device joined: {ieee}")

        # Create device wrapper
        self.devices[ieee] = ZHADevice(self, device)

        # Mark as immediately seen (device just joined!)
        self.devices[ieee].last_seen = int(time.time() * 1000)

        # Record join history
        self.join_history.insert(0, {
            "join_timestamp": time.time() * 1000,
            "ieee_address": ieee,
            "manufacturer": str(device.manufacturer) if device.manufacturer else "Unknown",
            "model": str(device.model) if device.model else "Unknown",
        })

        self._rebuild_name_maps()

        # Enhanced Logging
        name = self.friendly_names.get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Joined"
        self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_joined", {"ieee": ieee})

    def raw_device_initialized(self, device: zigpy.device.Device):
        """Called when device descriptors are read but endpoints not yet configured."""
        logger.debug(f"Raw device initialized: {device.ieee}")

    def device_initialized(self, device: zigpy.device.Device):
        """Called when a device is fully initialized (endpoints configured)."""
        ieee = str(device.ieee)
        logger.info(f"Device initialized: {ieee}")

        if ieee in self.devices:
            # Re-wrap with full endpoint information
            self.devices[ieee] = ZHADevice(self, device)
            # Mark as seen
            self.devices[ieee].last_seen = int(time.time() * 1000)

            # Load cached state if available (including last_seen for availability)
            if ieee in self.state_cache:
                logger.info(f"[{ieee}] Restoring state from cache")
                self.devices[ieee].restore_state(self.state_cache[ieee])

            # Configure reporting and bindings
            asyncio.create_task(self._async_device_initialized(ieee))
        else:
            # New device
            self.devices[ieee] = ZHADevice(self, device)
            # Mark as seen
            self.devices[ieee].last_seen = int(time.time() * 1000)
            asyncio.create_task(self._async_device_initialized(ieee))

        self._rebuild_name_maps()
        self._emit_sync("device_initialized", {"ieee": ieee})

    def device_left(self, device: zigpy.device.Device):
        """Called when a device leaves the network."""
        ieee = str(device.ieee)
        logger.info(f"Device left: {ieee}")

        if ieee in self.devices:
            del self.devices[ieee]

        self.polling_scheduler.disable_for_device(ieee)
        self._rebuild_name_maps()

        # Enhanced Logging
        name = self.friendly_names.get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Left"
        self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_left", {"ieee": ieee})

    def device_removed(self, device: zigpy.device.Device):
        """Called when a device is removed from the network."""
        ieee = str(device.ieee)
        logger.info(f"Device removed: {ieee}")

        if ieee in self.devices:
            del self.devices[ieee]

        # Remove from cache
        if ieee in self.state_cache:
            del self.state_cache[ieee]
            self._save_state_cache()

        self.polling_scheduler.disable_for_device(ieee)
        self._rebuild_name_maps()

        # Enhanced Logging
        name = self.friendly_names.get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Removed"
        self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_left", {"ieee": ieee})

    def device_relays_updated(self, device: zigpy.device.Device, relays):
        pass

    def group_member_removed(self, *args, **kwargs): pass
    def group_member_added(self, *args, **kwargs): pass
    def group_added(self, *args, **kwargs): pass
    def group_removed(self, *args, **kwargs): pass

    def handle_message(
            self,
            sender: zigpy.device.Device,
            profile: int,
            cluster: int,
            src_ep: int,
            dst_ep: int,
            message: bytes
    ):
        """Raw message interceptor - called for EVERY Zigbee message."""
        ieee = str(sender.ieee)

        # === FAST PATH: Try immediate processing for time-critical messages ===
        try:
            fast_pathed = self.fast_path.process_frame(
                ieee, profile, cluster, src_ep, dst_ep, message
            )
            if fast_pathed:
                # Fast path handled it, but continue for debug/logging
                logger.debug(f"[{ieee}] Fast-pathed: cluster=0x{cluster:04x}")
        except Exception as e:
            logger.debug(f"[{ieee}] Fast path error: {e}")

        # Capture packet for debugging
        try:
            debugger = get_debugger()
            if debugger and debugger.enabled:
                debugger.capture_packet(
                    sender_ieee=ieee,
                    sender_nwk=sender.nwk,
                    profile=profile,
                    cluster=cluster,
                    src_ep=src_ep,
                    dst_ep=dst_ep,
                    message=message,
                    direction="RX"
                )
        except Exception as e:
            logger.debug(f"Debug capture error: {e}")

        # Handle Tuya manufacturer-specific cluster
        if cluster == 0xEF00 and ieee in self.devices:
            self.devices[ieee].handle_raw_message(cluster, message)

        # Log for debugging (only at debug level to avoid spam)
        logger.debug(f"[{ieee}] Raw message: profile=0x{profile:04x}, cluster=0x{cluster:04x}, "
                     f"src_ep={src_ep}, dst_ep={dst_ep}, len={len(message)}")

    # =========================================================================
    # INTERNAL DEVICE MANAGEMENT
    # =========================================================================

    async def _async_device_restored(self, device: zigpy.device.Device):
        """Handle a device restored from database on startup."""
        ieee = str(device.ieee)
        self.devices[ieee] = ZHADevice(self, device)

        # If this is the coordinator, mark it as seen immediately
        if self.devices[ieee].get_role() == "Coordinator":
            self.devices[ieee].last_seen = int(time.time() * 1000)

        # RESTORE STATE FROM CACHE (including last_seen for availability)
        if ieee in self.state_cache:
            self.devices[ieee].restore_state(self.state_cache[ieee])
            logger.debug(f"[{ieee}] Restored {len(self.state_cache[ieee])} state attributes from cache")

        logger.debug(f"Restored device: {ieee}")


    async def _async_device_initialized(self, ieee: str):
        """Configure a newly initialized device."""
        if ieee not in self.devices:
            return

        try:
            zdev = self.devices[ieee]
            await zdev.configure()
            logger.info(f"[{ieee}] Device configured successfully")

            # -------------------------------------------------------------------
            # --- PHILIPS HUE MOTION SENSOR SPECIFIC CONFIGURATION (NEW BLOCK) ---
            # -------------------------------------------------------------------
            manufacturer = str(zdev.zigpy_dev.manufacturer or "").lower()
            model = str(zdev.zigpy_dev.model or "").lower()

            if ("philips" in manufacturer or "signify" in manufacturer) and "sml" in model:
                logger.info(f"[{ieee}] Applying Philips Hue Motion Sensor (SML) fixes on EP2...")

                # Philips telemetry is on EP2 (Illuminance, Temp, Occupancy)
                # Configure Reporting for Illuminance Measurement
                await configure_illuminance_reporting(zdev, endpoint_id=2)

                # Configure Reporting for Temperature Measurement (often overlooked)
                await configure_temperature_reporting(zdev, endpoint_id=2)

                # NOTE: Occupancy (0x0406) usually reports correctly by default,
                # but adding explicit reporting here is also possible if needed.

            # Immediate Poll on join/configure
            logger.info(f"[{ieee}] Performing initial state poll...")
            await zdev.poll()

            # Trigger HA Discovery
            if self.mqtt:
                await self.announce_device(ieee)

        except Exception as e:
            logger.warning(f"[{ieee}] Device configuration failed: {e}")


    async def announce_device(self, ieee: str):
        """Publish HA Discovery configs for a device."""
        if not self.mqtt or ieee not in self.devices:
            return

        try:
            zdev = self.devices[ieee]
            configs = zdev.get_device_discovery_configs()

            # Use consistent safe name generation (Allows spaces now)
            safe_name = self.get_safe_name(ieee)

            device_info = {
                "ieee": ieee,
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "safe_name": safe_name,
                "model": str(zdev.zigpy_dev.model),
                "manufacturer": str(zdev.zigpy_dev.manufacturer)
            }

            await self.mqtt.publish_discovery(device_info, configs)
            logger.info(f"[{ieee}] Announced to Home Assistant (Topic: {safe_name})")
        except Exception as e:
            logger.error(f"[{ieee}] Failed to announce: {e}")

    async def announce_all_devices(self):
        """
        Announce ALL devices to Home Assistant on startup.
        This is called after the Zigbee network has fully started and MQTT is connected.
        Based on ZHA's device announcement pattern.
        """
        if not self.mqtt:
            logger.warning("Cannot announce devices - MQTT not available")
            return

        # Wait a moment to ensure MQTT is fully connected
        await asyncio.sleep(1)

        logger.info(f"📢 Announcing {len(self.devices)} devices to Home Assistant...")

        announced = 0
        failed = 0

        for ieee in list(self.devices.keys()):
            try:
                await self.announce_device(ieee)
                announced += 1
                await asyncio.sleep(0.1)  # Pace announcements to avoid MQTT flooding
            except Exception as e:
                logger.error(f"[{ieee}] Failed to announce: {e}")
                failed += 1

        logger.info(f"✅ Device announcement complete: {announced} successful, {failed} failed")
        await self._emit("log", {
            "level": "INFO",
            "message": f"Announced {announced} devices to Home Assistant",
            "ieee": None
        })

    # =========================================================================
    # POLLING MANAGEMENT API
    # =========================================================================

    async def set_polling_interval(self, ieee: str, interval: int):
        """Set polling interval for a device."""
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        self.polling_scheduler.set_interval(ieee, interval)

        # Save to config
        self.polling_config[ieee] = interval
        self._save_json("polling_config.json", self.polling_config)

        return {"success": True, "ieee": ieee, "interval": interval}

    def get_polling_interval(self, ieee: str) -> int:
        """Get polling interval for a device."""
        return self.polling_scheduler.get_interval(ieee)

    def get_all_polling_intervals(self) -> Dict[str, int]:
        """Get all polling intervals."""
        return self.polling_scheduler.get_all_intervals()

    # =========================================================================
    # PAIRING MANAGEMENT
    # =========================================================================

    async def permit_join(self, duration=240, ieee=None):
        """Enable or Disable pairing mode."""
        if duration == 0:
            self.pairing_expiration = 0
            # Broadcast disable to be safe
            await self.app.permit(0)

            msg = "Pairing disabled"
            self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": None})
            # Emit pairing status event for UI
            self._emit_sync("pairing_status", {"enabled": False, "remaining": 0})
            logger.info(msg)
            return {"success": True, "enabled": False}

        # Handle Enable
        # Set expiration time (Current time + duration)
        self.pairing_expiration = time.time() + duration

        if ieee:
            # Enable joining on a specific device (Router)
            if ieee not in self.devices:
                return {"success": False, "error": "Target device not found"}

            try:
                zdev = self.devices[ieee].zigpy_dev
                # Mgmt_Permit_Joining_req(duration, significance=1)
                # TC_Significance=1 means "permit joining on this device and children"
                result = await zdev.zdo.Mgmt_Permit_Joining_req(duration, 1)
                logger.info(f"[{ieee}] Permit join result: {result}")

                msg = f"Pairing enabled via {self.friendly_names.get(ieee, ieee)} for {duration}s"
                self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee})
                # Emit pairing status event for UI
                self._emit_sync("pairing_status", {"enabled": True, "remaining": duration})
                logger.info(msg)
                return {"success": True, "duration": duration, "target": ieee}
            except Exception as e:
                logger.error(f"Failed to enable pairing via {ieee}: {e}")
                return {"success": False, "error": str(e)}
        else:
            # Broadcast permit join (Coordinator + All Routers)
            try:
                await self.app.permit(duration)
                msg = f"Pairing enabled (Broadcast) for {duration}s"
                self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": None})
                # Emit pairing status event for UI
                self._emit_sync("pairing_status", {"enabled": True, "remaining": duration})
                logger.info(msg)
                return {"success": True, "duration": duration, "target": "all"}
            except Exception as e:
                logger.error(f"Failed to enable broadcast pairing: {e}")
                return {"success": False, "error": str(e)}

    async def touchlink_scan(self):
        """
        Perform Touchlink scan for Light Link devices (Ikea, Philips bulbs).
        """
        try:
            logger.info("Starting Touchlink scan for Light Link devices...")

            # Check if the application object has touchlink support
            if not hasattr(self.app, '_ezsp'):
                logger.warning("Touchlink not available - coordinator doesn't support EZSP")
                return {
                    "success": False,
                    "error": "Touchlink requires EZSP coordinator (Silicon Labs/Ember)"
                }

            # Check for the touchlink method in various possible locations
            touchlink_method = None

            # Try zigpy's newer API
            if hasattr(self.app, 'permit_with_link_key'):
                logger.info("Using zigpy permit_with_link_key for touchlink")
                try:
                    # Enable permit joining with the well-known touchlink key
                    touchlink_key = zigpy.types.KeyData([0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,
                                                         0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF])
                    await self.app.permit_with_link_key(bytes([0xFF]*8), touchlink_key, 60)

                    # Also enable standard permit join
                    await self.app.permit(60)

                    await self._emit("log", {
                        "level": "INFO",
                        "message": "Touchlink-style pairing enabled - power cycle bulb NOW (close to coordinator)",
                        "ieee": None
                    })

                    return {"success": True, "message": "Touchlink pairing enabled for 60 seconds"}

                # EXCEPTION HANDLER:
                except NcpFailure as e:
                    logger.error(f"NCP Failure during touchlink_scan: {e}")
                    if hasattr(self, 'resilience'):
                        await self.resilience.handle_ncp_failure(e)
                    return {"success": False, "error": f"NCP Failure: {e}"}
                except Exception as e:
                    logger.error(f"Touchlink scan failed: {e}")
                    return {"success": False, "error": str(e)}

            # Try direct EZSP touchlink scan (older firmware)
            if hasattr(self.app._ezsp, 'startScan'):
                logger.info("Using EZSP startScan for touchlink")
                try:
                    # EZSP scan for touchlink devices
                    # Channel mask for all channels
                    channel_mask = 0x07FFF800  # Channels 11-26
                    scan_result = await self.app._ezsp.startScan(
                        scanType=0x02,  # ENERGY_SCAN
                        channelMask=channel_mask,
                        duration=3
                    )
                    logger.info(f"EZSP scan initiated: {scan_result}")

                    await self._emit("log", {
                        "level": "INFO",
                        "message": "EZSP scan started - power cycle bulb within 10 seconds",
                        "ieee": None
                    })

                    return {"success": True, "message": "EZSP touchlink scan started"}
                except Exception as e:
                    logger.warning(f"EZSP startScan failed: {e}")

            # Fallback: Enhanced permit join with optimal settings for bulbs
            logger.info("Touchlink not directly supported - using enhanced permit join for bulbs")

            try:
                # Enable permit join on all devices with maximum time
                await self.app.permit(254)  # Maximum duration

                await self._emit("log", {
                    "level": "INFO",
                    "message": "Enhanced pairing mode for bulbs (254s) - reset bulb and power on",
                    "ieee": None
                })

                return {
                    "success": True,
                    "message": "Enhanced pairing enabled - Touchlink not directly supported but using optimal settings for bulbs",
                    "note": "Reset bulb (6x ON/OFF for Ikea, 5x for Hue) and keep close to coordinator"
                }
            except Exception as e:
                logger.error(f"Even enhanced permit join failed: {e}")
                raise

        except Exception as e:
            logger.error(f"Touchlink scan failed: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "recommendation": "Try standard pairing: Reset bulb, enable pairing, power on bulb within 30 seconds"
            }

    def get_pairing_status(self):
        """Get current pairing status and remaining time."""
        remaining = max(0, int(self.pairing_expiration - time.time()))
        return {
            "enabled": remaining > 0,
            "remaining": remaining
        }

    # =========================================================================
    # DEVICE MANAGEMENT API
    # =========================================================================

    async def remove_device(self, ieee, force=False):
        """Remove a device from the network and cleanup."""
        # Normalize IEEE
        ieee = str(ieee).lower()

        try:
            # 1. Get the zigpy device object (EUI64)
            z_ieee = zigpy.types.EUI64.convert(ieee)
            zdev = None

            # Check zigpy's internal list
            if z_ieee in self.app.devices:
                zdev = self.app.devices[z_ieee]

            # 2. Try graceful leave if device is known and online-ish
            if not force and zdev:
                logger.info(f"[{ieee}] Sending Leave Request...")
                try:
                    # Short timeout for leave request
                    async with asyncio.timeout(5.0):
                        await zdev.zdo.leave()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"[{ieee}] Leave request failed/timed out: {e}")

            # 3. FORCE REMOVE from Zigpy (Database)
            # CRITICAL FIX: Await the remove() call!
            if zdev:
                await self.app.remove(z_ieee)
                logger.info(f"[{ieee}] Removed from zigpy application")
            else:
                pass

            # 4. Cleanup Local State (In-Memory)
            if ieee in self.devices:
                del self.devices[ieee]

            # 5. Cleanup Persistent JSON Files
            if ieee in self.friendly_names:
                del self.friendly_names[ieee]
                self._save_json("names.json", self.friendly_names)

            if ieee in self.device_settings:
                del self.device_settings[ieee]
                self._save_json("device_settings.json", self.device_settings)

            # 6. Poll device
            if ieee in self.polling_config:
                del self.polling_config[ieee]
                self._save_json("polling_config.json", self.polling_config)

            # 7. Remove from state cache
            if ieee in self.state_cache:
                del self.state_cache[ieee]
                self._save_state_cache()

            self.polling_scheduler.disable_for_device(ieee)
            self._rebuild_name_maps()

            # 8. Notify Frontend
            self._emit_sync("device_left", {"ieee": ieee})
            self._emit_sync("log", {"level": "WARNING", "message": f"Device Removed: {ieee}", "ieee": ieee})

            # 9. Remove from HA discovery
            if self.mqtt and ieee in self.devices:
                configs = self.devices[ieee].get_device_discovery_configs()
                await self.mqtt.remove_discovery(ieee, configs)

            return {"success": True}
        # EXCEPTION HANDLER:
        except NcpFailure as e:
            logger.error(f"[{ieee}] NCP Failure during device removal: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"Remove failed: {e}")
            return {"success": False, "error": str(e)}

    async def rename_device(self, ieee, name):
        """Rename a device."""
        self.friendly_names[ieee] = name
        self._save_json("names.json", self.friendly_names)
        self._rebuild_name_maps()

        # Re-announce to HA with new name
        if self.mqtt and ieee in self.devices:
            await self.announce_device(ieee)

        return {"success": True}


    async def configure_device(self, ieee, config=None):
        """Reconfigure a device (bindings and reporting)."""
        if ieee in self.devices:
            try:
                # Pass config to device.configure
                await self.devices[ieee].configure(config)

                # Save settings to file for persistence if they are legacy style
                if config and 'tuya_settings' in config:
                    self.device_settings[ieee] = config
                    self._save_json("device_settings.json", self.device_settings)

                return {"success": True}

            # EXCEPTION HANDLER:
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during configuration: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}

    async def interview_device(self, ieee):
        """Re-interview a device."""
        if ieee in self.devices:
            try:
                await self.devices[ieee].interview()
                return {"success": True}

            # EXCEPTION HANDLER:
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during interview: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}


    async def poll_device(self, ieee):
        """Manually poll device attributes and send result notification."""
        if ieee in self.devices:
            try:
                device = self.devices[ieee]

                # Call poll, which returns results + success status
                results = await device.poll()

                # Get and remove flag for reporting
                poll_success = results.pop('__poll_success', True)

                friendly_name = self.friendly_names.get(ieee, ieee)

                if poll_success:
                    message = f"Manual poll for {friendly_name} successful. All attributes updated."
                    self._emit_sync("poll_result", {"ieee": ieee, "success": True, "message": message})
                    logger.info(f"[{ieee}] {message}")
                    return {"success": True, "message": "Poll successful"}
                else:
                    message = f"Manual poll for {friendly_name} completed, but one or more attributes failed to read after retries."
                    self._emit_sync("poll_result", {"ieee": ieee, "success": False, "message": message, "error_type": "PartialFailure"})
                    logger.warning(f"[{ieee}] {message}")
                    # Return success:True to the API client since the operation completed,
                    # but with warnings.
                    return {"success": True, "message": "Poll completed with partial failures. Check logs for details."}

            # EXCEPTION HANDLER for catastrophic errors (e.g., NCP Failure)
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during poll: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)

                error_message = f"Manual poll failed due to critical error (NCP Failure)."
                self._emit_sync("poll_result", {"ieee": ieee, "success": False, "message": error_message, "error_type": "NCPFailure"})
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                logger.error(f"[{ieee}] Manual poll failed: {e}")

                error_message = f"Manual poll failed: {str(e)}"
                self._emit_sync("poll_result", {"ieee": ieee, "success": False, "message": error_message, "error_type": "Exception"})
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Device not found"}


    async def bind_devices(self, source_ieee, target_ieee, cluster_id):
        """Bind a source device to a target device."""
        if source_ieee not in self.devices or target_ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        try:
            src_zdev = self.devices[source_ieee].zigpy_dev
            dst_zdev = self.devices[target_ieee].zigpy_dev

            # Get device-specific binding preferences
            src_prefs = self.devices[source_ieee].get_binding_preferences()
            dst_prefs = self.devices[target_ieee].get_binding_preferences()

            # === Find Source Endpoint ===
            src_ep = None

            # Try device-specific preference first
            preferred_src_ep = src_prefs.get('source_endpoints', {}).get(cluster_id)
            if preferred_src_ep and preferred_src_ep in src_zdev.endpoints:
                ep = src_zdev.endpoints[preferred_src_ep]
                if cluster_id in ep.out_clusters:
                    src_ep = ep
                    logger.debug(f"Using preferred source endpoint {preferred_src_ep} for cluster 0x{cluster_id:04x}")

            # Fallback: scan all endpoints
            if not src_ep:
                for ep_id, ep in sorted(src_zdev.endpoints.items()):
                    if ep_id == 0: continue
                    if cluster_id in ep.out_clusters:
                        src_ep = ep
                        break

            if not src_ep:
                return {"success": False, "error": f"Source device does not have output cluster 0x{cluster_id:04x}"}

            # === Find Target Endpoint ===
            dst_ep = None

            # Try device-specific preference first
            preferred_dst_ep = dst_prefs.get('target_endpoints', {}).get(cluster_id)
            if preferred_dst_ep and preferred_dst_ep in dst_zdev.endpoints:
                ep = dst_zdev.endpoints[preferred_dst_ep]
                if cluster_id in ep.in_clusters:
                    dst_ep = ep
                    logger.debug(f"Using preferred target endpoint {preferred_dst_ep} for cluster 0x{cluster_id:04x}")

            # Fallback: scan all endpoints
            if not dst_ep:
                for ep_id, ep in sorted(dst_zdev.endpoints.items()):
                    if ep_id == 0: continue
                    if cluster_id in ep.in_clusters:
                        dst_ep = ep
                        break

            if not dst_ep:
                # Last resort: use first non-ZDO endpoint
                logger.warning(f"Target device does not have input cluster 0x{cluster_id:04x}, using fallback")
                valid_eps = [ep for ep_id, ep in dst_zdev.endpoints.items() if ep_id != 0]
                if valid_eps:
                    dst_ep = valid_eps[0]
                else:
                    return {"success": False, "error": f"Target device has no valid endpoints"}

            # === Create Binding ===
            dst_addr = zdo_types.MultiAddress()
            dst_addr.addrmode = 3  # 64-bit IEEE addressing
            dst_addr.ieee = dst_zdev.ieee
            dst_addr.endpoint = dst_ep.endpoint_id

            logger.info(
                f"Creating binding: {source_ieee} EP{src_ep.endpoint_id} -> {target_ieee} EP{dst_ep.endpoint_id} (Cluster 0x{cluster_id:04x})")

            async with asyncio.timeout(15.0):
                result = await src_zdev.zdo.Bind_req(
                    src_zdev.ieee,
                    src_ep.endpoint_id,
                    cluster_id,
                    dst_addr
                )
                logger.info(f"Bind_req result: {result}")

            # Configure reporting for Thermostat
            if cluster_id == 0x0201:
                logger.info(f"Configuring thermostat attribute reporting")
                try:
                    if cluster_id in src_ep.out_clusters:
                        cluster = src_ep.out_clusters[cluster_id]

                        # Bind to coordinator for monitoring
                        await cluster.bind()

                        # Configure reporting for key thermostat attributes
                        await cluster.configure_reporting(
                            0x0000,  # local_temperature
                            30,      # min interval: 30 seconds
                            300,     # max interval: 5 minutes
                            50       # reportable change: 0.5°C (in 0.01°C units)
                        )
                        await cluster.configure_reporting(
                            0x0012,  # occupied_heating_setpoint
                            10,      # min interval: 10 seconds
                            3600,    # max interval: 1 hour
                            50       # reportable change: 0.5°C
                        )
                        await cluster.configure_reporting(
                            0x0008,  # pi_heating_demand
                            10,      # min interval: 10 seconds
                            900,     # max interval: 15 minutes
                            5        # reportable change: 5%
                        )
                        logger.info(f"Configured thermostat attribute reporting")
                except Exception as e:
                    logger.warning(f"Failed to configure reporting after binding: {e}")

            logger.info(f"Successfully bound {source_ieee} EP{src_ep.endpoint_id} {target_ieee} EP{dst_ep.endpoint_id} (Cluster 0x{cluster_id:04x})")

            await self._emit("log", {
                "level": "INFO",
                "message": f"Bound: {self.friendly_names.get(source_ieee, source_ieee)} EP{src_ep.endpoint_id} -> {self.friendly_names.get(target_ieee, target_ieee)} EP{dst_ep.endpoint_id}",
                "ieee": source_ieee
            })

            return {
                "success": True,
                "source_ep": src_ep.endpoint_id,
                "target_ep": dst_ep.endpoint_id,
                "message": f"Bound EP{src_ep.endpoint_id} -> EP{dst_ep.endpoint_id}"
            }
        # eXCEPTION HANDLER:
        except NcpFailure as e:
            logger.error(f"NCP Failure during binding: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"Binding failed: {e}")
            return {"success": False, "error": str(e)}


    async def send_command(self, ieee: str, command: str, value=None, endpoint_id=None):
        """Send a command to a device."""
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        try:
            device = self.devices[ieee]
            result = await device.send_command(command, value, endpoint_id)
            return {"success": True, "result": result}

        # ADD THIS EXCEPTION HANDLER:
        except NcpFailure as e:
            logger.error(f"[{ieee}] NCP Failure during command: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"[{ieee}] Command failed: {e}")
            return {"success": False, "error": str(e)}


    async def read_attribute(self, ieee, ep_id, cluster_id, attr_name):
        """Read a specific attribute from a device."""
        if ieee in self.devices:
            try:
                result = await self.devices[ieee].read_attribute_raw(ep_id, cluster_id, attr_name)
                return {"success": True, "value": str(result)}

            # EXCEPTION HANDLER:
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during interview: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}  #


    # =========================================================================
    # TOPOLOGY & MESH (REAL DATA)
    # =========================================================================

    def get_simple_mesh(self):
        """Get network topology for mesh visualization using REAL neighbor data."""
        nodes = []
        connections = []

        # 1. Build Nodes
        for ieee, zdev in self.devices.items():
            d = zdev.zigpy_dev
            nodes.append({
                "id": ieee,
                "ieee_address": ieee,
                "network_address": hex(d.nwk),
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "role": zdev.get_role(),
                "manufacturer": str(d.manufacturer) if d.manufacturer else "Unknown",
                "model": str(d.model) if d.model else "Unknown",
                "lqi": getattr(d, 'lqi', 0) or 0,
                "online": zdev.is_available(),
                "polling_interval": self.polling_scheduler.get_interval(ieee)
            })

        # 2. Build Links from Zigpy Topology
        if hasattr(self.app, 'topology') and self.app.topology.neighbors:
            for src_ieee, neighbors in self.app.topology.neighbors.items():
                src_str = str(src_ieee)
                for neighbor in neighbors:
                    dst_str = str(neighbor.ieee)
                    # Filter out links to unknown devices to prevent ghost nodes
                    if src_str in self.devices and dst_str in self.devices:
                        connections.append({
                            "source": src_str,
                            "target": dst_str,
                            "lqi": neighbor.lqi or 0,
                            "relationship": getattr(neighbor, 'relationship', 'Unknown')
                        })

        # 3. Fallback: If topology is empty (no scan yet), attach EndDevices to parents if known
        # This handles the "hub and spoke" visually until a scan completes
        if not connections:
            for ieee, zdev in self.devices.items():
                # If EndDevice has a known parent in zigpy
                # (Not always populated without scan, but worth a try)
                pass

        return {"success": True, "nodes": nodes, "connections": connections}

    async def scan_network_topology(self):
        """Force a topology scan to populate neighbor tables."""
        if hasattr(self.app, 'topology'):
            logger.info("Starting manual topology scan...")
            await self.app.topology.scan()
            logger.info("Topology scan complete.")
            return {"success": True, "message": "Scan complete"}
        return {"success": False, "error": "Topology scanning not supported"}

    def get_join_history(self):
        """Get device join history."""
        return self.join_history

    # =========================================================================
    # EVENT EMISSION HELPERS
    # =========================================================================

    def _emit_sync(self, evt, data):
        """Emit event synchronously."""
        if self.callback:
            asyncio.create_task(self.callback(evt, data))

    async def _emit(self, evt, data):
        """Emit event asynchronously."""
        if self.callback:
            await self.callback(evt, data)

    # =========================================================================
    # DEVICE UPDATE HANDLING
    # =========================================================================
    def handle_device_update(self, zha_device, changed_data, full_state=None, qos: Optional[int] = None, endpoint_id: Optional[int] = None):
        """
        Called by ZHADevice when state changes.
        ENHANCED: Adapts Light/Switch payload to JSON Schema while keeping Sensors as templates.
        """
        ieee = zha_device.ieee

        # Use full_state if provided, otherwise assume changed_data is everything we know
        payload_data = full_state if full_state else changed_data

        # Start with base payload
        payload = {
            'available': zha_device.is_available(),
            'linkquality': getattr(zha_device.zigpy_dev, 'lqi', 0) or 0,
        }

        # ========================================================================
        # 2: Process Capabilities (Lights = JSON Schema, Sensors = Template)
        # ========================================================================
        caps = zha_device.capabilities if hasattr(zha_device, 'capabilities') else None

        if caps:
            # ---------------------------------------------------------------------
            # LIGHTS & SWITCHES: Convert to standard JSON Schema (state, brightness, etc.)
            # ---------------------------------------------------------------------
            if caps.is_light or caps.is_switch:
                # Determine State (ON/OFF)
                # Try specific endpoint key first, then general keys
                state_val = (payload_data.get(f'state_{endpoint_id}') or
                             payload_data.get('state') or
                             zha_device.state.get(f'state_{endpoint_id}') or
                             zha_device.state.get('state', 'OFF'))

                on_val = payload_data.get(f'on_{endpoint_id}') or payload_data.get('on')

                # Normalize to ON/OFF
                if on_val is not None:
                    payload['state'] = 'ON' if on_val else 'OFF'
                elif state_val is not None:
                    if isinstance(state_val, str):
                        payload['state'] = state_val.upper()
                    else:
                        payload['state'] = 'ON' if state_val else 'OFF'

                # Brightness (JSON Schema expects 0-254)
                if caps.supports_brightness:
                    bri = (payload_data.get(f'brightness_{endpoint_id}') or
                           payload_data.get('brightness') or
                           payload_data.get('level'))

                    if bri is None:
                        bri = zha_device.state.get('brightness') or zha_device.state.get('level', 0)

                    # Auto-scale 0-100 to 0-254 if detected
                    if isinstance(bri, (int, float)):
                        if bri <= 100 and bri > 1:
                            # User requested 2.54 scaling logic:
                            bri = int(bri * 2.54)
                        payload['brightness'] = min(254, max(0, int(bri)))

                # Color Temperature (Mireds)
                if caps.supports_color_temp:
                    ct = payload_data.get('color_temp_mireds') or payload_data.get('color_temp')
                    if ct is None:
                        ct = zha_device.state.get('color_temp_mireds', 250)
                    if ct:
                        payload['color_temp'] = int(ct)

            # ---------------------------------------------------------------------
            # SENSORS: Retain existing keys (Contact, Power, Temp, etc.)
            # ---------------------------------------------------------------------

            # === CONTACT SENSORS ===
            if caps.is_contact_sensor:
                # Check for endpoint specific or general contact state
                contact_key = f"is_open_{endpoint_id}" if endpoint_id else "is_open"
                val = payload_data.get(contact_key)
                if val is None:
                    val = zha_device.state.get(contact_key, False)
                payload[contact_key] = val

            # === POWER MONITORING ===
            if caps.supports_power_monitoring:
                for attr in ['power', 'voltage', 'current']:
                    key = f"{attr}_{endpoint_id}" if endpoint_id else attr
                    if key not in payload:
                        payload[key] = payload_data.get(key) or zha_device.state.get(key, 0)

            # === ENVIRONMENTAL SENSORS ===
            if caps.is_temperature_sensor:
                payload['temperature'] = payload_data.get('temperature', zha_device.state.get('temperature'))

            if caps.is_humidity_sensor:
                payload['humidity'] = payload_data.get('humidity', zha_device.state.get('humidity'))

            if caps.is_illuminance_sensor:
                payload['illuminance'] = payload_data.get('illuminance', zha_device.state.get('illuminance'))

            # === MOTION/OCCUPANCY ===
            if caps.is_motion_sensor:
                payload['occupancy'] = payload_data.get('occupancy', zha_device.state.get('occupancy', False))
                payload['motion'] = payload_data.get('motion', zha_device.state.get('motion', False))

        # ========================================================================
        # 3: Finalize, Cache & Publish
        # ========================================================================

        # SANITISE payload
        from json_helpers import sanitise_device_state
        safe_payload = sanitise_device_state(payload)

        # UPDATE CACHE
        if ieee not in self.state_cache:
            self.state_cache[ieee] = {}
        self.state_cache[ieee].update(safe_payload)
        self._cache_dirty = True

        # Emit to WebSocket clients
        self._emit_sync("device_updated", {"ieee": ieee, "data": safe_payload})

        # Publish to MQTT
        if self.mqtt:
            safe_name = self.get_safe_name(ieee)
            asyncio.create_task(self.mqtt.publish(safe_name, json.dumps(safe_payload), qos=qos))

        # ========================================================================
        # 4: Logging
        # ========================================================================
        friendly_name = self.friendly_names.get(ieee, "Unknown")

        for k, v in changed_data.items():
            if k != 'last_seen':
                ep_str = f"[EP{endpoint_id}]" if endpoint_id is not None else ""
                msg = f"[{ieee}] ({friendly_name}) {ep_str} {k}={v}"

                log_payload = {
                    "level": "INFO",
                    "message": msg,
                    "ieee": ieee,
                    "device_name": friendly_name,
                    "category": "attribute_update",
                    "attribute": k,
                    "value": v,
                    "endpoint_id": endpoint_id
                }
                from json_helpers import prepare_for_json
                safe_log_payload = prepare_for_json(log_payload)
                self._emit_sync("log", safe_log_payload)

        # ========================================================================
        # 3: Finalize, Cache & Publish
        # ========================================================================

        # SANITISE payload
        from json_helpers import sanitise_device_state
        safe_payload = sanitise_device_state(payload)

        # UPDATE CACHE
        if ieee not in self.state_cache:
            self.state_cache[ieee] = {}
        self.state_cache[ieee].update(safe_payload)
        self._cache_dirty = True

        # Emit to WebSocket clients
        self._emit_sync("device_updated", {"ieee": ieee, "data": safe_payload})

        # Publish to MQTT
        if self.mqtt:
            safe_name = self.get_safe_name(ieee)
            asyncio.create_task(self.mqtt.publish(safe_name, json.dumps(safe_payload), qos=qos))

        # ========================================================================
        # 4: Logging
        # ========================================================================
        friendly_name = self.friendly_names.get(ieee, "Unknown")

        for k, v in changed_data.items():
            if k != 'last_seen':
                ep_str = f"[EP{endpoint_id}]" if endpoint_id is not None else ""
                msg = f"[{ieee}] ({friendly_name}) {ep_str} {k}={v}"

                log_payload = {
                    "level": "INFO",
                    "message": msg,
                    "ieee": ieee,
                    "device_name": friendly_name,
                    "category": "attribute_update",
                    "attribute": k,
                    "value": v,
                    "endpoint_id": endpoint_id
                }
                from json_helpers import prepare_for_json
                safe_log_payload = prepare_for_json(log_payload)
                self._emit_sync("log", safe_log_payload)

    # =========================================================================
    # API METHODS
    # =========================================================================

    def get_device_list(self):
        """Get list of all devices with their current state - JSON-safe."""
        res = []
        for ieee, zdev in self.devices.items():
            d = zdev.zigpy_dev
            caps = []

            for ep_id, ep in d.endpoints.items():
                if ep_id == 0:
                    continue  # Skip ZDO

                caps.append({
                    "id": ep_id,
                    "profile": f"0x{ep.profile_id:04x}" if ep.profile_id else "0x0000",
                    "inputs": [{"id": c.cluster_id, "name": c.name} for c in ep.in_clusters.values()],
                    "outputs": [{"id": c.cluster_id, "name": c.name} for c in ep.out_clusters.values()]
                })

            res.append({
                "ieee": ieee,
                "nwk": hex(d.nwk),
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "model": str(d.model) if d.model else "Unknown",
                "manufacturer": str(d.manufacturer) if d.manufacturer else "Unknown",
                "lqi": getattr(d, 'lqi', 0) or 0,
                "last_seen_ts": zdev.last_seen,
                "state": zdev.state,
                "type": zdev.get_role(),
                "quirk": getattr(d, 'quirk_class', type(None)).__name__,
                "capabilities": caps,
                "settings": self.device_settings.get(ieee, {}),
                "available": zdev.is_available(),
                "config_schema": zdev.get_device_config_schema() if hasattr(zdev, 'get_device_config_schema') else [],
                "polling_interval": self.polling_scheduler.get_interval(ieee)
            })

        # Sanitize the entire result to be JSON-safe
        return prepare_for_json(res)