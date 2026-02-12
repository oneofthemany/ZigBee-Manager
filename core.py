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
import bellows.uart
import bellows.config
from bellows.ash import NcpFailure
import zigpy.types
import zigpy.config
import zigpy.device
import bellows.ezsp
import zigpy_znp.api
import zigpy_znp.config
from pathlib import Path
import asyncio
import json
import time


# Import ZDO types for binding
import zigpy.zdo.types as zdo_types
from zigpy.zcl.clusters.security import IasZone

# Import Device Wrapper
from device import ZigManDevice

# import services
from modules.json_helpers import prepare_for_json, sanitise_device_state
from modules.packet_stats import packet_stats
from modules.zones import ZoneManager
from handlers.zones_handler import setup_rssi_listener
from modules.zigbee_debug import get_debugger
from handlers.fast_path import FastPathProcessor
from modules.device_ban import get_ban_manager
from modules.touchlink import create_touchlink_manager, TouchlinkManager
#from handlers.sensors import configure_illuminance_reporting, configure_temperature_reporting

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

                    power_source = device.state.get('power_source', '').lower()
                    is_battery = 'battery' in power_source

                    # ===== DIAGNOSTIC LOOP =====
                    manufacturer = str(device.zigpy_dev.manufacturer or "").lower()
                    model = str(device.zigpy_dev.model or "").lower()
                    is_philips_motion = ("philips" in manufacturer or "signify" in manufacturer) and "sml" in model

                    if is_philips_motion:
                        logger.warning(f"[{ieee}] Philips motion sensor in poll loop! "
                                       f"power_source='{power_source}', is_battery={is_battery}")

                    # Skip passive battery sensors
                    is_sensor = any([
                        0x0406 in [h.CLUSTER_ID for h in device.handlers.values()],
                        0x0500 in [h.CLUSTER_ID for h in device.handlers.values()],
                        device.get_role() == "EndDevice" and not any([
                            0x0006 in ep.in_clusters for ep in device.zigpy_dev.endpoints.values()
                        ])
                    ])

                    if is_battery and is_sensor:
                        logger.debug(f"[{ieee}] Skipping poll - battery sensor")
                        continue

                    # Skip covers during movement
                    is_cover = 0x0102 in [h.CLUSTER_ID for h in device.handlers.values()]
                    if is_cover:
                        cover_state = device.state.get('state', '').lower()
                        if cover_state in ['opening', 'closing']:
                            logger.debug(f"[{ieee}] Skipping poll - cover moving")
                            continue

                    # Skip TRVs during active heating
                    is_trv = 0x0201 in [h.CLUSTER_ID for h in device.handlers.values()]
                    if is_trv and is_battery:
                        pi_heating_demand = device.state.get('pi_heating_demand', 0)
                        if pi_heating_demand > 0:
                            logger.debug(f"[{ieee}] Skipping poll - TRV actively heating")
                            continue

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
        self._update_debounce_tasks = {}

        # Connect MQTT callbacks
        if self.mqtt:
            self.mqtt.command_callback = self.handle_mqtt_command
            # Connect HA Status callback (Birth Message)
            self.mqtt.ha_status_callback = self.republish_all_devices
            # Connect Bridge Status callback (for frontend notification)
            self.mqtt.status_change_callback = self.handle_bridge_status_change

        self.event_callback = event_callback or self._default_event_callback

        self.devices: Dict[str, ZigManDevice] = {}
        self.friendly_names = self._load_json("./data/names.json")
        self.device_settings = self._load_json("./data/device_settings.json")
        self.polling_config = self._load_json("./data/polling_config.json")

        # --- STATE CACHE ---
        self.state_cache = self._load_json("./data/device_state_cache.json")
        self._cache_dirty = False
        self._save_task = None  # Track debounce task
        self._debounce_seconds = 2.0

        self.join_history = []
        self._config = config

        # Pairing state
        self.pairing_expiration = 0

        # Banning
        self.ban_manager = get_ban_manager()

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

        # Create Group Manager
        from modules.groups import GroupManager
        self.group_manager = GroupManager(self)

        # Zone Manager
        self.zone_manager = None

        # Connect MQTT callbacks
        if self.mqtt:
            self.mqtt.command_callback = self.handle_mqtt_command
            self.mqtt.ha_status_callback = self.republish_all_devices
            self.mqtt.status_change_callback = self.handle_bridge_status_change
            self.mqtt.group_command_callback = self.group_manager.handle_mqtt_group_command

        self._accepting_commands = False  # Block commands until ready

        # Touchlink
        self._touchlink: Optional[TouchlinkManager] = None

        # tabs

        self.device_tabs = self._load_json("./data/device_tabs.json") or {}
        # Format: {"Heating": ["ieee1", "ieee2"], "Lighting": ["ieee3"]}

        os.makedirs("logs", exist_ok=True)


    async def _probe_radio_type(self) -> str:
        """Get radio type from config or probe"""

        # Check if manually specified
        radio_type = self._config.get('radio_type', 'auto')
        if radio_type != 'auto':
            logger.info(f"Using manually configured radio type: {radio_type.upper()}")
            return radio_type.upper()

        detected_type = None

        # Try ZNP first
        logger.info(f"Probing {self.port} for ZNP radio...")
        try:

            znp = zigpy_znp.api.ZNP(zigpy_znp.config.CONFIG_SCHEMA({"device": {"path": self.port}}))
            try:
                await asyncio.wait_for(znp.connect(), timeout=3.0)
                detected_type = "ZNP"
                logger.info("✅ ZNP radio detected")
            finally:
                try:
                    znp.close()
                    if hasattr(znp, '_uart') and znp._uart:
                        transport = getattr(znp._uart, '_transport', None)
                        if transport:
                            transport.close()
                except:
                    pass  # Ignore cleanup errors
                await asyncio.sleep(1.0)  # Wait for background tasks to finish
                del znp
        except Exception as e:
            logger.info(f"Not ZNP: {e}")

        if detected_type == "ZNP":
            await asyncio.sleep(3.0)
            return "ZNP"

        # Try EZSP
        logger.info(f"Probing {self.port} for EZSP radio...")
        try:

            protocol = await asyncio.wait_for(
                bellows.uart.connect({
                    "path": self.port,
                    "baudrate": 115200,
                    "flow_control": "hardware"
                }, None),
                timeout=5.0
            )
            detected_type = "EZSP"
            logger.info("✅ EZSP radio detected")
            try:
                protocol.close()
            except:
                pass  # Ignore cleanup errors
            await asyncio.sleep(1.0)  # Wait for background tasks to finish
            del protocol
        except Exception as e:
            logger.info(f"Not EZSP: {e}")

        if detected_type == "EZSP":
            logger.info("Note: Background task errors during probe are expected and harmless")
            await asyncio.sleep(3.0)
            return "EZSP"

        raise RuntimeError(f"No compatible Zigbee radio found on {self.port}")


    def _get_radio_config(self) -> dict:
        """Extract radio-specific config"""
        radio_type = self._config.get('radio_type', 'auto')

        if radio_type == 'auto':
            # Will be detected in _probe_radio_type
            return {}
        elif radio_type in self._config:
            return self._config[radio_type]
        else:
            logger.warning(f"No config found for radio_type: {radio_type}")
            return {}

    def _build_ezsp_config(self, ezsp_conf: dict, network_key) -> dict:
        """Build EZSP config from zigbee.ezsp section"""
        ezsp_settings = self._config.get('ezsp', {})

        return {
            "device": {
                "path": self.port,
                "baudrate": ezsp_settings.get('baudrate', 460800),
                "flow_control": ezsp_settings.get('flow_control', 'hardware')
            },
            "database_path": "zigbee.db",
            "ezsp_config": ezsp_conf,  # From enhanced + user overrides
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 0)
        }

    def _build_znp_config(self, network_key) -> dict:
        """Build ZNP config from zigbee.znp section"""
        znp_settings = self._config.get('znp', {})

        return {
            "device": {
                "path": self.port,
                "baudrate": znp_settings.get('baudrate', 115200)
            },
            "database_path": "zigbee.db",
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 0)
        }

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
        self._save_json("./data/device_state_cache.json", self.state_cache)

    async def _debounced_save(self):
        """Save state cache after debounce period."""
        try:
            await asyncio.sleep(self._debounce_seconds)
            if self._cache_dirty:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._save_state_cache)
                self._cache_dirty = False
                logger.debug("State cache saved to disk")
        except asyncio.CancelledError:
            pass

    def _schedule_save(self):
        """Schedule a debounced save, canceling any pending save."""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()

        self._save_task = asyncio.create_task(self._debounced_save())

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
        # STEP 1: Backwards Compatibility Migration
        # ========================================================================
        if 'ezsp_config' in self._config and 'ezsp' not in self._config:
            logger.info("Migrating old EZSP config format...")
            self._config['ezsp'] = {
                'baudrate': self._config.get('baudrate', 460800),
                'flow_control': self._config.get('flow_control', 'hardware'),
                'config': self._config['ezsp_config']
            }

        # ========================================================================
        # STEP 2: Probe Radio Type
        # ========================================================================
        radio_type = await self._probe_radio_type()
        logger.info(f"✅ Detected radio type: {radio_type}")

        # ========================================================================
        # STEP 3: Import Correct Radio Driver
        # ========================================================================
        if radio_type == "EZSP":
            from bellows.zigbee.application import ControllerApplication
        elif radio_type == "ZNP":
            from zigpy_znp.zigbee.application import ControllerApplication
        else:
            raise RuntimeError(f"Unsupported radio type: {radio_type}")

        # ========================================================================
        # STEP 4: Build Radio-Specific Configuration
        # ========================================================================
        if radio_type == "EZSP":
            # Load enhanced EZSP configuration
            from modules.config_enhanced import get_production_config
            device_count = len(self.devices) if hasattr(self, 'devices') and self.devices else 0
            enhanced_config = get_production_config(self._config, device_count)
            logger.info(f"Loaded enhanced EZSP config (device count: {device_count})")

            # Merge with user overrides
            ezsp_conf = enhanced_config.copy()
            user_ezsp = self._config.get('ezsp', {}).get('config', {})

            # Support old format too
            if not user_ezsp and 'ezsp_config' in self._config:
                user_ezsp = self._config['ezsp_config']

            # OLD MAPPING - Keep for backward compatibility
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

            # Apply user overrides
            for friendly_key, ezsp_key in config_map.items():
                if friendly_key in user_ezsp:
                    ezsp_conf[ezsp_key] = user_ezsp[friendly_key]
                    logger.info(f"User override: {ezsp_key} = {user_ezsp[friendly_key]}")

            # Also support direct CONFIG_* keys
            for key, val in user_ezsp.items():
                if key.startswith('CONFIG_'):
                    ezsp_conf[key] = val
                    logger.info(f"User override: {key} = {val}")

            conf = self._build_ezsp_config(ezsp_conf, network_key)

        elif radio_type == "ZNP":
            conf = self._build_znp_config(network_key)

        # ========================================================================
        # STEP 5: Robust Startup with Retries
        # ========================================================================
        for attempt in range(12):
            try:
                # Create the ControllerApplication
                self.app = await ControllerApplication.new(
                    config=conf,
                    auto_form=True,
                    start_radio=True
                )

                self._touchlink = await create_touchlink_manager(self.app)
                if self._touchlink:
                    logger.info(f"✅ Touchlink support enabled ({self._touchlink.coordinator_type})")

                # ================================================================
                # STEP 6: Wrap with resilience system (EZSP only for now)
                # ================================================================
                if radio_type == "EZSP":
                    from modules.resilience import wrap_with_resilience
                    self.resilience = wrap_with_resilience(
                        self.app,
                        event_callback=self.event_callback
                    )
                    logger.info("✅ Resilience system enabled")

                # Register as listener for application-level events
                self.app.add_listener(self)

                # ================================================================
                # STEP 7: HOOK RADIO LAYER FOR LIVE RSSI/LQI CAPTURE
                # ================================================================
                if radio_type == "EZSP":
                    try:
                        # Method 1: Hook at bellows EZSP callback level
                        ezsp = self.app._ezsp

                        # Store original callback
                        if hasattr(ezsp, '_protocol') and hasattr(ezsp._protocol, 'cb'):
                            original_cb = ezsp._protocol.cb

                            class LQICapturingCallback:
                                def __init__(self, original, service):
                                    self._original = original
                                    self._service = service

                                def __getattr__(self, name):
                                    return getattr(self._original, name)

                                def incomingMessageHandler(self, *args, **kwargs):
                                    """Intercept incoming messages to extract LQI."""
                                    # args typically: (messageType, apsFrame, lastHopLqi, lastHopRssi, sender, ...)
                                    try:
                                        if len(args) >= 4:
                                            lqi = args[2] if len(args) > 2 else None  # lastHopLqi
                                            rssi = args[3] if len(args) > 3 else None  # lastHopRssi
                                            sender_nwk = args[4] if len(args) > 4 else None

                                            if lqi is not None and sender_nwk is not None:
                                                # Convert NWK to IEEE
                                                for ieee, dev in self._service.app.devices.items():
                                                    if dev.nwk == sender_nwk:
                                                        sender_ieee = str(ieee)

                                                        zone_mgr = getattr(self._service, 'zone_manager', None)
                                                        if zone_mgr:
                                                            coord_ieee = str(self._service.app.ieee)
                                                            if rssi is None:
                                                                rssi = int(-100 + (lqi / 255) * 70)

                                                            zone_mgr.record_link_quality(
                                                                source_ieee=coord_ieee,
                                                                target_ieee=sender_ieee,
                                                                rssi=rssi,
                                                                lqi=lqi
                                                            )
                                                        break
                                    except Exception:
                                        pass

                                    # Call original handler
                                    if hasattr(self._original, 'incomingMessageHandler'):
                                        return self._original.incomingMessageHandler(*args, **kwargs)

                            ezsp._protocol.cb = LQICapturingCallback(original_cb, self)
                            logger.info("✅ Hooked EZSP incomingMessageHandler for live LQI")

                    except Exception as e:
                        logger.warning(f"Could not hook EZSP for LQI capture: {e}")

                # ================================================================
                # STEP 8: HOOK handle_message TO CAPTURE RSSI WITH LIVE LQI
                # ================================================================
                self._original_handle_message = self.handle_message

                def wrapped_handle_message(sender, profile, cluster, src_ep, dst_ep, message):
                    result = self._original_handle_message(sender, profile, cluster, src_ep, dst_ep, message)

                    # Capture LQI from sender device object (set by zigpy)
                    zone_mgr = getattr(self, 'zone_manager', None)
                    if zone_mgr:
                        ieee = str(sender.ieee)
                        lqi = getattr(sender, 'lqi', None)
                        rssi = getattr(sender, 'rssi', None)

                        if lqi is not None:
                            coord_ieee = str(self.app.ieee)
                            if rssi is None:
                                rssi = int(-100 + (lqi / 255) * 70)

                            zone_mgr.record_link_quality(
                                source_ieee=coord_ieee,
                                target_ieee=ieee,
                                rssi=rssi,
                                lqi=lqi
                            )

                    return result

                self.handle_message = wrapped_handle_message
                logger.info("✅ Wrapped handle_message for fallback LQI capture")

                # Load existing devices from database
                for ieee, zigpy_dev in self.app.devices.items():
                    await self._async_device_restored(zigpy_dev)

                # Load existing devices from database
                for ieee, zigpy_dev in self.app.devices.items():
                    await self._async_device_restored(zigpy_dev)

                # Rebuild name mappings
                self._rebuild_name_maps()

                # Start polling scheduler
                self.polling_scheduler.start()

                # Start background tasks
                self._watchdog_task = asyncio.create_task(self.polling_scheduler._availability_watchdog_loop())

                # Load saved polling intervals
                for ieee, interval in self.polling_config.items():
                    if ieee in self.devices:
                        self.polling_scheduler.set_interval(ieee, interval)

                await self._emit("log", {
                    "level": "INFO",
                    "message": f"Zigbee Core Started on {self.port} ({radio_type})",
                    "ieee": None
                })
                logger.info(f"Zigbee network started successfully on {self.port} ({radio_type})")

                # CRITICAL: Announce all devices to Home Assistant after startup
                asyncio.create_task(self.announce_all_devices())

                # Initialise zones
                asyncio.create_task(self._init_zones_internal())

                return

            except Exception as e:
                logger.warning(f"Startup Attempt {attempt + 1} failed: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")
                if self.app:
                    try:
                        await self.app.shutdown()
                    except:
                        pass
                await asyncio.sleep(2)

        raise RuntimeError("Failed to start Zigbee Radio after 12 attempts. Check hardware.")


    async def _init_zones_internal(self):
        """Internal zones init called from start()."""
        try:
            await asyncio.sleep(2)  # Brief delay for network stability
            await self.init_zones()
        except Exception as e:
            logger.error(f"Failed to initialise zones: {e}")

    async def init_zones(self, mqtt_handler=None):
        """Initialise zone manager after Zigbee network is started."""
        from pathlib import Path
        import yaml

        # Initialise the manager
        # PASS self._emit as the event_emitter so zones can push to WebSocket
        self.zone_manager = ZoneManager(
            app_controller=self.app,
            mqtt_handler=mqtt_handler or self.mqtt,
            event_emitter=self._emit
        )

        # Hook RSSI listener to Zigpy application
        if hasattr(self, 'app'):
            setup_rssi_listener(self.app, self.zone_manager)
            logger.info("RSSI listener attached to Zigbee stack")

        # Load saved zones
        zones_path = Path("./data/zones.yaml")
        if zones_path.exists():
            try:
                with open(zones_path) as f:
                    zones_config = yaml.safe_load(f) or {}
                self.zone_manager.load_config(zones_config.get('zones', []))
                logger.info(f"Loaded {len(self.zone_manager.zones)} zones from config")
            except Exception as e:
                logger.error(f"Failed to load zones config: {e}")

        # Start background tasks
        await self.zone_manager.start_zone()

        # Publish Discovery
        for zone in self.zone_manager.zones.values():
            await self.zone_manager.publish_discovery(zone)

        logger.info("Zone manager initialised")

    async def stop(self):
        """Shutdown the Zigbee network."""
        self.polling_scheduler.stop()


        if self.zone_manager:
            await self.zone_manager.stop_zone()
            try:
                import yaml
                # Save to data directory
                configs = self.zone_manager.save_config()
                with open("./data/zones.yaml", "w") as f:
                    yaml.dump({'zones': configs}, f)
                logger.info("Saved zone configurations")
            except Exception as e:
                logger.error(f"Failed to save zones config: {e}")

        if self._save_task: self._save_task.cancel()
        if self._watchdog_task: self._watchdog_task.cancel()

        # Force one last save
        if self._cache_dirty:
            self._save_state_cache()

        if self.app:
            await self.app.shutdown()
            logger.info("Zigbee network stopped")

    # =========================================================================
    # REPUBLISH / BIRTH MESSAGE HANDLER
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
        # Ignore commands during startup grace period
        if not getattr(self, '_accepting_commands', True):
            logger.warning(f"Ignoring command during startup: {device_identifier} {data}")
            return

        ieee = self._resolve_device_identifier(device_identifier)
        if not ieee or ieee not in self.devices:
            logger.warning(f"MQTT command for unknown device: {device_identifier}")
            return

        device = self.devices[ieee]
        logger.info(f"[{ieee}] MQTT command: {data}")

        try:
            # Extract endpoint from object_id (e.g., "light_11" -> 11)
            endpoint = None
            if object_id:
                match = re.search(r'_(\d+)$', object_id)
                endpoint = int(match.group(1)) if match else None

            # Fallback: find first light endpoint if this is a light
            if endpoint is None and device.capabilities.has_capability('light'):
                for ep_id in device.zigpy_dev.endpoints:
                    if ep_id == 0:
                        continue
                    ep = device.zigpy_dev.endpoints[ep_id]
                    if 0x0008 in ep.in_clusters or 0x0006 in ep.in_clusters:
                        endpoint = ep_id
                        logger.debug(f"[{ieee}] Auto-detected light endpoint: {endpoint}")
                        break

            # Track state changes for optimistic update
            optimistic_state = {}

            # =========================================================================
            # JSON SCHEMA FORMAT - Process ALL attributes
            # =========================================================================
            state = data.get('state')
            brightness = data.get('brightness')
            color_temp = data.get('color_temp')
            color = data.get('color')

            # Handle State
            if state:
                cmd = 'on' if str(state).upper() == 'ON' else 'off'
                logger.info(f"[{ieee}] Executing state command: {cmd} EP={endpoint}")
                result = await device.send_command(cmd, endpoint_id=endpoint, data=data)
                if result:
                    optimistic_state['state'] = state.upper() if isinstance(state, str) else ('ON' if state else 'OFF')
                    optimistic_state['on'] = (cmd == 'on')

            # Handle Brightness (HA sends 0-254)
            if brightness is not None:
                pct = int(brightness / 2.54)
                logger.info(f"[{ieee}] Executing brightness command: {pct}% (raw={brightness}) EP={endpoint}")
                result = await device.send_command('brightness', pct, endpoint_id=endpoint)
                if result:
                    optimistic_state['brightness'] = int(brightness)
                    optimistic_state['level'] = pct
                    if brightness > 0:
                        optimistic_state['state'] = 'ON'
                        optimistic_state['on'] = True

            # Handle Color Temp (mireds)
            if color_temp is not None:
                try:
                    kelvin = int(1000000 / color_temp)
                    logger.info(f"[{ieee}] Executing color_temp command: {kelvin}K (mireds={color_temp}) EP={endpoint}")
                    result = await device.send_command('color_temp', kelvin, endpoint_id=endpoint)
                    if result:
                        optimistic_state['color_temp'] = int(color_temp)
                except ZeroDivisionError:
                    pass

            # Handle XY Color
            if color and 'x' in color and 'y' in color:
                logger.info(f"[{ieee}] Executing xy_color command: x={color['x']}, y={color['y']} EP={endpoint}")
                result = await device.send_command('xy_color', (color['x'], color['y']), endpoint_id=endpoint)
                if result:
                    optimistic_state['color'] = {'x': color['x'], 'y': color['y']}

            # =========================================================================
            # LEGACY FORMAT (fallback)
            # =========================================================================
            if not optimistic_state and 'command' in data:
                command = data['command'].lower()
                value = data.get('value')
                ep = data.get('endpoint', endpoint)

                logger.info(f"[{ieee}] Executing legacy command: {command}={value} EP={ep}")
                result = await device.send_command(command, value, endpoint_id=ep)
                if result:
                    if command == 'on':
                        optimistic_state['state'] = 'ON'
                        optimistic_state['on'] = True
                    elif command == 'off':
                        optimistic_state['state'] = 'OFF'
                        optimistic_state['on'] = False
                    elif command == 'brightness' and value is not None:
                        optimistic_state['brightness'] = int(value * 2.54) if value <= 100 else value
                        optimistic_state['level'] = value

            # =========================================================================
            # OPTIMISTIC STATE UPDATE
            # =========================================================================
            if optimistic_state:
                logger.info(f"[{ieee}] Optimistic update: {optimistic_state}")
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

        # CHECK BAN LIST FIRST
        if self.ban_manager.is_banned(ieee):
            logger.warning(f"🚫 BLOCKED: Banned device {ieee} attempted to join - sending leave request")
            self._emit_sync("log", {
                "level": "WARNING",
                "message": f"Blocked banned device: {ieee}",
                "ieee": ieee,
                "category": "security"
            })
            asyncio.create_task(self._kick_banned_device(device))
            return

        # Check for duplicates BEFORE logging
        if ieee in self.devices:
            logger.error(f"[{ieee}] Device ALREADY EXISTS! Duplicate join event - ignoring")
            return

        logger.info(f"Device joined: {ieee}")

        # Create device wrapper
        self.devices[ieee] = ZigManDevice(self, device)
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

        # Schedule handler re-identification after zigpy completes interview
        asyncio.create_task(self._delayed_handler_init(ieee))


    async def _delayed_handler_init(self, ieee: str):
        await asyncio.sleep(2)

        if ieee not in self.devices:
            return

        # Debug: Check zigpy's view of the device
        zigpy_dev = self.devices[ieee].zigpy_dev
        logger.info(f"[{ieee}] Zigpy status: is_initialized={zigpy_dev.is_initialized}, "
                    f"status={zigpy_dev.status}, endpoints={list(zigpy_dev.endpoints.keys())}")

        dev = self.devices[ieee]
        zigpy_dev = dev.zigpy_dev

        # Check if endpoints are now populated
        endpoint_count = len([ep for ep in zigpy_dev.endpoints.keys() if ep != 0])
        handler_count = len(dev.handlers)

        if endpoint_count > 0 and handler_count == 0:
            logger.info(f"[{ieee}] Re-running handler identification (found {endpoint_count} endpoints, 0 handlers)")
            dev._identify_handlers()
            dev.capabilities._detect_capabilities()

            # Announce to Home Assistant
            await self.announce_device(ieee)

            # Configure reporting
            await self._async_device_initialized(ieee)
        elif handler_count == 0:
            logger.warning(f"[{ieee}] No endpoints discovered after 2s delay")

    async def _kick_banned_device(self, device: zigpy.device.Device):
        """Send leave request to a banned device."""
        ieee = str(device.ieee)
        try:
            logger.info(f"[{ieee}] Sending leave request to banned device...")
            await device.zdo.leave()
            logger.info(f"[{ieee}] Leave request sent successfully")
        except Exception as e:
            logger.warning(f"[{ieee}] Leave request failed (device may have already left): {e}")

        # Also try to remove from zigpy's device list
        try:
            z_ieee = zigpy.types.EUI64.convert(ieee)
            if z_ieee in self.app.devices:
                await self.app.remove(z_ieee)
                logger.info(f"[{ieee}] Removed from zigpy device list")
        except Exception as e:
            logger.debug(f"[{ieee}] Could not remove from zigpy: {e}")

    # 4. Add API methods to ZigbeeService for managing the ban list:

    def ban_device(self, ieee: str, reason: str = None) -> dict:
        """Ban a device by IEEE address."""
        ieee = str(ieee).lower()
        success = self.ban_manager.ban(ieee, reason)

        # If device is currently connected, kick it
        if success and ieee in self.devices:
            device = self.devices[ieee]
            asyncio.create_task(self._kick_banned_device(device.zigpy_dev))

        return {
            "success": success,
            "ieee": ieee,
            "message": f"Device {ieee} has been banned" if success else f"Device {ieee} was already banned"
        }

    def unban_device(self, ieee: str) -> dict:
        """Remove a device from the ban list."""
        ieee = str(ieee).lower()
        success = self.ban_manager.unban(ieee)
        return {
            "success": success,
            "ieee": ieee,
            "message": f"Device {ieee} has been unbanned" if success else f"Device {ieee} was not banned"
        }

    def get_banned_devices(self) -> list:
        """Get list of all banned IEEE addresses."""
        return self.ban_manager.get_banned_list()

    def is_device_banned(self, ieee: str) -> bool:
        """Check if a device is banned."""
        return self.ban_manager.is_banned(ieee)

    def raw_device_initialized(self, device: zigpy.device.Device):
        """Called when device descriptors are read but endpoints not yet configured."""
        logger.debug(f"Raw device initialized: {device.ieee}")

    def device_initialized(self, device: zigpy.device.Device):
        """Called when a device is fully initialized (endpoints configured)."""
        ieee = str(device.ieee)
        logger.info(f"Device initialized: {ieee}")

        if ieee in self.devices:
            # Re-wrap with full endpoint information
            self.devices[ieee] = ZigManDevice(self, device)
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
            self.devices[ieee] = ZigManDevice(self, device)
            # Mark as seen
            self.devices[ieee].last_seen = int(time.time() * 1000)
            asyncio.create_task(self._async_device_initialized(ieee))

        self._rebuild_name_maps()
        self._emit_sync("device_initialized", {"ieee": ieee})


    def device_left(self, device: zigpy.device.Device):
        ieee = str(device.ieee)

        import traceback
        logger.warning(f"[{ieee}] Device left called, stack:\n{''.join(traceback.format_stack()[-5:])}")

        logger.info(f"Device left: {ieee}")

        if ieee in self.devices:
            # Mark offline
            self.devices[ieee].last_seen = self.devices[ieee].last_seen  # preserve last_seen
            self.devices[ieee]._available = False
            self.handle_device_update(self.devices[ieee], {"available": False})

        self.polling_scheduler.disable_for_device(ieee)

        name = self.friendly_names.get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Left - marked offline"
        self._emit_sync("log", {"level": "WARNING", "message": msg, "ieee": ieee, "device_name": name, "category": "connection"})


    def device_removed(self, device: zigpy.device.Device):
        """Called when a device is removed from the network."""
        ieee = str(device.ieee)
        logger.info(f"Device removed: {ieee}")

        if ieee in self.devices:
            self.devices[ieee].cleanup()
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

        # ---------------------------------------------------------------------
        # 1. DEBUGGER (PRIORITY 1) - Capture packet BEFORE any logic crashes
        # ---------------------------------------------------------------------
        try:
            debugger = get_debugger()
            # Capture if debugger exists AND is enabled
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
            # Never let the debugger crash the actual message handling
            logger.debug(f"Debug capture error: {e}")

        # ---------------------------------------------------------------------
        # 2. STATS & LOGGING
        # ---------------------------------------------------------------------
        try:
            packet_stats.record_rx(ieee, size=len(message) if message else 0)

            # Log for debugging (debug level to avoid spam)
            logger.debug(f"[{ieee}] Raw message: profile=0x{profile:04x}, cluster=0x{cluster:04x}, "
                         f"src_ep={src_ep}, dst_ep={dst_ep}, len={len(message)}")
        except Exception:
            pass

        # ---------------------------------------------------------------------
        # 3. FAST PATH (Time-critical)
        # ---------------------------------------------------------------------
        try:
            fast_pathed = self.fast_path.process_frame(
                ieee, profile, cluster, src_ep, dst_ep, message
            )
            if fast_pathed:
                logger.debug(f"[{ieee}] Fast-pathed: cluster=0x{cluster:04x}")
        except Exception as e:
            logger.debug(f"[{ieee}] Fast path error: {e}")

        # ---------------------------------------------------------------------
        # 4. ZONE MANAGER (Presence Detection)
        # ---------------------------------------------------------------------
        try:
            if hasattr(self, 'zone_manager') and self.zone_manager:
                # Get RSSI/LQI safely
                rssi = getattr(sender, 'rssi', None)
                lqi = getattr(sender, 'lqi', None)

                # Fallback: Zigpy stores last LQI on device object sometimes
                if lqi is None and hasattr(sender, 'last_seen'):
                    if hasattr(sender, '_application') and hasattr(sender._application, '_device'):
                        radio_dev = sender._application._device
                        lqi = getattr(radio_dev, 'lqi', None)

                if rssi is not None or lqi is not None:
                    # Ultra-robust Coordinator IEEE retrieval
                    coordinator_ieee = None

                    # Strategy 1: Check app.state (Modern Zigpy)
                    if hasattr(self.app, 'state'):
                        if hasattr(self.app.state, 'ieee') and self.app.state.ieee:
                            coordinator_ieee = str(self.app.state.ieee)
                        elif hasattr(self.app.state, 'node_info') and hasattr(self.app.state.node_info, 'ieee'):
                            coordinator_ieee = str(self.app.state.node_info.ieee)

                    # Strategy 2: Legacy/Fallback
                    if not coordinator_ieee and hasattr(self.app, 'ieee'):
                        coordinator_ieee = str(self.app.ieee)

                    # Only proceed if we successfully found the coordinator IEEE
                    if coordinator_ieee:
                        # Normalization
                        if rssi is None and lqi is not None:
                            rssi = int(-100 + (lqi / 255) * 70)
                        if lqi is None and rssi is not None:
                            lqi = int((rssi + 100) * 255 / 70)
                            lqi = max(0, min(255, lqi))

                        self.zone_manager.record_link_quality(
                            source_ieee=coordinator_ieee,
                            target_ieee=ieee,
                            rssi=rssi,
                            lqi=lqi
                        )
        except Exception as e:
            logger.error(f"[{ieee}] Zone manager error in handle_message: {e}")

        # ---------------------------------------------------------------------
        # 5. VENDOR SPECIFIC (Tuya, etc.)
        # ---------------------------------------------------------------------
        try:
            if cluster == 0xEF00 and ieee in self.devices:
                self.devices[ieee].handle_raw_message(cluster, message)
        except Exception as e:
            logger.error(f"[{ieee}] Vendor handler error: {e}")

    # =========================================================================
    # INTERNAL DEVICE MANAGEMENT
    # =========================================================================

    async def _async_device_restored(self, device: zigpy.device.Device):
        """Handle a device restored from database on startup."""
        ieee = str(device.ieee)
        self.devices[ieee] = ZigManDevice(self, device)

        # If this is the coordinator, mark it as seen immediately
        if self.devices[ieee].get_role() == "Coordinator":
            self.devices[ieee].last_seen = int(time.time() * 1000)

        # RESTORE STATE FROM CACHE (including last_seen for availability)
        if ieee in self.state_cache:
            logger.info(f"[{ieee}] Restoring cached state: {self.state_cache[ieee]}")
            self.devices[ieee].restore_state(self.state_cache[ieee])
        else:
            logger.warning(f"[{ieee}] NO cached state found!")


    async def _async_device_initialized(self, ieee: str):
        """Configure a newly initialized device."""
        if ieee not in self.devices:
            return

        try:
            zdev = self.devices[ieee]
            manufacturer = str(zdev.zigpy_dev.manufacturer or "").lower()
            model = str(zdev.zigpy_dev.model or "").lower()

            # === ADD: Philips Hue Motion Sensor Configuration ===
            if ("philips" in manufacturer or "signify" in manufacturer) and "sml" in model:
                logger.info(f"[{ieee}] Configuring Philips Hue Motion Sensor...")

                try:
                    ep2 = zdev.zigpy_dev.endpoints[2]

                    # 1. BIND coordinator for motion events
                    if 0x0006 in ep2.out_clusters:
                        await ep2.out_clusters[0x0006].bind()
                        logger.info(f"[{ieee}] Bound OnOff cluster for motion")

                    # 2. Configure illuminance reporting (change of 100 = ~1 lux)
                    if 0x0400 in ep2.in_clusters:
                        await ep2.in_clusters[0x0400].bind()
                        await ep2.in_clusters[0x0400].configure_reporting(
                            0x0000,  # measured_value
                            30,      # min 30s
                            300,     # max 5min
                            100      # change of 100 raw units (~1 lux)
                        )
                        logger.info(f"[{ieee}] Configured illuminance reporting")

                    # 3. Configure temperature reporting (change of 10 = 0.1°C)
                    if 0x0402 in ep2.in_clusters:
                        await ep2.in_clusters[0x0402].bind()
                        await ep2.in_clusters[0x0402].configure_reporting(
                            0x0000,  # measured_value
                            60,      # min 60s
                            3600,    # max 1hr
                            10       # change of 10 (0.1°C)
                        )
                        logger.info(f"[{ieee}] Configured temperature reporting")

                except Exception as e:
                    logger.warning(f"[{ieee}] Philips config failed: {e}")

            # Standard configuration
            await zdev.configure()
            logger.info(f"[{ieee}] Device configured successfully")

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

            # ==================================================================
            # RESTORE CACHED STATE
            # ==================================================================
            if ieee in self.state_cache:
                # Restore last known state from cache
                zdev.state.update(self.state_cache[ieee])
                logger.debug(f"[{ieee}] Restored cached state")

            configs = zdev.get_device_discovery_configs()

            # Use consistent safe name generation
            safe_name = self.get_safe_name(ieee)

            device_info = {
                "ieee": ieee,
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "safe_name": safe_name,
                "model": str(zdev.zigpy_dev.model),
                "manufacturer": str(zdev.zigpy_dev.manufacturer)
            }

            # Publish discovery configs
            await self.mqtt.publish_discovery(device_info, configs)
            logger.info(f"[{ieee}] Published HA discovery")

            # ==================================================================
            # PUBLISH INITIAL STATE FROM CACHE
            # ==================================================================
            try:
                from modules.json_helpers import sanitise_device_state

                # Build initial state from cached/current state
                initial_state = zdev.state.copy()

                # Mark as unavailable until device actually responds
                initial_state['available'] = False
                initial_state['lqi'] = 0

                # Sanitize for JSON serialization
                safe_state = sanitise_device_state(initial_state)

                # Remove numeric 'state' that conflicts with string state_N
                if 'state' in safe_state and isinstance(safe_state['state'], (int, bool, float)):
                    logger.warning(f"[{ieee}] Removing numeric 'state' value: {safe_state['state']}")
                    del safe_state['state']

                # If multi-endpoint device, ensure global state matches first endpoint
                if 'state_1' in safe_state and 'state' not in safe_state:
                    safe_state['state'] = safe_state['state_1']
                elif 'state_11' in safe_state and 'state' not in safe_state:
                    safe_state['state'] = safe_state['state_11']

                # Publish to device state topic with retain=True
                import json
                await self.mqtt.publish(
                    safe_name,
                    json.dumps(safe_state),
                    ieee=ieee,
                    qos=1,
                    retain=True
                )
                logger.info(f"[{ieee}] Published cached state: available=False, state={safe_state.get('state')}")

            except Exception as e:
                logger.error(f"[{ieee}] Failed to publish initial state: {e}")
                import traceback
                traceback.print_exc()

        except Exception as e:
            logger.error(f"[{ieee}] Failed to announce: {e}")
            import traceback
            traceback.print_exc()

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

        # 1. Announce Devices
        for ieee in list(self.devices.keys()):
            try:
                await self.announce_device(ieee)
                announced += 1
                await asyncio.sleep(0.1)  # Pace announcements to avoid MQTT flooding
            except Exception as e:
                logger.error(f"[{ieee}] Failed to announce: {e}")
                failed += 1

        # 2. Announce Groups
        if hasattr(self, 'group_manager'):
            logger.info("📢 Announcing Groups...")
            # This calls the method we added to groups.py
            await self.group_manager.announce_groups()

        logger.info(f"✅ Device & Group announcement complete: {announced} devices successful")

        # Grace period for HA to sync state before accepting commands
        logger.info("⏳ Startup grace period (20s) - ignoring commands...")
        await asyncio.sleep(20)
        self._accepting_commands = True
        logger.info("✅ Now accepting MQTT commands")

        await self._emit("log", {
            "level": "INFO",
            "message": f"Announced {announced} devices to Home Assistant",
            "ieee": None
        })

    async def find_duplicate_devices(self) -> dict:
        """Find devices that exist in database but not in active network."""
        import sqlite3

        # Use the database path from config or default
        db_path = "zigbee.db"  # Same path used in config

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Get table version
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'")
            devices_table = cursor.fetchone()
            if not devices_table:
                conn.close()
                return {"error": "Could not detect table version"}

            version = devices_table[0].split('_')[-1]

            # Get all devices from database
            cursor.execute(f"SELECT ieee FROM devices_{version}")
            db_devices = [row[0] for row in cursor.fetchall()]
            conn.close()

            # Get active devices from zigpy
            active_devices = [str(ieee).lower() for ieee in self.app.devices.keys()]

            # Normalize IEEE addresses for comparison
            db_devices_normalized = [ieee.lower() for ieee in db_devices]

            # Find orphaned devices (in DB but not active)
            orphaned = [ieee for ieee in db_devices_normalized if ieee not in active_devices]

            return {
                "total_in_db": len(db_devices),
                "active": len(active_devices),
                "orphaned": orphaned,
                "count": len(orphaned)
            }
        except Exception as e:
            logger.error(f"Failed to find duplicate devices: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    async def cleanup_orphaned_devices(self) -> dict:
        """Remove all orphaned devices from database."""
        result = await self.find_duplicate_devices()

        if "error" in result:
            return result

        orphaned = result["orphaned"]
        removed = []
        failed = []

        for ieee in orphaned:
            try:
                await self._force_cleanup_device_db(ieee)
                removed.append(ieee)
                logger.info(f"Removed orphaned device: {ieee}")
            except Exception as e:
                failed.append({"ieee": ieee, "error": str(e)})
                logger.error(f"Failed to remove {ieee}: {e}")

        return {
            "removed": removed,
            "failed": failed,
            "count_removed": len(removed),
            "count_failed": len(failed)
        }

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
        self._save_json("./data/polling_config.json", self.polling_config)

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

    async def touchlink_scan(self, channel: int = None):
        """Perform Touchlink scan for Light Link devices."""
        if self._touchlink is None:
            self._touchlink = await create_touchlink_manager(self.app)

        if self._touchlink is None:
            return {
                "success": False,
                "error": "Touchlink not supported by this coordinator",
                "note": "For Philips Hue: Use Hue Dimmer (hold ON+OFF 10s) or power cycle 5x"
            }

        return await self._touchlink.scan(channel)


    async def touchlink_identify(self, channel: int = None):
        """Scan for Touchlink devices and make them identify (blink)."""
        if self._touchlink is None:
            self._touchlink = await create_touchlink_manager(self.app)

        if self._touchlink is None:
            return {"success": False, "error": "Touchlink not supported"}

        return await self._touchlink.identify(channel)


    async def touchlink_factory_reset(self, channel: int = None):
        """Scan for Touchlink devices and factory reset them."""
        if self._touchlink is None:
            self._touchlink = await create_touchlink_manager(self.app)

        if self._touchlink is None:
            return {"success": False, "error": "Touchlink not supported"}

        return await self._touchlink.factory_reset(channel)

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
        ieee = str(ieee).lower()

        try:
            # 1. Get the zigpy device object (EUI64)
            z_ieee = zigpy.types.EUI64.convert(ieee)
            zdev = None

            if z_ieee in self.app.devices:
                zdev = self.app.devices[z_ieee]

            # 2. Try graceful leave if device is known and online-ish
            if not force and zdev:
                logger.info(f"[{ieee}] Sending Leave Request...")
                try:
                    async with asyncio.timeout(5.0):
                        await zdev.zdo.leave()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"[{ieee}] Leave request failed/timed out: {e}")

            # 3. FORCE REMOVE from Zigpy (Database)
            if zdev:
                await self.app.remove(z_ieee)
                logger.info(f"[{ieee}] Removed from zigpy application")

            # 3.5. Force database cleanup if requested
            if force:
                await self._force_cleanup_device_db(ieee)

            # 4. Cleanup Local State (In-Memory)
            if ieee in self.devices:
                del self.devices[ieee]

            # 5. Cleanup Persistent JSON Files
            if ieee in self.friendly_names:
                del self.friendly_names[ieee]
                self._save_json("./data/names.json", self.friendly_names)

            if ieee in self.device_settings:
                del self.device_settings[ieee]
                self._save_json("./data/device_settings.json", self.device_settings)

            # 6. Poll device
            if ieee in self.polling_config:
                del self.polling_config[ieee]
                self._save_json("./data/polling_config.json", self.polling_config)

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


    async def _force_cleanup_device_db(self, ieee: str):
        """Force cleanup device from all zigpy database tables."""
        import sqlite3

        db_path = "zigbee.db"

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Dynamically detect table version
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'")
            devices_table = cursor.fetchone()

            if not devices_table:
                logger.warning(f"[{ieee}] Could not detect zigpy table version")
                conn.close()
                return

            # Extract version suffix (e.g., "v13")
            version = devices_table[0].split('_')[-1]

            tables = [
                f'devices_{version}',
                f'endpoints_{version}',
                f'clusters_{version}',
                f'node_descriptors_{version}',
                f'attributes_cache_{version}',
                f'neighbors_{version}',
                f'routes_{version}',
                f'relays_{version}'
            ]

            logger.info(f"[{ieee}] Force cleaning database tables (version: {version})...")

            for table in tables:
                try:
                    cursor.execute(f"DELETE FROM {table} WHERE ieee=?", (ieee,))
                    deleted = cursor.rowcount
                    if deleted > 0:
                        logger.info(f"[{ieee}] Deleted {deleted} rows from {table}")
                except sqlite3.Error as e:
                    logger.debug(f"[{ieee}] Could not clean {table}: {e}")

            conn.commit()
            conn.close()
            logger.info(f"[{ieee}] Database cleanup complete")

        except Exception as e:
            logger.error(f"[{ieee}] Database cleanup failed: {e}")
    async def rename_device(self, ieee, name):
        """Rename a device."""
        self.friendly_names[ieee] = name
        self._save_json("./data/names.json", self.friendly_names)
        self._rebuild_name_maps()

        # Re-announce to HA with new name
        if self.mqtt and ieee in self.devices:
            await self.announce_device(ieee)

        return {"success": True}


    async def configure_device(self, ieee, config=None):
        """Reconfigure a device (bindings and reporting)."""
        if ieee in self.devices:
            try:
                await self.devices[ieee].configure(config)

                # Save settings if any meaningful config provided
                if config:
                    # Merge with existing settings
                    existing = self.device_settings.get(ieee, {})
                    existing.update({k: v for k, v in config.items() if v is not None and k != 'ieee'})

                    if existing:
                        self.device_settings[ieee] = existing
                        self._save_json("./data/device_settings.json", self.device_settings)

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


    def create_device_tab(self, tab_name: str) -> dict:
        """Create new device tab/category."""
        if tab_name in self.device_tabs:
            return {"success": False, "error": "Tab already exists"}

        self.device_tabs[tab_name] = []
        self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True, "tab_name": tab_name}

    def delete_device_tab(self, tab_name: str) -> dict:
        """Delete device tab."""
        if tab_name not in self.device_tabs:
            return {"success": False, "error": "Tab not found"}

        del self.device_tabs[tab_name]
        self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True}

    def add_device_to_tab(self, tab_name: str, ieee: str) -> dict:
        """Add device to tab."""
        if tab_name not in self.device_tabs:
            return {"success": False, "error": "Tab not found"}

        ieee = str(ieee).lower()
        if ieee not in self.device_tabs[tab_name]:
            self.device_tabs[tab_name].append(ieee)
            self._save_json("./data/device_tabs.json", self.device_tabs)

        return {"success": True}

    def remove_device_from_tab(self, tab_name: str, ieee: str) -> dict:
        """Remove device from tab."""
        if tab_name not in self.device_tabs:
            return {"success": False, "error": "Tab not found"}

        ieee = str(ieee).lower()
        if ieee in self.device_tabs[tab_name]:
            self.device_tabs[tab_name].remove(ieee)
            self._save_json("./data/device_tabs.json", self.device_tabs)

        return {"success": True}

    def get_device_tabs(self) -> dict:
        """Get all tabs."""
        return self.device_tabs

    # =========================================================================
    # TOPOLOGY & MESH (REAL DATA)
    # =========================================================================

    def get_simple_mesh(self):
        """Get network topology for mesh visualization with packet statistics."""
        from modules.packet_stats import packet_stats

        nodes = []
        connections = []
        device_stats = packet_stats.get_all_stats()

        # 1. Build Nodes with stats
        for ieee, zdev in self.devices.items():
            d = zdev.zigpy_dev
            stats = device_stats.get(ieee, {})

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
                "polling_interval": self.polling_scheduler.get_interval(ieee),
                # Packet statistics
                "packet_stats": {
                    "rx_packets": stats.get("rx_packets", 0),
                    "tx_packets": stats.get("tx_packets", 0),
                    "total_packets": stats.get("total_packets", 0),
                    "rx_rate": stats.get("rx_rate_per_min", 0),
                    "tx_rate": stats.get("tx_rate_per_min", 0),
                    "errors": stats.get("errors", 0),
                    "error_rate": stats.get("error_rate", 0)
                }
            })

        # 2. Build Links from Zigpy Topology
        if hasattr(self.app, 'topology') and self.app.topology.neighbors:
            for src_ieee, neighbors in self.app.topology.neighbors.items():
                src_str = str(src_ieee)
                for neighbor in neighbors:
                    dst_str = str(neighbor.ieee)
                    if src_str in self.devices and dst_str in self.devices:
                        connections.append({
                            "source": src_str,
                            "target": dst_str,
                            "lqi": neighbor.lqi or 0,
                            "relationship": getattr(neighbor, 'relationship', 'Unknown')
                        })

        # 3. Build connection table data
        connection_table = self._build_connection_table()

        return {
            "nodes": nodes,
            "links": connections,
            "connection_table": connection_table,
            "stats_summary": packet_stats.get_summary()
        }


    def _build_connection_table(self):
        """Build textual connection table from topology."""
        table = []

        # Get coordinator IEEE
        coord_ieee = str(self.app.ieee) if hasattr(self.app, 'ieee') else None

        if hasattr(self.app, 'topology') and self.app.topology.neighbors:
            # Build parent-child relationships
            for src_ieee, neighbors in self.app.topology.neighbors.items():
                src_str = str(src_ieee)
                src_name = self.friendly_names.get(src_str, src_str[-8:])

                for neighbor in neighbors:
                    dst_str = str(neighbor.ieee)
                    if dst_str not in self.devices:
                        continue

                    dst_name = self.friendly_names.get(dst_str, dst_str[-8:])
                    relationship = getattr(neighbor, 'relationship', 0)

                    # Relationship types from Zigbee spec
                    rel_str = {
                        0: "Parent",
                        1: "Child",
                        2: "Sibling",
                        3: "None",
                        4: "Previous Child"
                    }.get(relationship, f"Unknown({relationship})")

                    # Determine device types
                    src_dev = self.devices.get(src_str)
                    dst_dev = self.devices.get(dst_str)

                    src_role = src_dev.get_role() if src_dev else "Unknown"
                    dst_role = dst_dev.get_role() if dst_dev else "Unknown"

                    table.append({
                        "source_ieee": src_str,
                        "source_name": src_name,
                        "source_role": src_role,
                        "target_ieee": dst_str,
                        "target_name": dst_name,
                        "target_role": dst_role,
                        "relationship": rel_str,
                        "lqi": neighbor.lqi or 0,
                        "depth": getattr(neighbor, 'depth', None)
                    })

        # Sort by source name, then target
        table.sort(key=lambda x: (x["source_name"], x["target_name"]))

        return table

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
        """Called by ZigManDevice when state changes."""
        ieee = zha_device.ieee

        # Cancel any pending debounced update for this device
        if ieee in self._update_debounce_tasks:
            self._update_debounce_tasks[ieee].cancel()

        # Schedule debounced update
        self._update_debounce_tasks[ieee] = asyncio.create_task(
            self._debounced_device_update(zha_device, changed_data, full_state, qos, endpoint_id)
        )

    async def _debounced_device_update(self, zha_device, changed_data, full_state, qos, endpoint_id):
        """Actual update logic with debounce."""
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

        ieee = zha_device.ieee

        # Build MQTT payload from changed data only
        if full_state is None:
            mqtt_payload = changed_data.copy()
            mqtt_payload['available'] = zha_device.is_available()
            mqtt_payload['lqi'] = getattr(zha_device.zigpy_dev, 'lqi', 0) or 0

            # FOR MULTI-ENDPOINT: Include ALL endpoint states from cache
            if ieee in self.state_cache:
                cached = self.state_cache[ieee]
                # Add all state_N and on_N fields from cache
                for key in cached:
                    if (key.startswith('state_') or key.startswith('on_')) and key not in mqtt_payload:
                        mqtt_payload[key] = cached[key]
        else:
            mqtt_payload = full_state.copy()
            mqtt_payload['available'] = zha_device.is_available()
            mqtt_payload['lqi'] = getattr(zha_device.zigpy_dev, 'lqi', 0) or 0

        from modules.json_helpers import sanitise_device_state
        safe_mqtt_payload = sanitise_device_state(mqtt_payload)

        # Normalise contact sensors for MQTT
        device_caps = zha_device.capabilities
        if device_caps.has_capability('contact_sensor'):
            for key in list(safe_mqtt_payload.keys()):
                if key == 'contact' or key.startswith('contact_'):
                    raw = safe_mqtt_payload.get(key)
                    if isinstance(raw, bool):
                        ha_val = not raw
                        safe_mqtt_payload[key] = ha_val
                        ep = key.split('_', 1)[1] if '_' in key else None
                        open_key = f"is_open_{ep}" if ep else "is_open"
                        closed_key = f"is_closed_{ep}" if ep else "is_closed"
                        safe_mqtt_payload[open_key] = ha_val
                        safe_mqtt_payload[closed_key] = not ha_val

        # Remove internal keys
        keys_to_remove = [k for k in list(safe_mqtt_payload.keys())
                          if k.endswith('_raw') or k.startswith('attr_')]

        if not device_caps.has_capability('motion_sensor'):
            keys_to_remove.extend(['occupancy', 'motion', 'presence'])

        for key in keys_to_remove:
            safe_mqtt_payload.pop(key, None)

        # Fix multi-endpoint state
        endpoint_state_keys = [k for k in safe_mqtt_payload.keys() if k.startswith('state_') and k[6:].isdigit()]
        if endpoint_state_keys and endpoint_id is not None:
            endpoint_state_key = f"state_{endpoint_id}"
            if endpoint_state_key in safe_mqtt_payload:
                safe_mqtt_payload['state'] = safe_mqtt_payload[endpoint_state_key]
                safe_mqtt_payload['on'] = safe_mqtt_payload.get(f"on_{endpoint_id}", False)

        if 'state' in safe_mqtt_payload and isinstance(safe_mqtt_payload['state'], (int, float)):
            del safe_mqtt_payload['state']
            if endpoint_state_keys:
                first_ep_key = sorted(endpoint_state_keys)[0]
                safe_mqtt_payload['state'] = safe_mqtt_payload[first_ep_key]

        # UPDATE CACHE (with full device state)
        if ieee not in self.state_cache:
            self.state_cache[ieee] = {}

        # Merge changed data into cache
        cache_update = changed_data.copy()
        cache_update['available'] = zha_device.is_available()
        cache_update['lqi'] = getattr(zha_device.zigpy_dev, 'lqi', 0) or 0

        self.state_cache[ieee].update(sanitise_device_state(cache_update))
        self._cache_dirty = True

        # Emit to WebSocket (only changed data)
        self._emit_sync("device_updated", {"ieee": ieee, "data": safe_mqtt_payload})

        # PUBLISH TO MQTT (only changed attributes)
        if self.mqtt:
            import json
            safe_name = self.get_safe_name(ieee)
            mqtt_qos = qos

            asyncio.create_task(
                self.mqtt.publish(
                    safe_name,
                    json.dumps(safe_mqtt_payload),
                    ieee=ieee,
                    qos=mqtt_qos,
                    retain=True
                )
            )

        # Log changed attributes
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
                from modules.json_helpers import prepare_for_json
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

                # Determine component type for this endpoint
                component_type = None

                # Key cluster checks
                has_onoff_input = 0x0006 in ep.in_clusters
                has_onoff_output = 0x0006 in ep.out_clusters
                has_level = 0x0008 in ep.in_clusters
                has_color = 0x0300 in ep.in_clusters
                has_lightlink = 0x1000 in ep.in_clusters
                has_electrical = 0x0B04 in ep.in_clusters
                has_multi_state = (0x0012 in ep.in_clusters or 0x0012 in ep.out_clusters or
                                   0x0013 in ep.in_clusters or 0x0013 in ep.out_clusters or
                                   0x0014 in ep.in_clusters or 0x0014 in ep.out_clusters)
                has_power_config = 0x0001 in ep.in_clusters  # Battery powered
                has_ias_zone = 0x0500 in ep.in_clusters
                has_occupancy = 0x0406 in ep.in_clusters

                # Sensors/Buttons: Battery + OnOff in outputs OR MultistateInput OR IAS/Occupancy
                if has_ias_zone or has_occupancy:
                    component_type = "sensor"
                elif has_power_config and has_onoff_output and not has_onoff_input:
                    component_type = "sensor"  # Door/window sensor
                # Buttons (multistate + no actuator clusters + no OnOff INPUT)
                elif has_multi_state and not has_onoff_input and not (has_level or has_color or has_electrical):
                    component_type = "sensor"  # Button/remote
                elif has_onoff_input:
                    # Real OnOff actuator - determine if light or switch
                    # Force switch if electrical + level OR multistate present
                    if (has_electrical and has_level or has_multi_state) and not (has_color or has_lightlink):
                        component_type = "switch"
                    elif has_lightlink or has_color or has_level:
                        component_type = "light"
                    else:
                        component_type = "switch"
                elif 0x0102 in ep.in_clusters:
                    component_type = "cover"
                elif 0x0201 in ep.in_clusters:
                    component_type = "thermostat"

                caps.append({
                    "id": ep_id,
                    "profile": f"0x{ep.profile_id:04x}" if ep.profile_id else "0x0000",
                    "inputs": [{"id": c.cluster_id, "name": c.name} for c in ep.in_clusters.values()],
                    "outputs": [{"id": c.cluster_id, "name": c.name} for c in ep.out_clusters.values()],
                    "component_type": component_type
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

        # Sanitise the entire result to be JSON-safe
        return prepare_for_json(res)