"""
Zigbee Matter Service Core - ZHA-inspired architecture.
ZigbeeService is composed from focused mixin classes.

Mixins:
  ConfigBuilderMixin  - Radio config builders (EZSP/ZNP)
  MQTTHandlerMixin    - Announcements, republish, bridge status
  TopologyMixin       - Mesh data, LQI scanning, connection table
  BanningMixin        - Ban/unban/kick
  DatabaseMixin       - Orphan detection, DB cleanup
  TabsMixin           - Device tab CRUD

This file retains: __init__, start(), stop(), zigpy listener interface,
handle_device_update, handle_mqtt_command, announce_device, send_command,
rename_device, configure_device, interview_device, poll_device, bind_devices,
permit_join, touchlink, get_device_list, and utility methods.
"""
import asyncio
import logging
import json
import time
import os
import re
import traceback
from typing import Dict, Any, Optional
from contextlib import suppress

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

import zigpy.zdo.types as zdo_types
from zigpy.zcl.clusters.security import IasZone

from device import ZigManDevice
from modules.json_helpers import prepare_for_json, sanitise_device_state
from modules.packet_stats import packet_stats
from modules.zones import ZoneManager
from handlers.zones_handler import setup_rssi_listener
from modules.zigbee_debug import get_debugger
from handlers.fast_path import FastPathProcessor
from modules.device_ban import get_ban_manager
from modules.touchlink import create_touchlink_manager, TouchlinkManager
from modules.automation import AutomationEngine
from modules.ota import OTAManager, build_ota_config
from modules.multipan import MultiPanManager

# Import mixins
from core.config_builder import ConfigBuilderMixin
from core.mqtt_handler import MQTTHandlerMixin
from core.topology import TopologyMixin
from core.banning import BanningMixin
from core.database import DatabaseMixin
from core.tabs import TabsMixin
from core.polling import PollingScheduler

logger = logging.getLogger("core")

# Try Loading Quirks
try:
    import zhaquirks
    try:
        import zhaquirks.centralite
        logger.info("Loaded Centralite/Hive quirks")
    except ImportError:
        pass
    zhaquirks.setup()
    logger.info("ZHA Quirks loaded successfully")
except Exception as e:
    logging.warning(f"Failed to load ZHA Quirks: {e}")


