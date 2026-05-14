"""
Device Lifecycle Mixin
Handles device joining, initialization, updates, and removal.
"""
import logging
import asyncio
import time
import json
from typing import Dict, Any, Optional
import zigpy.device

# Assuming these are imported from your project structure
from device import ZigManDevice
from modules.json_helpers import prepare_for_json, sanitise_device_state

logger = logging.getLogger("core.lifecycle")

class DeviceLifecycleMixin:

    def raw_device_initialized(self, device: zigpy.device.Device):
        logger.debug(f"Raw device initialized: {device.ieee}")

    def device_joined(self, device: zigpy.device.Device):
        """Called when a device joins the network."""
        ieee = str(device.ieee)

        if getattr(self, 'ban_manager', None) and self.ban_manager.is_banned(ieee):
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

        # Initialize join_history if it doesn't exist
        if not hasattr(self, 'join_history'):
            self.join_history = []

        self.join_history.insert(0, {
            "join_timestamp": time.time() * 1000,
            "ieee_address": ieee,
            "manufacturer": str(device.manufacturer) if device.manufacturer else "Unknown",
            "model": str(device.model) if device.model else "Unknown",
        })

        # Cap the history list at 100 entries to prevent memory leaks
        self.join_history = self.join_history[:100]

        self._rebuild_name_maps()

        name = getattr(self, 'friendly_names', {}).get(ieee, "Unknown")
        self._emit_sync("log", {"level": "INFO", "message": f"[{ieee}] ({name}) Device Joined",
                                "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_joined", {"ieee": ieee})

        # Track join time for interview status
        if hasattr(self, 'interview_status'):
            try:
                self.interview_status.on_device_joined(ieee)
            except Exception as _e:
                logger.exception(f"[{ieee}] interview_status.on_device_joined failed: {_e}")

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
                if hasattr(dev, 'capabilities'):
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

        if not hasattr(self, 'state_cache'):
            self.state_cache = {}

        if ieee in self.devices:
            # Refresh in place — preserves listeners/state across re-inits
            wrapper = self.devices[ieee]
            wrapper.zigpy_dev = device
            wrapper._identify_handlers()
            if hasattr(wrapper, 'capabilities'):
                wrapper.capabilities._detect_capabilities()
            wrapper.last_seen = int(time.time() * 1000)
        else:
            self.devices[ieee] = ZigManDevice(self, device)
            self.devices[ieee].last_seen = int(time.time() * 1000)
            if ieee in self.state_cache:
                self.devices[ieee].restore_state(self.state_cache[ieee])

        # --- Auto-pair Hive thermostat ↔ receiver ---
        if getattr(self, '_permit_join_via', None):
            new_model = str(device.model or "").upper()
            via_ieee = self._permit_join_via

            if via_ieee in self.devices:
                via_model = str(self.devices[via_ieee].zigpy_dev.model or "").upper()

                # SLT thermostat joined via SLR receiver
                if "SLT" in new_model and ("SLR" in via_model or "RECEIVER" in via_model):
                    self.device_settings.setdefault(via_ieee, {})["paired_thermostat"] = ieee
                    self.device_settings.setdefault(ieee, {})["paired_receiver"] = via_ieee
                    self._save_json("./data/device_settings.json", self.device_settings)
                    logger.info(f"🔗 Auto-paired thermostat [{ieee}] ↔ receiver [{via_ieee}]")
                    # Schedule the radio-level bind + report-config
                    asyncio.create_task(
                        self._setup_hive_thermostat_binding(slt_ieee=ieee, slr_ieee=via_ieee)
                    )

                # SLR receiver joined via SLT thermostat (reverse)
                elif ("SLR" in new_model or "RECEIVER" in new_model) and "SLT" in via_model:
                    self.device_settings.setdefault(ieee, {})["paired_thermostat"] = via_ieee
                    self.device_settings.setdefault(via_ieee, {})["paired_receiver"] = ieee
                    self._save_json("./data/device_settings.json", self.device_settings)
                    logger.info(f"🔗 Auto-paired receiver [{ieee}] ↔ thermostat [{via_ieee}]")
                    asyncio.create_task(
                        self._setup_hive_thermostat_binding(slt_ieee=via_ieee, slr_ieee=ieee)
                    )

        asyncio.create_task(self._async_device_initialized(ieee))
        self._rebuild_name_maps()
        self._emit_sync("device_initialized", {"ieee": ieee})

        # Refresh interview status — transitions to "interviewed"
        try:
            self.interview_status.emit_for(ieee)
        except Exception as _e:
            logger.debug(f"[{ieee}] interview_status.emit_for failed: {_e}")

    async def _async_device_initialized(self, ieee: str):
        """Configure device after initialization."""
        if ieee not in self.devices:
            return
        try:
            zdev = self.devices[ieee]

            self._emit_sync("join_progress", {"ieee": ieee, "stage": "configuring"})
            await zdev.configure()
            logger.info(f"[{ieee}] Device configured successfully")

            # Apply matching device profile (unified Zigbee+Matter system).
            # Runs after handler configure so per-handler reporting wins on conflicts,
            # before MQTT announce so the friendly capability set is in discovery,
            # and before poll() so friendly keys are present in the first state snapshot.
            # Idempotent — no-op when no profile matches.
            try:
                from modules.device_profile_apply import apply_profile
                await apply_profile(zdev)
            except Exception as _e:
                logger.debug(f"[{ieee}] apply_profile skipped: {_e}")

            self._emit_sync("join_progress", {"ieee": ieee, "stage": "polling"})

            await zdev.poll()
            self._emit_sync("join_progress", {"ieee": ieee, "stage": "ready"})

            if self.mqtt:
                await self.announce_device(ieee)

            # Cache topology (endpoints + clusters) — zero device traffic,
            # reads already-interviewed state from zigpy.
            try:
                from modules.zigbee_cache import record_topology
                record_topology(zdev.zigpy_dev)
            except Exception as e:
                logger.warning(f"[{ieee}] Topology cache failed: {e}")

        except Exception as e:
            logger.warning(f"[{ieee}] Device configuration failed: {e}")
            self._emit_sync("join_progress", {"ieee": ieee, "stage": "error", "error": str(e)})

    def device_left(self, device: zigpy.device.Device):
        ieee = str(device.ieee)
        logger.info(f"Device left: {ieee}")

        if ieee in self.devices:
            self.devices[ieee]._available = False
            self.handle_device_update(self.devices[ieee], {"available": False})

        if hasattr(self, 'polling_scheduler'):
            self.polling_scheduler.disable_for_device(ieee)

        name = getattr(self, 'friendly_names', {}).get(ieee, "Unknown")
        self._emit_sync("log", {"level": "WARNING",
                                "message": f"[{ieee}] ({name}) Device Left - marked offline",
                                "ieee": ieee, "device_name": name, "category": "connection"})
        self._emit_sync("device_offline", {"ieee": ieee, "name": name})

    def device_removed(self, device: zigpy.device.Device):
        ieee = str(device.ieee)
        logger.info(f"Device removed: {ieee}")

        if ieee in self.devices:
            self.devices[ieee].cleanup()
            del self.devices[ieee]

        if hasattr(self, 'interview_status'):
            try:
                self.interview_status.on_device_removed(ieee)
            except Exception:
                pass

        if hasattr(self, 'state_cache') and ieee in self.state_cache:
            del self.state_cache[ieee]
            self._save_state_cache()

        if hasattr(self, 'polling_scheduler'):
            self.polling_scheduler.disable_for_device(ieee)

        self._rebuild_name_maps()

        name = getattr(self, 'friendly_names', {}).get(ieee, "Unknown")
        msg = f"[{ieee}] ({name}) Device Removed"
        self._emit_sync("log", {"level": "INFO", "message": msg, "ieee": ieee,
                                "device_name": name, "category": "connection"})
        self._emit_sync("device_left", {"ieee": ieee})

        # Prevent memory leaks from pending tasks/data
        if ieee in getattr(self, '_update_debounce_tasks', {}):
            self._update_debounce_tasks[ieee].cancel()
            del self._update_debounce_tasks[ieee]

        if ieee in getattr(self, '_pending_device_updates', {}):
            del self._pending_device_updates[ieee]

    def handle_device_update(self, zha_device, changed_data, full_state=None,
                             qos: Optional[int] = None, endpoint_id: Optional[int] = None):
        """Called by ZigManDevice when state changes."""
        try:
            from modules.device_profile_apply import transform_state_with_profile
            if isinstance(updates, dict) and any(k.startswith("cluster_") for k in updates):
                merged = {**device.state, **updates}
                transformed = transform_state_with_profile(device, merged)
                extras = {k: v for k, v in transformed.items()
                          if k not in updates and k not in device.state}
                if extras:
                    updates = {**updates, **extras}
        except Exception:
            pass

        ieee = zha_device.ieee

        # >>> Instant Universal Automation Trigger <<<
        # Fire automation immediately before any debouncing/sleeping occurs
        if hasattr(self, 'automation') and changed_data:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.automation.evaluate(ieee, changed_data))
            except Exception as e:
                logger.error(f"[{ieee}] Instant automation trigger failed: {e}")

        # >>> Telemetry history recording <<<
        # Persist attribute changes to DuckDB for History tab / trend charts.
        if getattr(self, 'telemetry_collector', None) and changed_data:
            try:
                self.telemetry_collector.record_state_change(ieee, changed_data)
            except Exception as e:
                logger.debug(f"[{ieee}] Telemetry record failed: {e}")

        # 1. Initialize accumulation dictionaries if they don't exist yet
        if not hasattr(self, '_pending_device_updates'):
            self._pending_device_updates = {}

        if not hasattr(self, '_update_debounce_tasks'):
            self._update_debounce_tasks = {}

        if ieee not in self._pending_device_updates:
            self._pending_device_updates[ieee] = {}

        # 2. ACCUMULATE the changes instead of replacing them
        self._pending_device_updates[ieee].update(changed_data)

        if ieee in self._update_debounce_tasks:
            self._update_debounce_tasks[ieee].cancel()

        self._update_debounce_tasks[ieee] = asyncio.create_task(
            self._debounced_device_update(zha_device, full_state, qos, endpoint_id)
        )

    async def _debounced_device_update(self, zha_device, full_state, qos, endpoint_id):
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

        ieee = zha_device.ieee

        # 3. Pop the accumulated data for processing
        changed_data = self._pending_device_updates.pop(ieee, {})
        if not changed_data:
            return

        device_caps = getattr(zha_device, 'capabilities', None)
        has_motion = device_caps.has_capability('motion_sensor') if device_caps else False

        # Build safe MQTT payload (delta-only)
        safe_mqtt_payload = sanitise_device_state(changed_data.copy())

        # safely handle zigpy device attributes
        zigpy_dev = getattr(zha_device, 'zigpy_dev', None)

        safe_mqtt_payload['available'] = zha_device.is_available()
        safe_mqtt_payload['lqi'] = getattr(zigpy_dev, 'lqi', 0) if zigpy_dev else 0

        # Contact sensor HA value transforms
        contact_keys = [k for k in safe_mqtt_payload.keys() if k.startswith('contact_') and k.split('_')[-1].isdigit()]
        for ck in contact_keys:
            ep_suffix = ck.split('_')[-1]
            contact_val = safe_mqtt_payload[ck]
            if isinstance(contact_val, bool):
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

        if not has_motion:
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
        if not hasattr(self, 'state_cache'):
            self.state_cache = {}

        if ieee not in self.state_cache:
            self.state_cache[ieee] = {}

        cache_update = changed_data.copy()
        cache_update['available'] = zha_device.is_available()
        cache_update['lqi'] = getattr(zigpy_dev, 'lqi', 0) if zigpy_dev else 0
        self.state_cache[ieee].update(sanitise_device_state(cache_update))
        self._cache_dirty = True

        # Emit to WebSocket (only changed data)
        self._emit_sync("device_updated", {"ieee": ieee, "data": safe_mqtt_payload})

        # Publish to MQTT (delta-only)
        if getattr(self, 'mqtt', None):
            safe_name = self.get_safe_name(ieee)
            mqtt_qos = qos
            asyncio.create_task(
                self.mqtt.publish(safe_name, json.dumps(safe_mqtt_payload),
                                  ieee=ieee, qos=mqtt_qos, retain=True)
            )

        # Log changed attributes
        friendly_name = getattr(self, 'friendly_names', {}).get(ieee, "Unknown")
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
        if hasattr(self, '_schedule_save'):
            self._schedule_save()