class ZigbeeService(
    ConfigBuilderMixin,
    MQTTHandlerMixin,
    TopologyMixin,
    BanningMixin,
    DatabaseMixin,
    TabsMixin,
):
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
            self.mqtt.ha_status_callback = self.republish_all_devices
            self.mqtt.status_change_callback = self.handle_bridge_status_change

        self.event_callback = event_callback or self._default_event_callback

        self.devices: Dict[str, ZigManDevice] = {}
        self.friendly_names = self._load_json("./data/names.json")
        self.device_settings = self._load_json("./data/device_settings.json")

        # Device override manager
        from modules.device_overrides import get_override_manager
        self.override_manager = get_override_manager()

        self.polling_config = self._load_json("./data/polling_config.json")

        # State cache
        self.state_cache = self._load_json("./data/device_state_cache.json")
        self._cache_dirty = False
        self._save_task = None
        self._debounce_seconds = 2.0

        self.join_history = []
        self._config = config
        self.multipan: Optional['MultiPanManager'] = None

        # Pairing state
        self.pairing_expiration = 0
        self._permit_join_via = None  # IEEE of device used for targeted permit join

        # Banning
        self.ban_manager = get_ban_manager()

        # Polling scheduler
        self.polling_scheduler = PollingScheduler(self)

        # Background tasks
        self._save_task = None
        self._watchdog_task = None
        self._announce_task = None
        self._zones_init_task = None
        self._scan_task = None
        self._scan_in_progress = False
        self._scan_last_completed = None

        # IEEE lookup by name (for MQTT command routing)
        self._name_to_ieee: Dict[str, str] = {}
        self._node_id_to_ieee: Dict[str, str] = {}

        # Fast-path processor
        self.fast_path = FastPathProcessor(self)
        logger.info("Fast-path processor initialised")

        # Group Manager
        from modules.groups import GroupManager
        self.group_manager = GroupManager(self)

        # Zone Manager
        self.zone_manager = None

        # Connect group command callback
        if self.mqtt:
            self.mqtt.group_command_callback = self.group_manager.handle_mqtt_group_command

        self._accepting_commands = False

        # Touchlink
        self._touchlink: Optional[TouchlinkManager] = None

        # Tabs
        self.device_tabs = self._load_json("./data/device_tabs.json") or {}

        # Automation engine
        self.automation = AutomationEngine(
            device_registry_getter=lambda: self.devices,
            friendly_names_getter=lambda: self.friendly_names,
            event_emitter=self.callback,
            group_manager_getter=lambda: getattr(self, 'group_manager', None),
            matter_device_getter=lambda: self._get_matter_devices(),
        )

        # OTA firmware update manager
        self.ota_manager = None

        os.makedirs("logs", exist_ok=True)

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    async def _default_event_callback(self, event_type: str, data: dict):
        pass

    def _get_matter_devices(self):
        """Return Matter device registry for automation engine merged view."""
        try:
            from main import get_matter_bridge
            mb = get_matter_bridge()
            return mb.devices if mb else {}
        except Exception:
            return {}

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
        try:
            safe_data = prepare_for_json(data)
            with open(f, 'w') as file:
                json.dump(safe_data, file, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {f}: {e}")

    def _save_state_cache(self):
        self._save_json("./data/device_state_cache.json", self.state_cache)

    async def _debounced_save(self):
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
                logger.warning(f"Event loop blocked for {duration:.2f}s (should be ~1.0s)")

    def get_safe_name(self, ieee):
        name = self.friendly_names.get(ieee, ieee)
        return re.sub(r'[+#/]', '-', name)

    def _rebuild_name_maps(self):
        self._name_to_ieee.clear()
        self._node_id_to_ieee.clear()
        for ieee in self.devices:
            safe_name = self.get_safe_name(ieee)
            self._name_to_ieee[safe_name] = ieee
            self._name_to_ieee[safe_name.lower()] = ieee
            node_id = ieee.replace(":", "")
            self._node_id_to_ieee[node_id] = ieee
            self._node_id_to_ieee[node_id.lower()] = ieee

    def _resolve_device_identifier(self, identifier: str) -> Optional[str]:
        """Resolve a device name/node_id/ieee to an IEEE address."""
        if identifier in self.devices:
            return identifier
        if identifier in self._name_to_ieee:
            return self._name_to_ieee[identifier]
        if identifier in self._node_id_to_ieee:
            return self._node_id_to_ieee[identifier]
        lower_id = identifier.lower()
        if lower_id in self._name_to_ieee:
            return self._name_to_ieee[lower_id]
        if lower_id in self._node_id_to_ieee:
            return self._node_id_to_ieee[lower_id]
        for name, ieee in self._name_to_ieee.items():
            if lower_id in name.lower():
                return ieee
        return None

    # =========================================================================
    # EVENT EMISSION
    # =========================================================================

    def _emit_sync(self, evt, data):
        if self.callback:
            asyncio.create_task(self.callback(evt, data))

    async def _emit(self, evt, data):
        if self.callback:
            await self.callback(evt, data)

    # =========================================================================
    # START / STOP
    # =========================================================================

    async def start(self, network_key=None, probe_progress_cb=None):
        """Start the Zigbee network with enhanced resilience."""
        # Backwards compatibility migration
        if 'ezsp_config' in self._config and 'ezsp' not in self._config:
            logger.info("Migrating old EZSP config format...")
            self._config['ezsp'] = {
                'baudrate': self._config.get('baudrate', 460800),
                'flow_control': self._config.get('flow_control', 'hardware'),
                'config': self._config['ezsp_config']
            }

        # Probe radio type + serial parameters (protocol-level handshake)
        probe_result = await self._probe_radio_type(progress_cb=probe_progress_cb)

        # ================================================================
        # MULTIPAN INTERCEPT — if Jedi detected RCP firmware, start the
        # CPC daemon stack and redirect bellows to the zigbeed socket.
        # ================================================================
        adapter_family = probe_result.get("adapter_family", "")
        if adapter_family == "Silicon Labs CPC Multi-PAN (RCP)":
            logger.info(
                "MultiPAN RCP firmware detected — starting CPC stack..."
            )
            logger.debug(f"MultiPAN probe_result: {probe_result}")
            try:
                from modules.multipan import MultiPanManager

                multipan_config = self._config.get("multipan", {})
                self.multipan = MultiPanManager(
                    zigbee_config=self._config,
                    multipan_config=multipan_config,
                    event_emitter=self._emit,
                )

                if await self.multipan.start(serial_port=self.port, jedi_result=probe_result):
                    # Override port — bellows will connect to zigbeed's socket
                    # instead of the serial port (which cpcd now owns)
                    original_port = self.port
                    self.port = self.multipan.ezsp_socket
                    logger.info(
                        f"MultiPAN active: {original_port} → {self.port}"
                    )

                    # Re-probe: socket path → returns EZSP with no serial params
                    probe_result = await self._probe_radio_type()
                else:
                    logger.error(
                        "MultiPAN stack failed to start — "
                        "falling back to direct serial (may fail if "
                        "dongle has RCP firmware)"
                    )
                    await self.multipan.stop()
                    self.multipan = None

            except ImportError:
                logger.warning(
                    "multipan module not available — "
                    "cannot use MultiPAN RCP firmware. "
                    "Install cpcd/zigbeed or flash NCP firmware."
                )
            except Exception as e:
                logger.error(f"MultiPAN startup error: {e}")
                self.multipan = None


        radio_type = probe_result["radio_type"]
        logger.info(
            f"✅ Detected radio: {radio_type} @ "
            f"{probe_result.get('baudrate', '?')} baud / "
            f"{probe_result.get('flow_control', '?')} flow"
        )

        # Import correct driver
        if radio_type == "EZSP":
            from bellows.zigbee.application import ControllerApplication
        elif radio_type == "ZNP":
            from zigpy_znp.zigbee.application import ControllerApplication
        elif radio_type == "DECONZ":
            from zigpy_deconz.zigbee.application import ControllerApplication
        else:
            raise RuntimeError(f"Unsupported radio type: {radio_type}")

        # Build config using detected serial parameters
        if radio_type == "EZSP":
            ezsp_conf = {}
            user_ezsp = self._config.get('ezsp', {}).get('config', {})
            for key, val in user_ezsp.items():
                if key.startswith('CONFIG_'):
                    ezsp_conf[key] = val
                    logger.info(f"User override: {key} = {val}")
            conf = self._build_ezsp_config(ezsp_conf, network_key, detected=probe_result)
        elif radio_type == "ZNP":
            conf = self._build_znp_config(network_key, detected=probe_result)
        elif radio_type == "DECONZ":
            conf = self._build_deconz_config(network_key, detected=probe_result)

        # Robust startup with retries
        for attempt in range(12):
            try:
                # MultiPAN: zigbeed doesn't support EZSP readCounters.
                # The watchdog is spawned INSIDE ControllerApplication.new()
                # during startup(), so cancelling it after new() returns is
                # a race — the first watchdog tick can fire before we cancel.
                # Fix: monkey-patch _watchdog_feed to a no-op BEFORE new().
                _original_watchdog_feed = None
                if self.multipan and self.multipan.is_running:
                    _original_watchdog_feed = getattr(
                        ControllerApplication, '_watchdog_feed', None
                    )

                    async def _noop_watchdog_feed(self_app):
                        pass

                    ControllerApplication._watchdog_feed = _noop_watchdog_feed
                    logger.info(
                        "MultiPAN: patched _watchdog_feed to no-op "
                        "(zigbeed does not support readCounters)"
                    )

                self.app = await ControllerApplication.new(
                    config=conf, auto_form=True, start_radio=True
                )

                # MultiPAN: also cancel the watchdog task and restore the
                # original method (in case it's needed for non-MultiPAN later)
                if self.multipan and self.multipan.is_running:
                    if hasattr(self.app, '_watchdog_task') and self.app._watchdog_task:
                        self.app._watchdog_task.cancel()
                    if hasattr(self.app, '_watchdog_monitor'):
                        self.app._watchdog_monitor.stop()
                    if _original_watchdog_feed is not None:
                        ControllerApplication._watchdog_feed = _original_watchdog_feed
                    logger.info("Disabled EZSP watchdog (MultiPAN/zigbeed mode)")

                self._touchlink = await create_touchlink_manager(self.app)
                if self._touchlink:
                    logger.info(f"✅ Touchlink support enabled ({self._touchlink.coordinator_type})")

                # Wrap with resilience (EZSP only)
                if radio_type == "EZSP":
                    if hasattr(self, 'resilience') and self.resilience:
                        if hasattr(self.app, '_watchdog_monitor'):
                            self.app._watchdog_monitor.stop()
                    try:
                        from modules.resilience import wrap_with_resilience
                        self.resilience = wrap_with_resilience(self.app, self._emit)
                        logger.info("✅ Resilience system enabled")
                    except Exception as e:
                        logger.warning(f"Resilience not available: {e}")

                # Register as listener
                self.app.add_listener(self)

                # Restore devices from zigpy's database
                for ieee, device in self.app.devices.items():
                    ieee_str = str(ieee)
                    if self.ban_manager.is_banned(ieee_str):
                        logger.info(f"Skipping banned device: {ieee_str}")
                        continue
                    self.devices[ieee_str] = ZigManDevice(self, device)
                    if ieee_str in self.state_cache:
                        self.devices[ieee_str].restore_state(self.state_cache[ieee_str])

                self._rebuild_name_maps()
                logger.info(f"Restored {len(self.devices)} devices from database")

                # OTA manager
                if self.app:
                    self.ota_manager = OTAManager(self, self._emit)
                    logger.info("OTA Manager initialised")

                # Start polling
                self.polling_scheduler.start()
                self._watchdog_task = asyncio.create_task(
                    self.polling_scheduler._availability_watchdog_loop()
                )

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


                # Reset resilience watchdog after successful start
                if hasattr(self, 'resilience') and self.resilience:
                    self.resilience.last_watchdog_feed = time.time()
                    self.resilience.update_state("connected", "startup_complete")
                    logger.info("Resilience watchdog reset after successful startup")

                # Announce all devices to HA
                self._announce_task = asyncio.create_task(self.announce_all_devices())

                # Initialise zones
                self._zones_init_task = asyncio.create_task(self._init_zones_internal())

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
        try:
            await asyncio.sleep(2)
            await self.init_zones()
        except Exception as e:
            logger.error(f"Failed to initialise zones: {e}")

    async def init_zones(self, mqtt_handler=None):
        """Initialise zone manager after Zigbee network is started."""
        import yaml
        self.zone_manager = ZoneManager(
            app_controller=self.app,
            mqtt_handler=mqtt_handler or self.mqtt,
            event_emitter=self._emit
        )
        if hasattr(self, 'app'):
            setup_rssi_listener(self.app, self.zone_manager)
            logger.info("RSSI listener attached to Zigbee stack")

        zones_path = Path("./data/zones.yaml")
        if zones_path.exists():
            try:
                with open(zones_path) as f:
                    zones_config = yaml.safe_load(f) or {}
                self.zone_manager.load_config(zones_config.get('zones', []))
                logger.info(f"Loaded {len(self.zone_manager.zones)} zones from config")
            except Exception as e:
                logger.error(f"Failed to load zones config: {e}")

        await self.zone_manager.start_zone()
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
                configs = self.zone_manager.save_config()
                with open("./data/zones.yaml", "w") as f:
                    yaml.dump({'zones': configs}, f)
                logger.info("Saved zone configurations")
            except Exception as e:
                logger.error(f"Failed to save zones config: {e}")

        for task in [self._save_task, self._watchdog_task, self._announce_task,
                     self._zones_init_task, self._scan_task]:
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        if self.ota_manager:
            self.ota_manager.stop_background_checks()

        if self._cache_dirty:
            self._save_state_cache()

        if self.app:
            await self.app.shutdown()
            logger.info("Zigbee network stopped")

        # Stop MultiPAN stack AFTER bellows has disconnected
        # (reverse order: otbr → socat → zigbeed → cpcd)
        if self.multipan and self.multipan.is_running:
            await self.multipan.stop()
            self.multipan = None

    # =========================================================================
    # ZIGPY LISTENER INTERFACE
    # =========================================================================

    def raw_device_initialized(self, device: zigpy.device.Device):
        logger.debug(f"Raw device initialized: {device.ieee}")

    def device_joined(self, device: zigpy.device.Device):
        """Called when a device joins the network."""
        ieee = str(device.ieee)

        if self.ban_manager.is_banned(ieee):
            logger.warning(f"🚫 BLOCKED: Banned device {ieee} attempted to join")
            self._emit_sync("log", {
                "level": "WARNING", "message": f"Blocked banned device: {ieee}",
                "ieee": ieee, "category": "security"
            })
            asyncio.create_task(self._kick_banned_device(device))
            return

        if ieee in self.devices:
            logger.error(f"[{ieee}] Duplicate join event - ignoring")
            return

        logger.info(f"Device joined: {ieee}")
        self.devices[ieee] = ZigManDevice(self, device)
        self.devices[ieee].last_seen = int(time.time() * 1000)

        self.join_history.insert(0, {
            "join_timestamp": time.time() * 1000,
            "ieee_address": ieee,
            "manufacturer": str(device.manufacturer) if device.manufacturer else "Unknown",
            "model": str(device.model) if device.model else "Unknown",
        })

        self._rebuild_name_maps()

        name = self.friendly_names.get(ieee, "Unknown")
        self._emit_sync("log", {"level": "INFO", "message": f"[{ieee}] ({name}) Device Joined",
                                "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_joined", {"ieee": ieee})
        asyncio.create_task(self._delayed_handler_init(ieee))

    async def _delayed_handler_init(self, ieee: str):
        """
        Wait for zigpy to finish interviewing the device, then build handlers.
        """
        # Poll config: check every 3s for up to 180s (60 attempts)
        POLL_INTERVAL = 3.0
        MAX_WAIT_SECONDS = 180
        attempts = int(MAX_WAIT_SECONDS / POLL_INTERVAL)

        # Initial short wait so the node descriptor reply has a chance to land
        await asyncio.sleep(2)

        for attempt in range(attempts):
            if ieee not in self.devices:
                return

            dev = self.devices[ieee]
            zigpy_dev = dev.zigpy_dev

            endpoint_count = len([ep for ep in zigpy_dev.endpoints.keys() if ep != 0])
            handler_count = len(dev.handlers)

            # Success path: endpoints discovered and handlers not yet built
            if endpoint_count > 0 and handler_count == 0:
                logger.info(
                    f"[{ieee}] Endpoints discovered after ~{2 + attempt * POLL_INTERVAL:.0f}s "
                    f"({endpoint_count} endpoints) - building handlers"
                )
                logger.info(
                    f"[{ieee}] Zigpy status: is_initialized={zigpy_dev.is_initialized}, "
                    f"status={zigpy_dev.status}, endpoints={list(zigpy_dev.endpoints.keys())}"
                )
                dev._identify_handlers()
                dev.capabilities._detect_capabilities()
                await self.announce_device(ieee)
                await self._async_device_initialized(ieee)
                return

            # Already fully initialised by device_initialized() path - nothing to do
            if handler_count > 0:
                logger.debug(f"[{ieee}] Handlers already built ({handler_count}) - nothing to do")
                return

            # Log progress every ~30s so it's visible what we're waiting for
            if attempt > 0 and attempt % 10 == 0:
                logger.info(
                    f"[{ieee}] Still waiting for endpoint discovery "
                    f"(attempt {attempt}/{attempts}, "
                    f"is_initialized={zigpy_dev.is_initialized}, "
                    f"endpoints={list(zigpy_dev.endpoints.keys())})"
                )

            await asyncio.sleep(POLL_INTERVAL)

        # Timeout - log final state so we know where it got stuck
        if ieee in self.devices:
            dev = self.devices[ieee]
            zigpy_dev = dev.zigpy_dev
            logger.warning(
                f"[{ieee}] No endpoints discovered after {MAX_WAIT_SECONDS}s "
                f"(is_initialized={zigpy_dev.is_initialized}, "
                f"status={zigpy_dev.status}, "
                f"endpoints={list(zigpy_dev.endpoints.keys())}). "
                f"Device may be sleeping - handlers will be built when zigpy "
                f"fires device_initialized."
            )

    def device_initialized(self, device: zigpy.device.Device):
        """Called when a device is fully initialised."""
        ieee = str(device.ieee)
        logger.info(f"Device initialised: {ieee}")

        if ieee in self.devices:
            self.devices[ieee] = ZigManDevice(self, device)
            self.devices[ieee].last_seen = int(time.time() * 1000)
            if ieee in self.state_cache:
                logger.info(f"[{ieee}] Restoring state from cache")
                self.devices[ieee].restore_state(self.state_cache[ieee])
        else:
            self.devices[ieee] = ZigManDevice(self, device)
            self.devices[ieee].last_seen = int(time.time() * 1000)

        # --- Auto-pair Hive thermostat ↔ receiver ---
        # Model is now known at this point (unlike device_joined where it's None)
        if self._permit_join_via:
            new_model = str(device.model or "").upper()
            via_ieee = self._permit_join_via

            if via_ieee in self.devices:
                via_model = str(self.devices[via_ieee].zigpy_dev.model or "").upper()

                # SLT thermostat joined via SLR receiver
                if "SLT" in new_model and ("SLR" in via_model or "RECEIVER" in via_model):
                    self.device_settings.setdefault(via_ieee, {})["paired_thermostat"] = ieee
                    self.device_settings.setdefault(ieee, {})["paired_receiver"] = via_ieee
                    self._save_json("./data/device_settings.json", self.device_settings)
                    logger.info(
                        f"🔗 Auto-paired thermostat [{ieee}] ↔ receiver [{via_ieee}]"
                    )

                # SLR receiver joined via SLT thermostat (reverse)
                elif ("SLR" in new_model or "RECEIVER" in new_model) and "SLT" in via_model:
                    self.device_settings.setdefault(ieee, {})["paired_thermostat"] = via_ieee
                    self.device_settings.setdefault(via_ieee, {})["paired_receiver"] = ieee
                    self._save_json("./data/device_settings.json", self.device_settings)
                    logger.info(
                        f"🔗 Auto-paired receiver [{ieee}] ↔ thermostat [{via_ieee}]"
                    )

        asyncio.create_task(self._async_device_initialized(ieee))
        self._rebuild_name_maps()
        self._emit_sync("device_initialized", {"ieee": ieee})

    async def _async_device_initialized(self, ieee: str):
        """Configure device after initialization."""
        if ieee not in self.devices:
            return
        try:
            zdev = self.devices[ieee]
            # Philips-specific config, standard config, initial poll, HA discovery
            # (keeping full logic from original core.py)
            await zdev.configure()
            logger.info(f"[{ieee}] Device configured successfully")
            await zdev.poll()
            if self.mqtt:
                await self.announce_device(ieee)
        except Exception as e:
            logger.warning(f"[{ieee}] Device configuration failed: {e}")

    def device_left(self, device: zigpy.device.Device):
        ieee = str(device.ieee)
        logger.info(f"Device left: {ieee}")
        if ieee in self.devices:
            self.devices[ieee]._available = False
            self.handle_device_update(self.devices[ieee], {"available": False})
        self.polling_scheduler.disable_for_device(ieee)

        name = self.friendly_names.get(ieee, "Unknown")
        self._emit_sync("log", {"level": "WARNING",
                                "message": f"[{ieee}] ({name}) Device Left - marked offline",
                                "ieee": ieee, "device_name": name, "category": "connection"})

    def device_removed(self, device: zigpy.device.Device):
        ieee = str(device.ieee)
        logger.info(f"Device removed: {ieee}")

        if ieee in self.devices:
            self.devices[ieee].cleanup()
            del self.devices[ieee]

        if ieee in self.state_cache:
            del self.state_cache[ieee]
            self._save_state_cache()

        self.polling_scheduler.disable_for_device(ieee)
        self._rebuild_name_maps()

        name = self.friendly_names.get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Removed"
        self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee,
                                "device_name": name, "category": "connection"})
        self._emit_sync("device_left", {"ieee": ieee})

    # Stub listener methods required by zigpy
    def device_relays_updated(self, device: zigpy.device.Device, relays):
        pass

    def group_member_removed(self, *args, **kwargs): pass
    def group_member_added(self, *args, **kwargs): pass
    def group_added(self, *args, **kwargs): pass
    def group_removed(self, *args, **kwargs): pass

    # =========================================================================
    # RAW MESSAGE HANDLER (zigpy listener)
    # =========================================================================

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

        # 1. DEBUGGER - Capture packet BEFORE any logic
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

        # 2. STATS & LOGGING
        try:
            packet_stats.record_rx(ieee, size=len(message) if message else 0)
            logger.debug(f"[{ieee}] Raw message: profile=0x{profile:04x}, cluster=0x{cluster:04x}, "
                         f"src_ep={src_ep}, dst_ep={dst_ep}, len={len(message)}")
        except Exception:
            pass

        # 2b. UPDATE LAST SEEN for all known devices
        try:
            if ieee in self.devices:
                self.devices[ieee].update_last_seen()
        except Exception:
            pass

        # 3. FAST PATH (Time-critical)
        try:
            fast_pathed = self.fast_path.process_frame(
                ieee, profile, cluster, src_ep, dst_ep, message
            )
            if fast_pathed:
                logger.debug(f"[{ieee}] Fast-pathed: cluster=0x{cluster:04x}")
        except Exception as e:
            logger.debug(f"[{ieee}] Fast path error: {e}")

        # 4. ZONE LQI CAPTURE
        try:
            zone_mgr = getattr(self, 'zone_manager', None)
            if zone_mgr:
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
        except Exception as e:
            logger.debug(f"[{ieee}] Zone LQI capture error: {e}")

        # 5. CONTACT SENSOR FALLBACK
        # zigpy may not dispatch OnOff Report Attributes on output clusters
        # to handlers correctly — handle directly from raw message
        try:
            if cluster == 0x0006 and ieee in self.devices:
                zdev = self.devices[ieee]
                if hasattr(zdev, 'capabilities') and zdev.capabilities.has_capability('contact_sensor'):
                    if len(message) >= 7:
                        command_id = message[2]
                        if command_id == 0x0A:  # Report Attributes
                            attr_id = message[3] | (message[4] << 8)
                            if attr_id == 0x0000:  # OnOff attribute
                                value = bool(message[6])
                                is_open = value
                                ep_id = src_ep
                                updates = {
                                    f"contact_{ep_id}": not is_open,
                                    f"is_open_{ep_id}": is_open,
                                    f"is_closed_{ep_id}": not is_open,
                                    f"state_{ep_id}": "OPEN" if is_open else "CLOSED",
                                }
                                if ep_id == 1:
                                    updates.update({
                                        "contact": not is_open,
                                        "is_open": is_open,
                                        "is_closed": not is_open,
                                        "state": "OPEN" if is_open else "CLOSED",
                                    })
                                zdev.update_state(updates, endpoint_id=ep_id)
                                zdev.last_seen = int(time.time() * 1000)
                                logger.info(f"[{ieee}] Contact sensor: {'OPEN' if is_open else 'CLOSED'}")
        except Exception as e:
            logger.debug(f"[{ieee}] Contact sensor fallback error: {e}")


        # 6. GENERIC REPORT ATTRIBUTES DISPATCHER
        # zigpy cluster dispatch doesn't reliably reach handlers for
        # Report Attributes — parse raw ZCL and dispatch to handlers directly
        try:
            if ieee in self.devices and message and len(message) >= 7:
                frame_control = message[0]
                is_global = (frame_control & 0x03) == 0x00  # Frame type = global
                command_id = message[2]

                if is_global and command_id == 0x0A:  # Report Attributes
                    zdev = self.devices[ieee]

                    # Find handler for this cluster + endpoint
                    handler = zdev.handlers.get((src_ep, cluster))
                    if not handler:
                        handler = zdev.handlers.get(cluster)

                    if handler:
                        idx = 3
                        while idx + 3 <= len(message):
                            attr_id = message[idx] | (message[idx + 1] << 8)
                            data_type = message[idx + 2]
                            idx += 3

                            value, size = self._parse_zcl_value(data_type, message, idx)
                            if size < 0:
                                break
                            idx += size

                            if value is not None:
                                try:
                                    handler.attribute_updated(attr_id, value)
                                except Exception as e:
                                    logger.debug(f"[{ieee}] Handler dispatch 0x{cluster:04x} "
                                                 f"attr 0x{attr_id:04x} error: {e}")

                        # Update last seen
                        zdev.update_last_seen()
        except Exception as e:
            logger.debug(f"[{ieee}] Generic dispatch error: {e}")


        @staticmethod
        def _parse_zcl_value(data_type, message, idx):
            """
            Parse a ZCL typed value from raw bytes.
            Returns (value, size) or (None, -1) on error.
            """
            try:
                remaining = len(message) - idx

                # Boolean (0x10)
                if data_type == 0x10:
                    if remaining < 1:
                        return None, -1
                    return bool(message[idx]), 1

                # Bitmap8 (0x18), Uint8 (0x20), Enum8 (0x30)
                if data_type in (0x18, 0x20, 0x30):
                    if remaining < 1:
                        return None, -1
                    return message[idx], 1

                # Bitmap16 (0x19), Uint16 (0x21), Enum16 (0x31)
                if data_type in (0x19, 0x21, 0x31):
                    if remaining < 2:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 2], 'little'), 2

                # Uint24 (0x22)
                if data_type == 0x22:
                    if remaining < 3:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 3], 'little'), 3

                # Uint32 (0x23), Bitmap32 (0x1B)
                if data_type in (0x23, 0x1B):
                    if remaining < 4:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 4], 'little'), 4

                # Int8 (0x28)
                if data_type == 0x28:
                    if remaining < 1:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 1], 'little', signed=True), 1

                # Int16 (0x29)
                if data_type == 0x29:
                    if remaining < 2:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 2], 'little', signed=True), 2

                # Int32 (0x2B)
                if data_type == 0x2B:
                    if remaining < 4:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 4], 'little', signed=True), 4

                # Bitmap24 (0x1A), Uint24 (0x22)
                if data_type == 0x1A:
                    if remaining < 3:
                        return None, -1
                    return int.from_bytes(message[idx:idx + 3], 'little'), 3

                # Octet string (0x41), Character string (0x42)
                if data_type in (0x41, 0x42):
                    if remaining < 1:
                        return None, -1
                    str_len = message[idx]
                    if remaining < 1 + str_len:
                        return None, -1
                    raw = message[idx + 1:idx + 1 + str_len]
                    if data_type == 0x42:
                        return raw.decode('utf-8', errors='replace'), 1 + str_len
                    return raw, 1 + str_len

                # Unknown type — skip
                return None, -1

            except Exception:
                return None, -1
    # =========================================================================
    # DEVICE UPDATE HANDLING
    # =========================================================================

    def handle_device_update(self, zha_device, changed_data, full_state=None,
                             qos: Optional[int] = None, endpoint_id: Optional[int] = None):
        """Called by ZigManDevice when state changes."""
        ieee = zha_device.ieee

        # >>> Instant Universal Automation Trigger <<<
        # Fire automation immediately before any debouncing/sleeping occurs
        if hasattr(self, 'automation') and changed_data:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.automation.evaluate(ieee, changed_data))
            except Exception as e:
                logger.error(f"[{ieee}] Instant automation trigger failed: {e}")

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
        if not changed_data:
            return

        device_caps = zha_device.capabilities

        # Build safe MQTT payload (delta-only)
        safe_mqtt_payload = sanitise_device_state(changed_data.copy())
        safe_mqtt_payload['available'] = zha_device.is_available()
        safe_mqtt_payload['lqi'] = getattr(zha_device.zigpy_dev, 'lqi', 0) or 0

        # Contact sensor HA value transforms
        # Convert contact_N booleans to is_open/is_closed for HA
        contact_keys = [k for k in safe_mqtt_payload.keys() if k.startswith('contact_') and k.split('_')[-1].isdigit()]
        for ck in contact_keys:
            ep_suffix = ck.split('_')[-1]
            contact_val = safe_mqtt_payload[ck]
            if isinstance(contact_val, bool):
                # contact=True means closed, False means open (Zigbee convention)
                ha_val = contact_val  # True=closed
                open_key = f"is_open_{ep_suffix}"
                closed_key = f"is_closed_{ep_suffix}"
                safe_mqtt_payload[open_key] = not ha_val
                safe_mqtt_payload[closed_key] = ha_val

        # Also handle top-level 'contact' key
        if 'contact' in safe_mqtt_payload and isinstance(safe_mqtt_payload['contact'], bool):
            ha_val = safe_mqtt_payload['contact']
            safe_mqtt_payload['is_open'] = not ha_val
            safe_mqtt_payload['is_closed'] = ha_val

        # Remove internal keys
        keys_to_remove = [k for k in list(safe_mqtt_payload.keys())
                          if k.endswith('_raw') or k.startswith('attr_')]
        if not device_caps.has_capability('motion_sensor'):
            keys_to_remove.extend(['occupancy', 'motion', 'presence'])
        for key in keys_to_remove:
            safe_mqtt_payload.pop(key, None)

        # Fix multi-endpoint state
        endpoint_state_keys = [k for k in safe_mqtt_payload.keys()
                               if k.startswith('state_') and k[6:].isdigit()]
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

        # Update cache
        if ieee not in self.state_cache:
            self.state_cache[ieee] = {}

        cache_update = changed_data.copy()
        cache_update['available'] = zha_device.is_available()
        cache_update['lqi'] = getattr(zha_device.zigpy_dev, 'lqi', 0) or 0
        self.state_cache[ieee].update(sanitise_device_state(cache_update))
        self._cache_dirty = True

        # Emit to WebSocket (only changed data)
        self._emit_sync("device_updated", {"ieee": ieee, "data": safe_mqtt_payload})

        # Publish to MQTT (delta-only)
        if self.mqtt:
            safe_name = self.get_safe_name(ieee)
            mqtt_qos = qos
            asyncio.create_task(
                self.mqtt.publish(safe_name, json.dumps(safe_mqtt_payload),
                                  ieee=ieee, qos=mqtt_qos, retain=True)
            )

        # Log changed attributes
        friendly_name = self.friendly_names.get(ieee, "Unknown")
        for k, v in changed_data.items():
            if k != 'last_seen':
                ep_str = f"[EP{endpoint_id}]" if endpoint_id is not None else ""
                msg = f"[{ieee}] ({friendly_name}) {ep_str} {k}={v}"
                log_payload = {
                    "level": "INFO", "message": msg, "ieee": ieee,
                    "device_name": friendly_name, "category": "attribute_update",
                    "attribute": k, "value": v, "endpoint_id": endpoint_id
                }
                safe_log_payload = prepare_for_json(log_payload)
                self._emit_sync("log", safe_log_payload)

        # Schedule debounced cache save
        self._schedule_save()

    # =========================================================================
    # MQTT COMMAND HANDLER
    # =========================================================================

    async def handle_mqtt_command(self, device_identifier: str, data: Dict[str, Any],
                                  component: Optional[str] = None,
                                  object_id: Optional[str] = None):
        """Handle incoming MQTT command from Home Assistant."""
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

            if endpoint is None and device.capabilities.has_capability('light'):
                for ep_id in device.zigpy_dev.endpoints:
                    if ep_id == 0:
                        continue
                    ep = device.zigpy_dev.endpoints[ep_id]
                    if 0x0008 in ep.in_clusters or 0x0006 in ep.in_clusters:
                        endpoint = ep_id
                        break

            optimistic_state = {}

            state = data.get('state')
            brightness = data.get('brightness')
            color_temp = data.get('color_temp')
            color = data.get('color')

            if state:
                cmd = 'on' if str(state).upper() == 'ON' else 'off'
                result = await device.send_command(cmd, endpoint_id=endpoint, data=data)
                if result:
                    optimistic_state['state'] = state.upper() if isinstance(state, str) else ('ON' if state else 'OFF')
                    optimistic_state['on'] = (cmd == 'on')

            if brightness is not None:
                pct = int(brightness / 2.54)
                result = await device.send_command('brightness', pct, endpoint_id=endpoint)
                if result:
                    optimistic_state['brightness'] = int(brightness)
                    optimistic_state['level'] = pct
                    if brightness > 0:
                        optimistic_state['state'] = 'ON'
                        optimistic_state['on'] = True

            if color_temp is not None:
                try:
                    kelvin = int(1000000 / color_temp)
                    result = await device.send_command('color_temp', kelvin, endpoint_id=endpoint)
                    if result:
                        optimistic_state['color_temp'] = int(color_temp)
                except ZeroDivisionError:
                    pass

            if color and 'x' in color and 'y' in color:
                result = await device.send_command('xy_color', (color['x'], color['y']), endpoint_id=endpoint)
                if result:
                    optimistic_state['color'] = color

            # Handle cover/position/tilt
            position = data.get('position')
            tilt = data.get('tilt')
            if position is not None:
                await device.send_command('position', position, endpoint_id=endpoint)
            if tilt is not None:
                await device.send_command('tilt', tilt, endpoint_id=endpoint)

            # Handle thermostat
            temperature = data.get('temperature')
            if temperature is not None:
                await device.send_command('temperature', temperature, endpoint_id=endpoint)

            mode = data.get('mode') or data.get('preset_mode')
            if mode is not None:
                await device.send_command('mode', mode, endpoint_id=endpoint)

            # Optimistic update
            if optimistic_state:
                self.handle_device_update(device, optimistic_state, endpoint_id=endpoint)

        except NcpFailure as e:
            logger.error(f"[{ieee}] NCP Failure during MQTT command: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
        except Exception as e:
            logger.error(f"[{ieee}] MQTT command failed: {e}")
            traceback.print_exc()

    # =========================================================================
    # DEVICE OPERATIONS
    # =========================================================================

    async def announce_device(self, ieee: str):
        """Publish HA Discovery configs for a device."""
        if not self.mqtt or ieee not in self.devices:
            return

        try:
            zdev = self.devices[ieee]

            # Restore cached state
            if ieee in self.state_cache:
                zdev.state.update(self.state_cache[ieee])

            configs = zdev.get_device_discovery_configs()
            safe_name = self.get_safe_name(ieee)

            device_info = {
                "ieee": ieee,
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "safe_name": safe_name,
                "model": str(zdev.zigpy_dev.model),
                "manufacturer": str(zdev.zigpy_dev.manufacturer)
            }

            await self.mqtt.publish_discovery(device_info, configs)
            logger.info(f"[{ieee}] Published HA discovery")

            # Publish initial state from cache
            try:
                initial_state = zdev.state.copy()
                initial_state['available'] = False
                initial_state['lqi'] = 0
                safe_state = sanitise_device_state(initial_state)

                if 'state' in safe_state and isinstance(safe_state['state'], (int, bool, float)):
                    del safe_state['state']

                if 'state_1' in safe_state and 'state' not in safe_state:
                    safe_state['state'] = safe_state['state_1']
                elif 'state_11' in safe_state and 'state' not in safe_state:
                    safe_state['state'] = safe_state['state_11']

                await self.mqtt.publish(safe_name, json.dumps(safe_state),
                                        ieee=ieee, qos=1, retain=True)
            except Exception as e:
                logger.error(f"[{ieee}] Failed to publish initial state: {e}")

        except Exception as e:
            logger.error(f"[{ieee}] Failed to announce: {e}")

    async def send_command(self, ieee: str, command: str, value=None, endpoint_id=None):
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}
        try:
            device = self.devices[ieee]
            result = await device.send_command(command, value, endpoint_id)
            return {"success": True, "result": result}
        except NcpFailure as e:
            logger.error(f"[{ieee}] NCP Failure during command: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"[{ieee}] Command failed: {e}")
            return {"success": False, "error": str(e)}

    async def rename_device(self, ieee, name):
        self.friendly_names[ieee] = name
        self._save_json("./data/names.json", self.friendly_names)
        self._rebuild_name_maps()
        if self.mqtt and ieee in self.devices:
            await self.announce_device(ieee)
        return {"success": True}

    async def configure_device(self, ieee, config=None):
        if ieee in self.devices:
            try:
                await self.devices[ieee].configure(config)
                if config:
                    existing = self.device_settings.get(ieee, {})
                    existing.update({k: v for k, v in config.items() if v is not None and k != 'ieee'})
                    if existing:
                        self.device_settings[ieee] = existing
                        self._save_json("./data/device_settings.json", self.device_settings)
                return {"success": True}
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during configuration: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}

    async def interview_device(self, ieee):
        if ieee in self.devices:
            try:
                await self.devices[ieee].interview()
                return {"success": True}
            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during interview: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}

    async def poll_device(self, ieee):
        if ieee in self.devices:
            try:
                device = self.devices[ieee]
                results = await device.poll()
                poll_success = results.pop('__poll_success', True)
                friendly_name = self.friendly_names.get(ieee, ieee)

                if poll_success:
                    message = f"Manual poll for {friendly_name} successful."
                    self._emit_sync("poll_result", {"ieee": ieee, "success": True, "message": message})
                    return {"success": True, "message": "Poll successful"}
                else:
                    message = f"Manual poll for {friendly_name} completed with partial failures."
                    self._emit_sync("poll_result", {"ieee": ieee, "success": False,
                                                    "message": message, "error_type": "PartialFailure"})
                    return {"success": True, "message": "Poll completed with partial failures."}

            except NcpFailure as e:
                logger.error(f"[{ieee}] NCP Failure during poll: {e}")
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
                return {"success": False, "error": f"NCP Failure: {e}"}
            except Exception as e:
                logger.error(f"[{ieee}] Manual poll failed: {e}")
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "Device not found"}

    async def remove_device(self, ieee: str, force: bool = False):
        """Remove a device from the network and cleanup."""
        ieee = str(ieee).lower()

        try:
            # 1. Get the zigpy device object
            z_ieee = zigpy.types.EUI64.convert(ieee)
            zdev = None

            if z_ieee in self.app.devices:
                zdev = self.app.devices[z_ieee]

            # 2. Try graceful leave if device is known
            if not force and zdev:
                logger.info(f"[{ieee}] Sending Leave Request...")
                try:
                    async with asyncio.timeout(5.0):
                        await zdev.zdo.leave()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"[{ieee}] Leave request failed/timed out: {e}")

            # 3. Force remove from zigpy (Database)
            if zdev:
                await self.app.remove(z_ieee)
                logger.info(f"[{ieee}] Removed from zigpy application")

            # 3.5. Force database cleanup if requested
            if force:
                self._force_clean_database(ieee)

            # 4. Remove MQTT discovery BEFORE deleting device wrapper
            if self.mqtt and ieee in self.devices:
                try:
                    configs = self.devices[ieee].get_device_discovery_configs()
                    await self.mqtt.remove_discovery(ieee, configs)
                except Exception as e:
                    logger.warning(f"[{ieee}] MQTT discovery removal failed: {e}")

            # 5. Cleanup local state (In-Memory)
            if ieee in self.devices:
                del self.devices[ieee]

            # 6. Cleanup persistent JSON files
            if ieee in self.friendly_names:
                del self.friendly_names[ieee]
                self._save_json("./data/names.json", self.friendly_names)

            if ieee in self.device_settings:
                del self.device_settings[ieee]
                self._save_json("./data/device_settings.json", self.device_settings)

            # 7. Remove automation rules where this device is source or target
            if hasattr(self, 'automation'):
                for rule in list(self.automation.rules):
                    if rule.get("source_ieee") == ieee or rule.get("target_ieee") == ieee:
                        self.automation.delete_rule(rule["id"])

            # 8. Polling config
            if ieee in self.polling_config:
                del self.polling_config[ieee]
                self._save_json("./data/polling_config.json", self.polling_config)

            # 9. Remove from state cache
            if ieee in self.state_cache:
                del self.state_cache[ieee]
                self._save_state_cache()

            self.polling_scheduler.disable_for_device(ieee)
            self._rebuild_name_maps()

            # 10. Notify frontend
            self._emit_sync("device_left", {"ieee": ieee})
            self._emit_sync("log", {"level": "WARNING", "message": f"Device Removed: {ieee}", "ieee": ieee})

            return {"success": True}

        except NcpFailure as e:
            logger.error(f"[{ieee}] NCP Failure during device removal: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"Remove failed: {e}")
            return {"success": False, "error": str(e)}

    async def read_attribute(self, ieee, ep_id, cluster_id, attr_name):
        """Read a specific attribute from a device."""
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}
        try:
            device = self.devices[ieee]
            zigpy_dev = device.zigpy_dev
            if ep_id not in zigpy_dev.endpoints:
                return {"success": False, "error": f"Endpoint {ep_id} not found"}

            ep = zigpy_dev.endpoints[ep_id]
            if cluster_id not in ep.in_clusters:
                return {"success": False, "error": f"Cluster 0x{cluster_id:04x} not found"}

            cluster = ep.in_clusters[cluster_id]
            attr_id = int(attr_name) if attr_name.isdigit() else attr_name
            result = await cluster.read_attributes([attr_id])

            success_attrs = result[0] if result else {}
            safe_result = prepare_for_json(success_attrs)
            return {"success": True, "attributes": safe_result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def discover_cluster_attributes(self, ieee, endpoint_id, cluster_id):
        """Discover attributes and their access control on a device cluster."""
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        try:
            zigpy_dev = self.devices[ieee].zigpy_dev
            ep = zigpy_dev.endpoints.get(endpoint_id)
            if not ep:
                return {"success": False, "error": f"Endpoint {endpoint_id} not found"}

            cluster = ep.in_clusters.get(cluster_id) or ep.out_clusters.get(cluster_id)
            if not cluster:
                return {"success": False, "error": f"Cluster 0x{cluster_id:04X} not found"}

            # Step 1: Discover which attributes exist on the device
            discovered_ids = set()
            try:
                async with asyncio.timeout(10.0):
                    result = await cluster.discover_attributes(0, 255)
                if result:
                    for item in result:
                        try:
                            attr_id = item if isinstance(item, int) else getattr(item, 'attrid', None)
                            if attr_id is not None and isinstance(attr_id, int):
                                discovered_ids.add(attr_id)
                        except (TypeError, AttributeError):
                            continue
            except Exception as e:
                logger.warning(f"[{ieee}] Discover attributes failed: {e}")

            # For manufacturer-specific clusters (0xFC00+), also try extended range
            if cluster_id >= 0xFC00 or not discovered_ids:
                scan_ranges = [(0x0000, 0x0020)]
                if cluster_id >= 0xFC00:
                    scan_ranges = [(0x0000, 0x0050)]
                for start, end in scan_ranges:
                    for attr_id in range(start, end):
                        if attr_id in discovered_ids:
                            continue
                        try:
                            async with asyncio.timeout(2.0):
                                read_result = await cluster.read_attributes([attr_id])
                            if read_result:
                                success_attrs = read_result[0] if read_result else {}
                                failure_attrs = read_result[1] if len(read_result) > 1 else {}
                                # Only add if attr is in success dict (not in failures)
                                if attr_id in success_attrs and attr_id not in failure_attrs:
                                    discovered_ids.add(attr_id)
                        except Exception:
                            continue

            # Fallback: use zigpy cluster definition if nothing discovered
            if not discovered_ids and cluster.attributes:
                discovered_ids = set(cluster.attributes.keys())

            # Step 2: Read all discovered attributes and test write access
            attributes = []
            for attr_id in sorted(discovered_ids):

                # Get name from zigpy definition
                name = f"0x{attr_id:04X}"
                attr_type = ""
                if attr_id in cluster.attributes:
                    attr_def = cluster.attributes[attr_id]
                    if hasattr(attr_def, 'name'):
                        name = attr_def.name
                    if hasattr(attr_def, 'type') and attr_def.type:
                        attr_type = attr_def.type.__name__

                # Read value
                readable = False
                value = None
                try:
                    async with asyncio.timeout(5.0):
                        read_result = await cluster.read_attributes([attr_id])
                    if read_result and attr_id in read_result[0]:
                        val = read_result[0][attr_id]
                        if hasattr(val, 'value'):
                            val = val.value
                        value = val
                        readable = True
                except Exception:
                    pass

                # Write test: write current value back (non-destructive)
                writable = None
                if readable and value is not None:
                    try:
                        async with asyncio.timeout(5.0):
                            write_result = await cluster.write_attributes({attr_id: value})
                        if write_result and len(write_result) > 0:
                            status = write_result[0]
                            if hasattr(status, '__iter__'):
                                writable = all(
                                    getattr(s, 'status', s) == 0 for s in status
                                )
                            elif status == 0 or (hasattr(status, 'status') and status.status == 0):
                                writable = True
                            else:
                                writable = False
                    except Exception:
                        writable = False

                # Serialize value safely
                safe_value = None
                if value is not None:
                    try:
                        safe_value = prepare_for_json({0: value})[0]
                    except Exception:
                        safe_value = str(value)

                attributes.append({
                    "id": f"0x{attr_id:04X}",
                    "id_int": attr_id,
                    "name": name,
                    "type": attr_type,
                    "readable": readable,
                    "writable": writable,
                    "value": safe_value,
                })

            return {
                "success": True,
                "ieee": ieee,
                "endpoint_id": endpoint_id,
                "cluster_id": f"0x{cluster_id:04X}",
                "attributes": attributes,
            }

        except asyncio.TimeoutError:
            return {"success": False, "error": "Discovery timed out"}
        except Exception as e:
            logger.error(f"[{ieee}] Attribute discovery failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_device_config(self, ieee):
        """
        Retrieve complete device configuration:
        - Device identification (manufacturer, model, NWK, power source)
        - Node descriptor
        - Endpoints, clusters, attribute values
        - ZDO binding table
        - ZDO neighbor table (routers only)
        """
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        zdev = self.devices[ieee]
        zigpy_dev = zdev.zigpy_dev

        config = {
            "ieee": ieee,
            "nwk": f"0x{zigpy_dev.nwk:04X}" if zigpy_dev.nwk else None,
            "manufacturer": str(zigpy_dev.manufacturer or ""),
            "model": str(zigpy_dev.model or ""),
            "quirk": str(type(zigpy_dev).__module__) if type(zigpy_dev).__module__ != 'zigpy.device' else None,
            "is_initialized": zigpy_dev.is_initialized,
            "lqi": getattr(zigpy_dev, 'lqi', None),
            "rssi": getattr(zigpy_dev, 'rssi', None),
            "last_seen": getattr(zigpy_dev, 'last_seen', None),
            "node_descriptor": None,
            "endpoints": [],
            "bindings": [],
            "neighbors": [],
            "state": dict(zdev.state) if hasattr(zdev, 'state') else {},
            "device_settings": self.device_settings.get(ieee, {}),
        }

        # Node descriptor
        if zigpy_dev.node_desc:
            nd = zigpy_dev.node_desc
            config["node_descriptor"] = {
                "logical_type": str(nd.logical_type).split('.')[-1] if nd.logical_type else None,
                "manufacturer_code": nd.manufacturer_code,
                "maximum_buffer_size": nd.maximum_buffer_size,
                "maximum_incoming_transfer_size": nd.maximum_incoming_transfer_size,
                "maximum_outgoing_transfer_size": nd.maximum_outgoing_transfer_size,
                "mac_capability_flags": int(nd.mac_capability_flags) if nd.mac_capability_flags else 0,
                "is_mains_powered": getattr(nd, 'is_mains_powered', None),
                "is_router": getattr(nd, 'is_router', None),
                "is_end_device": getattr(nd, 'is_end_device', None),
                "is_receiver_on_when_idle": getattr(nd, 'is_receiver_on_when_idle', None),
            }

        # Endpoints with clusters and attribute values
        for ep_id, ep in zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue

            ep_data = {
                "endpoint_id": ep_id,
                "profile": getattr(ep, 'profile_id', None),
                "device_type": getattr(ep, 'device_type', None),
                "input_clusters": [],
                "output_clusters": [],
            }

            for cluster_id, cluster in (ep.in_clusters or {}).items():
                cluster_info = {
                    "id": f"0x{cluster_id:04X}",
                    "id_int": cluster_id,
                    "name": cluster.name if hasattr(cluster, 'name') else None,
                    "attributes": {},
                }

                # Get cached attribute values (no network I/O — use what we already have)
                try:
                    attrs_cache = getattr(cluster, '_attr_cache', {}) or {}
                    for attr_id, val in attrs_cache.items():
                        if hasattr(val, 'value'):
                            val = val.value
                        attr_name = None
                        if attr_id in cluster.attributes:
                            attr_def = cluster.attributes[attr_id]
                            attr_name = attr_def.name if hasattr(attr_def, 'name') else None
                        try:
                            safe_val = prepare_for_json({0: val})[0]
                        except Exception:
                            safe_val = str(val)
                        cluster_info["attributes"][f"0x{attr_id:04X}"] = {
                            "name": attr_name,
                            "value": safe_val,
                        }
                except Exception as e:
                    cluster_info["attributes_error"] = str(e)

                ep_data["input_clusters"].append(cluster_info)

            for cluster_id, cluster in (ep.out_clusters or {}).items():
                ep_data["output_clusters"].append({
                    "id": f"0x{cluster_id:04X}",
                    "id_int": cluster_id,
                    "name": cluster.name if hasattr(cluster, 'name') else None,
                })

            config["endpoints"].append(ep_data)

        # ZDO binding table
        try:
            async with asyncio.timeout(5.0):
                result = await zigpy_dev.zdo.Mgmt_Bind_req(0)
            if result and len(result) >= 3:
                status, _, binding_list = result[0], result[1], result[2]
                for b in binding_list or []:
                    try:
                        config["bindings"].append({
                            "src_ieee": str(b.SrcAddress) if hasattr(b, 'SrcAddress') else None,
                            "src_endpoint": getattr(b, 'SrcEndpoint', None),
                            "cluster": f"0x{b.ClusterId:04X}" if hasattr(b, 'ClusterId') else None,
                            "dst_ieee": str(b.DstAddress) if hasattr(b, 'DstAddress') else None,
                            "dst_endpoint": getattr(b, 'DstEndpoint', None),
                        })
                    except Exception:
                        pass
        except asyncio.TimeoutError:
            config["bindings_error"] = "Timeout"
        except Exception as e:
            config["bindings_error"] = str(e)

        # ZDO neighbor table (routers only)
        try:
            if config.get("node_descriptor", {}).get("is_router"):
                async with asyncio.timeout(5.0):
                    result = await zigpy_dev.zdo.Mgmt_Lqi_req(0)
                if result and len(result) >= 3:
                    status, _, neighbor_list = result[0], result[1], result[2]
                    for n in neighbor_list or []:
                        try:
                            config["neighbors"].append({
                                "ieee": str(n.IEEEAddr) if hasattr(n, 'IEEEAddr') else None,
                                "nwk": f"0x{n.NWKAddr:04X}" if hasattr(n, 'NWKAddr') else None,
                                "device_type": getattr(n, 'DeviceType', None),
                                "rx_on_when_idle": getattr(n, 'RxOnWhenIdle', None),
                                "relationship": str(getattr(n, 'Relationship', '')),
                                "depth": getattr(n, 'Depth', None),
                                "lqi": getattr(n, 'LQI', None),
                            })
                        except Exception:
                            pass
        except asyncio.TimeoutError:
            config["neighbors_error"] = "Timeout"
        except Exception as e:
            config["neighbors_error"] = str(e)

        # Serialize everything safely for JSON
        try:
            config = prepare_for_json(config)
        except Exception:
            pass

        return {"success": True, "config": config}

    async def bind_devices(self, source_ieee, target_ieee, cluster_id):
        """Bind a source device to a target device."""
        if source_ieee not in self.devices or target_ieee not in self.devices:
            return {"success": False, "error": "Device not found"}

        try:
            src_zdev = self.devices[source_ieee].zigpy_dev
            dst_zdev = self.devices[target_ieee].zigpy_dev

            src_prefs = self.devices[source_ieee].get_binding_preferences()
            dst_prefs = self.devices[target_ieee].get_binding_preferences()

            # Find source endpoint
            src_ep = None
            preferred_src_ep = src_prefs.get('source_endpoints', {}).get(cluster_id)
            if preferred_src_ep and preferred_src_ep in src_zdev.endpoints:
                ep = src_zdev.endpoints[preferred_src_ep]
                if cluster_id in ep.out_clusters:
                    src_ep = ep

            if not src_ep:
                for ep_id, ep in sorted(src_zdev.endpoints.items()):
                    if ep_id == 0:
                        continue
                    if cluster_id in ep.out_clusters:
                        src_ep = ep
                        break

            if not src_ep:
                return {"success": False, "error": f"Source has no output cluster 0x{cluster_id:04x}"}

            # Find target endpoint
            dst_ep = None
            preferred_dst_ep = dst_prefs.get('target_endpoints', {}).get(cluster_id)
            if preferred_dst_ep and preferred_dst_ep in dst_zdev.endpoints:
                ep = dst_zdev.endpoints[preferred_dst_ep]
                if cluster_id in ep.in_clusters:
                    dst_ep = ep

            if not dst_ep:
                for ep_id, ep in sorted(dst_zdev.endpoints.items()):
                    if ep_id == 0:
                        continue
                    if cluster_id in ep.in_clusters:
                        dst_ep = ep
                        break

            if not dst_ep:
                valid_eps = [ep for ep_id, ep in dst_zdev.endpoints.items() if ep_id != 0]
                if valid_eps:
                    dst_ep = valid_eps[0]
                else:
                    return {"success": False, "error": "Target has no valid endpoints"}

            dst_addr = zdo_types.MultiAddress()
            dst_addr.addrmode = 3
            dst_addr.ieee = dst_zdev.ieee
            dst_addr.endpoint = dst_ep.endpoint_id

            async with asyncio.timeout(15.0):
                result = await src_zdev.zdo.Bind_req(
                    src_zdev.ieee, src_ep.endpoint_id, cluster_id, dst_addr
                )

            logger.info(f"Bound {source_ieee} EP{src_ep.endpoint_id} -> "
                        f"{target_ieee} EP{dst_ep.endpoint_id} (0x{cluster_id:04x})")

            return {
                "success": True,
                "source_ep": src_ep.endpoint_id,
                "target_ep": dst_ep.endpoint_id,
                "message": f"Bound EP{src_ep.endpoint_id} -> EP{dst_ep.endpoint_id}"
            }
        except NcpFailure as e:
            logger.error(f"NCP Failure during binding: {e}")
            if hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": f"NCP Failure: {e}"}
        except Exception as e:
            logger.error(f"Binding failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # PAIRING
    # =========================================================================

    async def permit_join(self, duration=240, ieee=None):
        if duration == 0:
            self.pairing_expiration = 0
            self._permit_join_via = None
            try:
                await self.app.permit(0)
            except Exception as e:
                logger.warning(f"permit(0) failed: {e}")
            self._emit_sync("pairing_status", {"enabled": False, "remaining": 0})
            return {"success": True, "enabled": False}

        self.pairing_expiration = time.time() + duration

        if ieee:
            self._permit_join_via = ieee
            if ieee not in self.devices:
                return {"success": False, "error": "Target device not found"}
            try:
                zdev = self.devices[ieee].zigpy_dev
                await zdev.zdo.Mgmt_Permit_Joining_req(duration, 1)
                self._emit_sync("pairing_status", {"enabled": True, "remaining": duration})
                return {"success": True, "duration": duration, "target": ieee}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            self._permit_join_via = None
            try:
                await self.app.permit(duration)
                self._emit_sync("pairing_status", {"enabled": True, "remaining": duration})
                return {"success": True, "duration": duration, "target": "all"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def get_pairing_status(self):
        now = time.time()
        if self.pairing_expiration > now:
            remaining = int(self.pairing_expiration - now)
            return {"enabled": True, "remaining": remaining}
        return {"enabled": False, "remaining": 0}

    # =========================================================================
    # TOUCHLINK
    # =========================================================================

    async def touchlink_scan(self, channel: int = None):
        if not self._touchlink:
            self._touchlink = await create_touchlink_manager(self.app)
        return await self._touchlink.scan(channel)

    async def touchlink_identify(self, channel=None, target_ieee=None):
        if not self._touchlink:
            self._touchlink = await create_touchlink_manager(self.app)
        return await self._touchlink.identify(channel=channel, target_ieee=target_ieee)

    async def touchlink_factory_reset(self, channel=None, target_ieee=None):
        if not self._touchlink:
            self._touchlink = await create_touchlink_manager(self.app)
        return await self._touchlink.factory_reset(channel=channel, target_ieee=target_ieee)

    # =========================================================================
    # DEVICE LIST
    # =========================================================================

    def get_device_list(self):
        """Get list of all devices with their current state - JSON-safe."""
        res = []
        for ieee, zdev in self.devices.items():
            try:
                d = zdev.zigpy_dev
                caps = sorted(list(zdev.capabilities.get_capabilities())) if hasattr(zdev, 'capabilities') else []

                # Build endpoint info
                endpoints = []
                for ep_id, ep in d.endpoints.items():
                    if ep_id == 0:
                        continue

                    component_type = None
                    handler_key = (ep_id, 0x0006)
                    if handler_key in zdev.handlers:
                        h = zdev.handlers[handler_key]
                        if hasattr(h, 'get_component_type'):
                            component_type = h.get_component_type()

                    endpoints.append({
                        "id": ep_id,
                        "device_type": hex(ep.device_type) if ep.device_type else "0x0000",
                        "profile_id": hex(ep.profile_id) if ep.profile_id else "0x0000",
                        "profile": hex(ep.profile_id) if ep.profile_id else "0x0000",
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
                    "capabilities": endpoints,
                    "capability_list": caps,
                    "settings": self.device_settings.get(ieee, {}),
                    "available": zdev.is_available(),
                    "config_schema": zdev.get_device_config_schema() if hasattr(zdev, 'get_device_config_schema') else [],
                    "polling_interval": self.polling_scheduler._intervals.get(ieee, 0)
                })

            except Exception as e:
                logger.error(f"[{ieee}] Failed to build device info: {e}")

        return prepare_for_json(res)

    def get_join_history(self):
        return self.join_history

    # =========================================================================
    # POLLING CONFIG API
    # =========================================================================

    def set_polling_interval(self, ieee: str, interval: int):
        self.polling_config[ieee] = interval
        self._save_json("./data/polling_config.json", self.polling_config)
        self.polling_scheduler.set_interval(ieee, interval)
        return {"success": True, "ieee": ieee, "interval": interval}

    def get_polling_interval(self, ieee: str):
        return self.polling_scheduler._intervals.get(ieee, 0)

    def get_all_polling_intervals(self) -> Dict[str, int]:
        return dict(self.polling_scheduler._intervals)