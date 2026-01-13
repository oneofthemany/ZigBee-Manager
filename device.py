"""
Zigbee Manager Device Wrapper - Handles cluster handlers and state management.
Based on ZHA's device architecture.
"""
import time
import logging
import asyncio
import json
from typing import Dict, Any, Optional, List
from zigpy.zcl.clusters.general import Basic

# Import the registry from handlers package
from handlers.base import HANDLER_REGISTRY, ClusterHandler

# import from modules
from modules.error_handler import with_retries, CommandWrapper
from modules.device_capabilities import DeviceCapabilities
from modules.json_helpers import sanitise_device_state
from modules.packet_stats import packet_stats

# Import handlers to trigger registration decorators
from handlers.security import *
from handlers.basic import *
from handlers.switches import *
from handlers.power import *
from handlers.hvac import *
from handlers.sensors import *
from handlers.tuya import *
from handlers.blinds import *
from handlers.aqara import *
from handlers.lightlink import *
from handlers.lighting import *
from handlers.diagnostics import *


logger = logging.getLogger("device")

# How long before a device is considered unavailable
CONSIDER_UNAVAILABLE_BATTERY = 60 * 60 * 25  # 25 hours for battery devices
CONSIDER_UNAVAILABLE_MAINS = 60 * 60 * 25    # 25 hours for mains-powered devices


class ZigManDevice:
    """
    Wrapper around a zigpy device that manages cluster handlers
    and state aggregation.
    """

    def __init__(self, service, zigpy_dev):
        self.service = service
        self.zigpy_dev = zigpy_dev
        self.ieee = str(zigpy_dev.ieee)

        # Handlers stored by (endpoint_id, cluster_id) tuple
        self.handlers: Dict[Any, Any] = {}

        self.state: Dict[str, Any] = {}

        # Initialize to 0 so devices appear Offline until they communicate
        self.last_seen = 0

        self.quirk_name = "None"
        self._available = True

        # Track sources of attributes to detect duplicates
        self._attribute_sources: Dict[str, Dict[int, float]] = {}

        # User preferences for specific endpoints (loaded from settings)
        self._preferred_endpoints: Dict[str, int] = {}

        # Command wrapper for resilient operations
        self._cmd_wrapper = None  # Will be initialized after device is ready

        # Check if quirk is applied
        if hasattr(zigpy_dev, 'quirk_class'):
            self.quirk_name = zigpy_dev.quirk_class.__name__

        # Identify and attach handlers
        self._identify_handlers()

        # Initialize Capabilities Logic
        self.capabilities = DeviceCapabilities(self)

        # Initialize basic info from Zigpy device
        self.manufacturer = zigpy_dev.manufacturer
        self.model = zigpy_dev.model

        # --- FORCE INFO INTO STATE IMMEDIATELY ---
        if self.manufacturer:
            self.state["manufacturer"] = str(self.manufacturer)
        else:
            self.state["manufacturer"] = "Unknown"

        if self.model:
            self.state["model"] = str(self.model)
        else:
            self.state["model"] = "Unknown"

        # Initialize command wrapper
        try:
            self._cmd_wrapper = CommandWrapper(self)
        except Exception as e:
            logger.debug(f"[{self.ieee}] Could not create command wrapper: {e}")

        # Load preferred endpoints from settings if available
        if self.ieee in self.service.device_settings:
            settings = self.service.device_settings[self.ieee]
            if 'preferred_endpoints' in settings:
                self._preferred_endpoints = settings['preferred_endpoints']

        # Schedule query only if absolutely nothing is known
        if self.manufacturer is None or self.model is None:
            self._schedule_basic_info_query()

        # Perform initial cleanup of state
        self.sanitize_state()

        logger.info(f"[{self.ieee}] Device wrapper created - "
                    f"Model: {self.model}, Manufacturer: {self.manufacturer}, "
                    f"Quirk: {self.quirk_name}")

    def sanitize_state(self):
        """Purges invalid fields from self.state based on current capabilities."""
        if not hasattr(self, 'capabilities'):
            return

        keys_to_remove = []
        for key in self.state:
            # Check if field is allowed
            if not self.capabilities.allows_field(key):
                keys_to_remove.append(key)

        if keys_to_remove:
            logger.info(f"[{self.ieee}] 🧹 SANITIZING STATE: Removing unsupported keys: {keys_to_remove}")
            for key in keys_to_remove:
                del self.state[key]

            # Sync back to cache immediately if we purged something
            if hasattr(self.service, 'state_cache'):
                self.service.state_cache[self.ieee] = self.state.copy()
                self.service._cache_dirty = True
                logger.info(f"[{self.ieee}] 💾 Cache dirty flagged after sanitization")

    def _schedule_basic_info_query(self):
        """Schedule a background task to query basic info."""
        asyncio.create_task(self._query_basic_info())

    async def _query_basic_info(self):
        """Attempts to query the Basic cluster for manufacturer/model."""
        try:
            for ep_id, ep in self.zigpy_dev.endpoints.items():
                if ep_id == 0: continue
                if Basic.cluster_id in ep.in_clusters:
                    # Attr 0x0004=Manuf, 0x0005=Model
                    results = await ep.basic.read_attributes([0x0004, 0x0005])

                    updates = {}
                    if 0x0004 in results[0]:
                        self.manufacturer = results[0][0x0004]
                        self.zigpy_dev.manufacturer = self.manufacturer
                        updates["manufacturer"] = str(self.manufacturer)

                    if 0x0005 in results[0]:
                        self.model = results[0][0x0005]
                        self.zigpy_dev.model = self.model
                        updates["model"] = str(self.model)

                    if updates:
                        logger.info(f"[{self.ieee}] Resolved Info: {self.manufacturer} / {self.model}")
                        # Re-detect capabilities in case quirks apply now
                        self.capabilities._detect_capabilities()
                        # Sanitize state again with new capabilities
                        self.sanitize_state()
                        # Update state so cache gets the new info
                        self.update_state(updates)
                    break
        except Exception as e:
            logger.warning(f"[{self.ieee}] Failed basic info query: {e}")

    def get_capabilities_info(self) -> Dict[str, Any]:
        """Get device capabilities information for debugging/UI display."""
        if not hasattr(self, 'capabilities'):
            return {"error": "Capabilities not initialized", "capabilities": [], "clusters": []}
        return self.capabilities.get_info()

    def get_details(self) -> Dict[str, Any]:
        """
        Get device details.
        Required by handlers (like Tuya) for logging or logic branching.
        """
        return {
            "ieee": self.ieee,
            "manufacturer": str(self.manufacturer) if self.manufacturer else "Unknown",
            "model": str(self.model) if self.model else "Unknown",
            "quirk": self.quirk_name
        }

    # =========================================================================
    # ZOMBIE KILLER: The Fix for Triplicate Events
    # =========================================================================
    def _detach_handlers(self):
        """
        Aggressively clean up listeners to prevent 'Zombie Handler' duplication.
        This scans the underlying zigpy device clusters and removes any listener
        that looks like a Zigbee-Manager handler, even from previous instances.
        """
        # 1. Clean up handlers known to this instance (Standard cleanup)
        if self.handlers:
            for handler in self.handlers.values():
                if hasattr(handler, 'cluster') and handler.cluster:
                    if handler in handler.cluster._listeners:
                        handler.cluster._listeners.remove(handler)
            self.handlers.clear()

        # 2. NUCLEAR OPTION: Scan for zombies from previous runs
        # This fixes the issue where recreating the device wrapper leaves old handlers attached
        # because the zigpy_dev object persists in memory.
        cleaned_count = 0

        try:
            for ep in self.zigpy_dev.endpoints.values():
                if ep.endpoint_id == 0: continue

                # Check all clusters (In and Out)
                all_clusters = list(ep.in_clusters.values()) + list(ep.out_clusters.values())

                for cluster in all_clusters:
                    if not hasattr(cluster, '_listeners'): continue

                    # Create a copy to iterate safely while modifying
                    # zigpy listeners are usually a list
                    current_listeners = list(cluster._listeners)

                    for listener in current_listeners:
                        is_zombie = False

                        # Check if it's one of OUR handlers
                        # Method A: Instance of ClusterHandler class
                        if isinstance(listener, ClusterHandler):
                            is_zombie = True

                        # Method B: Check module name (handles reloads/different class refs)
                        elif hasattr(listener, '__module__') and 'handlers' in listener.__module__:
                            is_zombie = True

                        if is_zombie:
                            if listener in cluster._listeners:
                                cluster._listeners.remove(listener)
                                cleaned_count += 1

        except Exception as e:
            logger.error(f"[{self.ieee}] Error during zombie cleanup: {e}")

        if cleaned_count > 0:
            logger.warning(f"[{self.ieee}] 🧟 Removed {cleaned_count} zombie handlers from zigpy clusters")


    def _identify_handlers(self):
        """Scan device endpoints and attach appropriate cluster handlers."""

        # Aggressively clean up old handlers first!
        self._detach_handlers()

        binding_prefs = self.get_binding_preferences()
        preferred_endpoints = {}

        if 'target_endpoints' in binding_prefs:
            for cluster_id, ep_id in binding_prefs['target_endpoints'].items():
                preferred_endpoints[cluster_id] = ep_id

        # === ADD PHILIPS HUE MOTION SENSOR QUIRK ===
        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        if "philips" in manufacturer or "signify" in manufacturer:
            if "sml" in model:
                preferred_endpoints[0x0406] = 2
                preferred_endpoints[0x0400] = 2
                preferred_endpoints[0x0402] = 2
                logger.info(f"[{self.ieee}] Applied Philips Hue Motion quirk: sensors on EP2")
        # === END QUIRK ===

        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0: continue

            def attach_handler(cluster, is_server=True):
                cid = cluster.cluster_id
                handler_cls = HANDLER_REGISTRY.get(cid)

                if handler_cls:
                    if cid in preferred_endpoints and ep_id != preferred_endpoints[cid]:
                        logger.debug(f"[{self.ieee}] Skipping cluster 0x{cid:04x} on EP{ep_id} (preferred EP{preferred_endpoints[cid]})")
                        return

                    try:
                        handler_key = (ep_id, cid)
                        handler = handler_cls(self, cluster)
                        self.handlers[handler_key] = handler
                        if cid not in self.handlers or ep_id == 1:
                            self.handlers[cid] = handler
                    except Exception as e:
                        logger.error(f"[{self.ieee}] Failed to attach handler for EP{ep_id} 0x{cid:04x}: {e}")

            for cluster in ep.in_clusters.values(): attach_handler(cluster, is_server=True)
            for cluster in ep.out_clusters.values(): attach_handler(cluster, is_server=False)

        if hasattr(self, 'capabilities'):
            self.capabilities._detect_capabilities()
            self.sanitize_state()

    def restore_state(self, cached_state):
        """
        Restore device state from cache.
        Purges invalid data and syncs cleanup back to the Service cache.
        """
        if cached_state:
            # 1. Load the raw cached state
            self.state.update(cached_state)

            # ===== Clear motion timers on startup =====
            if any(k in cached_state for k in ['occupancy', 'motion', 'presence']):
                # Never restore motion state - sensors will send new events
                self.state['occupancy'] = False
                self.state['motion'] = False
                self.state['presence'] = False
                logger.debug(f"[{self.ieee}] Cleared motion state on restoration")

            # 2. Inject Manufacturer/Model from Zigpy DB if missing in cache
            if self.zigpy_dev.manufacturer and ('manufacturer' not in self.state or self.state['manufacturer'] == 'Unknown'):
                self.state['manufacturer'] = str(self.zigpy_dev.manufacturer)
                self.manufacturer = self.zigpy_dev.manufacturer
                logger.info(f"[{self.ieee}] 💉 Injected missing Manufacturer: {self.manufacturer}")

            if self.zigpy_dev.model and ('model' not in self.state or self.state['model'] == 'Unknown'):
                self.state['model'] = str(self.zigpy_dev.model)
                self.model = self.zigpy_dev.model
                logger.info(f"[{self.ieee}] 💉 Injected missing Model: {self.model}")

            # 3. PURGE: Aggressively remove fields that don't belong
            self.sanitize_state()


            # 4. SYNC BACK: CRITICAL STEP
            # Update the Service's cache with our now-clean state.
            if hasattr(self.service, 'state_cache'):
                self.service.state_cache[self.ieee] = self.state.copy()
                self.service._cache_dirty = True
                # logger.debug(f"[{self.ieee}] Cache synced after restore")

            # Restore last_seen
            if 'last_seen' in cached_state:
                self.last_seen = cached_state['last_seen']
                self._available = self.is_available()

    def check_availability_change(self) -> bool:
        """Check if availability state has changed."""
        is_now_available = self.is_available()
        if is_now_available != self._available:
            self._available = is_now_available
            status_str = "Online" if is_now_available else "Offline"
            logger.info(f"[{self.ieee}] Availability changed to {status_str}")
            self.service.handle_device_update(self, {})
            return True
        return False

    def update_last_seen(self):
        """Update the last_seen timestamp to now."""
        self.last_seen = int(time.time() * 1000)
        self.state['last_seen'] = self.last_seen
        if not self._available:
            self._available = True
            self.service.handle_device_update(self, {})

    def update_state(self, data: Dict[str, Any], qos: Optional[int] = None, endpoint_id: Optional[int] = None):
        """Update device state and notify the service."""

        # === CAPABILITY-BASED FILTERING ===
        if hasattr(self, 'capabilities') and hasattr(self.capabilities, 'filter_state_update'):
            data = self.capabilities.filter_state_update(data)

        if not data:
            return

        changed = {}
        duplicates_detected = []

        # Always report these fields even if value unchanged
        ALWAYS_REPORT = {'occupancy', 'presence', 'motion', 'contact', 'alarm',
                         'temperature', 'tamper', 'battery_low', 'vibration',
                         'on_with_timed_off', 'action'}

        for k, v in data.items():
            # --- DUPLICATE DETECTION ---
            if endpoint_id is not None:
                if k not in self._attribute_sources:
                    self._attribute_sources[k] = {}
                self._attribute_sources[k][endpoint_id] = time.time()

                if len(self._attribute_sources[k]) > 1:
                    if k in self._preferred_endpoints:
                        if endpoint_id != self._preferred_endpoints[k]:
                            continue

                    # Outlier detection (ignore 0 if we have better data)
                    if isinstance(v, (int, float)) and v == 0:
                        has_better = any(eid != endpoint_id for eid in self._attribute_sources[k])
                        if has_better:
                            duplicates_detected.append({
                                "attribute": k, "value": v,
                                "endpoint": endpoint_id, "reason": "outlier_zero"
                            })
                            continue

            # Report if always-report field OR value changed
            if k in ALWAYS_REPORT or self.state.get(k) != v:
                changed[k] = v

        # --- INTELLIGENT STATE MERGING FOR LIGHTS ---
        has_light = (hasattr(self, 'capabilities') and
                     hasattr(self.capabilities, 'has_capability') and
                     self.capabilities.has_capability('light'))

        if has_light:
            light_attrs = {'state', 'on', 'brightness', 'level', 'color_temp',
                           'color_temperature', 'color_temperature_mireds',
                           'color_temp_kelvin', 'hue', 'saturation', 'x', 'y'}

            has_light_update = any(
                k in light_attrs or
                any(k.startswith(f"{attr}_") for attr in light_attrs)
                for k in data.keys()
            )

            if has_light_update:
                # Include current state for consistency
                for attr in ['state', 'on', 'brightness', 'level', 'color_temp']:
                    if attr not in changed and attr in self.state:
                        changed[attr] = self.state[attr]

                # Endpoint-specific fields
                if endpoint_id is not None:
                    for attr in ['state', 'on']:
                        key = f"{attr}_{endpoint_id}"
                        if key in self.state and key not in changed:
                            changed[key] = self.state[key]

        # Update internal state
        self.state.update(data)

        # Ensure metadata
        if 'manufacturer' not in self.state and self.manufacturer:
            self.state['manufacturer'] = str(self.manufacturer)
        if 'model' not in self.state and self.model:
            self.state['model'] = str(self.model)
        if 'last_seen' not in data:
            self.last_seen = int(time.time() * 1000)
            self.state['last_seen'] = self.last_seen

        self._available = True

        if changed:
            changed['last_seen'] = self.last_seen

            # Update state cache and schedule save
            self.service.state_cache[self.ieee] = self.state.copy()
            self.service._cache_dirty = True
            self.service._schedule_save()

            # Update service cache
            self.service.handle_device_update(self, changed, qos=qos, endpoint_id=endpoint_id)

            # Publish for lights only
            if has_light:
                self._publish_json_state(changed, endpoint_id)

            if duplicates_detected:
                self.service._emit_sync("duplicate_attribute_warning", {
                    "ieee": self.ieee, "details": duplicates_detected
                })


    def _publish_json_state(self, changed_data: Dict[str, Any], endpoint_id: Optional[int] = None):
        """Helper to format and publish state in JSON format."""
        if not hasattr(self.service, 'mqtt') or not self.service.mqtt:
            return

        caps = self.capabilities

        # Build base payload
        payload = {
            'available': True,
            'linkquality': getattr(self.zigpy_dev, 'lqi', 0) or 0,
            'last_seen': self.last_seen,
        }

        # Determine device type
        is_light = caps.has_capability('light')
        is_cover = caps.has_capability('cover')
        is_switch = caps.has_capability('switch')
        is_motion = caps.has_capability('motion_sensor')
        is_contact = caps.has_capability('contact_sensor')

        # === LIGHTS ===
        if is_light:
            # State
            state_val = (changed_data.get(f'state_{endpoint_id}') or
                         changed_data.get('state') or
                         self.state.get(f'state_{endpoint_id}') or
                         self.state.get('state'))

            if state_val is not None:
                payload['state'] = state_val.upper() if isinstance(state_val, str) else ('ON' if state_val else 'OFF')

            # Brightness
            if caps.has_capability('level_control'):
                bri = (changed_data.get(f'brightness_{endpoint_id}') or
                       changed_data.get('brightness') or
                       self.state.get(f'brightness_{endpoint_id}') or
                       self.state.get('brightness'))

                if bri is not None and isinstance(bri, (int, float)):
                    if bri <= 100 and bri > 1:
                        bri = int(bri * 2.54)
                    payload['brightness'] = min(254, max(0, int(bri)))

            # Color temp
            if caps.has_capability('color_control'):
                ct = (changed_data.get('color_temp_mireds') or
                      changed_data.get('color_temp') or
                      self.state.get('color_temp_mireds') or
                      self.state.get('color_temp'))
                if ct:
                    payload['color_temp'] = int(ct)

        # === COVERS ===
        elif is_cover:
            position = (changed_data.get('position') or
                        changed_data.get('current_position') or
                        self.state.get('position', 0))
            payload['position'] = int(position) if position is not None else 0

            if payload['position'] == 0:
                payload['state'] = 'closed'
            elif payload['position'] == 100:
                payload['state'] = 'open'
            else:
                payload['state'] = 'open'

        # === SENSORS (Binary + Environmental) ===

        # ---- CONTACT SENSOR ----
        if is_contact:
            key = f'contact_{endpoint_id}' if endpoint_id is not None else 'contact'

            raw_contact = (
                changed_data.get(key) if key in changed_data
                else self.state.get(key)
            )

            if raw_contact is not None:
                # Zigbee: True = CLOSED, False = OPEN
                # HA door: True = OPEN, False = CLOSED
                ha_contact = not bool(raw_contact)

                payload[key] = ha_contact
                payload['state'] = 'ON' if ha_contact else 'OFF'

        # ---- MOTION SENSOR ----
        if is_motion:
            occ_val = (
                    changed_data.get('occupancy')
                    or changed_data.get('motion')
                    or changed_data.get('presence')
            )

            if occ_val is None:
                occ_val = (
                        self.state.get('occupancy')
                        or self.state.get('motion')
                        or self.state.get('presence')
                )

            if occ_val is not None:
                payload['occupancy'] = bool(occ_val)
                payload.setdefault('state', 'ON' if occ_val else 'OFF')


        blocked_fields = {f'contact_{endpoint_id}', 'contact'}


        # === ALLOWED FIELDS ===
        # Use allows_field() to filter - this prevents motion/contact cross-contamination
        for key in list(changed_data.keys()) + list(self.state.keys()):
            if key in payload or key in blocked_fields:
                continue

            # Get value from changed_data first, fallback to self.state
            value = changed_data.get(key)
            if value is None:
                value = self.state.get(key)

            if value is not None:
                payload[key] = value

        # Publish
        if payload and len(payload) > 3:  # More than just metadata
            safe_name = self.service.friendly_names.get(self.ieee, self.ieee)
            topic = f"{self.service.mqtt.base_topic}/{safe_name}"
            asyncio.create_task(self.service.mqtt.publish(topic, json.dumps(payload)))


    def set_preferred_endpoint(self, attribute: str, endpoint_id: int):
        """Pin a specific endpoint for an attribute."""
        self._preferred_endpoints[attribute] = endpoint_id
        if self.ieee not in self.service.device_settings:
            self.service.device_settings[self.ieee] = {}
        self.service.device_settings[self.ieee]['preferred_endpoints'] = self._preferred_endpoints
        self.service._save_json("device_settings.json", self.service.device_settings)
        logger.info(f"[{self.ieee}] Pinned {attribute} to Endpoint {endpoint_id}")

    def is_available(self) -> bool:
        """Check if device is considered available."""
        role = self.get_role()
        if role == "Coordinator":
            return True

        # Passive event-driven devices are always "available"
        # They only report on events, not periodic check-ins
        if self._is_passive_device():
            return True

        if self.last_seen == 0:
            return False

        elapsed = (time.time() * 1000) - self.last_seen
        threshold = CONSIDER_UNAVAILABLE_BATTERY if self._is_battery_powered() else CONSIDER_UNAVAILABLE_MAINS
        return elapsed < (threshold * 1000)

    def _is_passive_device(self) -> bool:
        """Check if device is passive (event-driven only)."""
        # Binary sensors that only report on state changes
        passive_capabilities = {
            'occupancy', 'motion', 'presence',
            'contact', 'water_leak', 'vibration',
            'smoke', 'gas', 'tamper', 'sos'
        }

        # Check if device ONLY has passive capabilities
        device_caps = set(self.capabilities.get_capabilities())
        has_passive = device_caps & passive_capabilities
        has_active = device_caps & {'temperature', 'humidity', 'battery'}

        # If it has passive sensors but no active reporting, it's passive-only
        return bool(has_passive) and not has_active

    def _is_battery_powered(self) -> bool:
        """Check if device is battery powered."""
        # 1. Check strict Power Source attribute if available
        power_source = str(self.state.get('power_source', '')).lower()
        if 'mains' in power_source or 'dc' in power_source:
            return False
        if 'battery' in power_source:
            return True

        # 2. Check Logical Type (Router vs End Device)
        role = self.get_role()
        if role == "Router" or role == "Coordinator":
            return False

        # 3. Check for Green Power Proxy (0x0021) - Indicates Mains
        # Skip ZDO (EP0) to avoid 'No such in_clusters ZDO command' error
        try:
            for ep_id, ep in self.zigpy_dev.endpoints.items():
                if ep_id == 0: continue # SKIP ZDO
                if 0x0021 in ep.in_clusters or 0x0021 in ep.out_clusters:
                    return False
        except Exception:
            pass # Safety catch for iteration issues

        # 4. Fallback for End Devices (Assume Battery unless proven otherwise)
        if "lumi.switch" in str(self.model).lower() and "neutral" in str(self.model).lower():
            pass

        return True


    async def configure(self, config: Optional[Dict] = None):
        """Configure cluster handlers with smart endpoint filtering."""
        logger.info(f"[{self.ieee}] Configuring device...")

        # Fast Path: Targeted Updates
        if config and config.get('updates'):
            updates = config['updates']
            for handler in self.handlers.values():
                if hasattr(handler, 'apply_configuration'):
                    try:
                        await handler.apply_configuration(updates)
                    except Exception as e:
                        logger.warning(f"[{self.ieee}] Config failed for {handler.__class__.__name__}: {e}")
            if 'qos' in config:
                self.service.device_settings.setdefault(self.ieee, {})['qos'] = config['qos']
            return

        # Slow Path: Smart Configuration using capabilities
        stats = {
            'configured': 0,
            'skipped_not_configurable': 0,
            'skipped_controller': 0,
            'failed': 0,
        }

        configured = set()

        for h in self.handlers.values():
            if h in configured:
                continue

            ep_id = h.endpoint.endpoint_id
            cluster_id = h.cluster_id

            # Check endpoint configurability
            if not self.capabilities.is_endpoint_configurable(ep_id):
                role = self.capabilities.get_endpoint_role(ep_id)
                if role == 'controller':
                    stats['skipped_controller'] += 1
                    logger.debug(f"[{self.ieee}] Skip EP{ep_id}:0x{cluster_id:04x} (controller endpoint)")
                else:
                    stats['skipped_not_configurable'] += 1
                    logger.debug(f"[{self.ieee}] Skip EP{ep_id}:0x{cluster_id:04x} (no configurable clusters)")
                continue

            # Check cluster configurability
            if not self.capabilities.is_cluster_configurable(cluster_id, ep_id):
                stats['skipped_not_configurable'] += 1
                logger.debug(f"[{self.ieee}] Skip EP{ep_id}:0x{cluster_id:04x} (cluster not configurable)")
                continue

            # Configure
            try:
                await h.configure()
                configured.add(h)
                stats['configured'] += 1
            except Exception as e:
                stats['failed'] += 1
                logger.warning(f"[{self.ieee}] Config failed EP{ep_id}:0x{cluster_id:04x}: {e}")

        # Summary
        total_skipped = stats['skipped_not_configurable'] + stats['skipped_controller']
        logger.info(
            f"[{self.ieee}] Config: {stats['configured']} configured, "
            f"{total_skipped} skipped, {stats['failed']} failed"
        )

    async def interview(self):
        """Re-interview the device."""
        logger.info(f"[{self.ieee}] Re-interviewing...")
        try:
            await self.zigpy_dev.zdo.Node_Desc_req()
            await self.zigpy_dev.zdo.Active_EP_req()
            for ep_id in self.zigpy_dev.endpoints:
                if ep_id == 0: continue
                await self.zigpy_dev.zdo.Simple_Desc_req(ep_id)
            self._identify_handlers()
            logger.info(f"[{self.ieee}] Interview complete")
        except Exception as e:
            logger.error(f"[{self.ieee}] Interview failed: {e}")
            raise

    async def poll(self) -> Dict[str, Any]:
        """Poll all handlers."""
        logger.info(f"[{self.ieee}] Polling device...")
        results = {}
        polled = set()
        success = True

        for h in self.handlers.values():
            if h in polled: continue
            polled.add(h)
            try:
                res = await self._cmd_wrapper.execute(h.poll)
                if res: results.update(res)
            except:
                success = False

        if results: self.update_state(results)
        results['__poll_success'] = success
        return results

    def get_control_commands(self) -> List[Dict[str, Any]]:
        """Get available control commands."""
        commands = []
        seen = set()
        for h in self.handlers.values():
            if h in seen: continue
            seen.add(h)

            eid = h.endpoint.endpoint_id
            if h.CLUSTER_ID == 0x0006:
                commands.extend([
                    {"command": "on", "label": "On", "endpoint_id": eid},
                    {"command": "off", "label": "Off", "endpoint_id": eid},
                    {"command": "toggle", "label": "Toggle", "endpoint_id": eid}
                ])
            elif h.CLUSTER_ID == 0x0008:
                commands.append({"command": "brightness", "label": "Brightness", "type": "slider", "min": 0, "max": 100, "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0300:
                commands.append({"command": "color_temp", "label": "Color Temp", "type": "slider", "min": 2000, "max": 6500, "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0201:
                commands.append({"command": "temperature", "label": "Temp Setpoint", "type": "number", "unit": "C", "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0102:
                commands.extend([
                    {"command": "open", "label": "Open", "endpoint_id": eid},
                    {"command": "close", "label": "Close", "endpoint_id": eid},
                    {"command": "stop", "label": "Stop", "endpoint_id": eid},
                    {"command": "position", "label": "Position", "type": "slider", "min": 0, "max": 100, "endpoint_id": eid}
                ])
        return commands

    @with_retries(max_retries=3, backoff_base=1.5, timeout=10.0)
    async def send_command(self, command: str, value=None, endpoint_id=None, data: Optional[Dict] = None):
        """Execute command on device."""
        try:
            packet_stats.record_tx(self.ieee)
            logger.info(f"[{self.ieee}] CMD: {command}={value} EP={endpoint_id}")
            command = command.lower()

            # Normalize value types (frontend sends strings)
            if value is not None and isinstance(value, str):
                if value.replace('.', '').replace('-', '').isdigit():
                    value = float(value) if '.' in value else int(value)

            def get_handler(cid):
                if endpoint_id:
                    if (endpoint_id, cid) in self.handlers:
                        return self.handlers[(endpoint_id, cid)]
                return self.handlers.get(cid)

            # Check capabilities safely
            has_cap = getattr(self.capabilities, 'has_capability', lambda x: False)

            # Track optimistic state changes
            optimistic_state = {}
            success = False

            # LIGHT / SWITCH LOGIC
            if has_cap('light') or has_cap('switch'):
                if command in ['on', 'off', 'toggle']:
                    h = get_handler(0x0006)
                    if h:
                        if command == 'on':
                            await h.turn_on()
                            optimistic_state['state'] = 'ON'
                            optimistic_state['on'] = True
                        elif command == 'off':
                            # Extract transition from data dict
                            transition = data.get('transition') if data else None
                            transition_time = int(transition * 10) if transition else None
                            await h.turn_off(transition_time=transition_time)
                            optimistic_state['state'] = 'OFF'
                            optimistic_state['on'] = False
                        else:
                            await h.toggle()
                            # Toggle: invert current state
                            current = self.state.get('on', False)
                            optimistic_state['state'] = 'OFF' if current else 'ON'
                            optimistic_state['on'] = not current
                        success = True

                elif command == 'brightness' and value is not None:
                    h = get_handler(0x0008)
                    if h:
                        await h.set_brightness_pct(int(value))
                        # Store both formats for compatibility
                        optimistic_state['brightness'] = int(value * 2.54) if value <= 100 else int(value)
                        optimistic_state['level'] = int(value) if value <= 100 else int(value / 2.54)
                        # Brightness > 0 implies ON
                        if value > 0:
                            optimistic_state['state'] = 'ON'
                            optimistic_state['on'] = True
                        success = True

                elif command == 'color_temp' and value is not None:
                    h = get_handler(0x0300)
                    if h:
                        await h.set_color_temp_kelvin(int(value))
                        # Convert Kelvin to mireds for state
                        mireds = int(1000000 / value) if value > 0 else 250
                        optimistic_state['color_temp'] = mireds
                        optimistic_state['color_temp_mireds'] = mireds
                        success = True

            # AQARA MANUFACTURER CLUSTER COMMANDS (0xFCC0)
            if not success and command in ['window_detection', 'valve_detection', 'motor_calibration', 'child_lock']:
                h = get_handler(0xFCC0)
                if h and hasattr(h, 'process_command'):
                    h.process_command(command, value)
                    success = True
                    # Optimistic updates
                    if command == 'motor_calibration':
                        optimistic_state['motor_calibration'] = 'calibrating' if value else 'idle'
                    else:
                        optimistic_state[command] = bool(value)

            # HVAC COMMANDS - route to handler if process_command exists
            if not success and command in ['temperature', 'system_mode']:
                h = get_handler(0x0201)
                if h:
                    if hasattr(h, 'process_command'):
                        h.process_command(command, value)
                        success = True
                        # Optimistic updates
                        if command == 'temperature':
                            optimistic_state['temperature_setpoint'] = float(value)
                        elif command == 'system_mode':
                            optimistic_state['system_mode'] = str(value).lower()
                    elif command == 'temperature':
                        # Fallback for compatibility with Hive receivers
                        await h.set_heating_setpoint(float(value))
                        optimistic_state['temperature_setpoint'] = float(value)
                        success = True

            # GENERAL COMMANDS (Fallthrough)
            elif not success and command == 'identify':
                h = get_handler(0x0003)
                if h:
                    await h.identify(5)
                    success = True

            # COVER COMMANDS
            if not success and (has_cap('cover') or command in ['open', 'close', 'stop', 'position']):
                h = get_handler(0x0102)
                if h:
                    if command == 'open':
                        await h.open()
                        optimistic_state['position'] = 100
                        optimistic_state['state'] = 'open'
                    elif command == 'close':
                        await h.close()
                        optimistic_state['position'] = 0
                        optimistic_state['state'] = 'closed'
                    elif command == 'stop':
                        await h.stop()
                    elif command == 'position' and value is not None:
                        await h.set_position(int(value))
                        optimistic_state['position'] = int(value)
                    success = True

            # =========================================================================
            # OPTIMISTIC STATE UPDATE
            # =========================================================================
            if success and optimistic_state:
                logger.info(f"[{self.ieee}] Optimistic update: {optimistic_state}")
                # This triggers handle_device_update AND _publish_json_state
                self.update_state(optimistic_state, endpoint_id=endpoint_id)

            return success

        except Exception as e:
            packet_stats.record_error(self.ieee)  # Track error
            raise

    async def read_attribute_raw(self, ep_id: int, cluster_id: int, attr_name: str) -> Any:
        ep = self.zigpy_dev.endpoints.get(ep_id)
        if not ep: raise ValueError(f"EP {ep_id} not found")
        c = ep.in_clusters.get(cluster_id) or ep.out_clusters.get(cluster_id)
        if not c: raise ValueError(f"Cluster 0x{cluster_id:04x} not found")
        res = await c.read_attributes([attr_name])
        return res[0][attr_name] if res and attr_name in res[0] else None

    def handle_raw_message(self, cluster_id: int, message: bytes):
        h = self.handlers.get(cluster_id)
        if h and hasattr(h, 'handle_raw_data'): h.handle_raw_data(message)

    def get_role(self) -> str:
        d = self.zigpy_dev
        if self.service.app.state.node_info.ieee == d.ieee: return "Coordinator"
        if "_TZE" in str(d.manufacturer): return "Router"
        if hasattr(d, 'node_desc') and d.node_desc:
            return "Router" if d.node_desc.logical_type == 1 else "EndDevice"
        return "EndDevice"

    def get_binding_preferences(self) -> Dict[str, Dict[int, int]]:
        """Get device-specific binding endpoint preferences."""
        model = str(self.zigpy_dev.model or "").upper()
        if "SLT6" in model: return {'source_endpoints': {0x0201: 9}}
        if "SLR" in model or "RECEIVER" in model: return {'target_endpoints': {0x0201: 5}}
        return {}

    def get_device_config_schema(self) -> List[Dict]:
        schema = []
        seen = set()
        for h in self.handlers.values():
            if h in seen: continue
            seen.add(h)
            if hasattr(h, 'get_configuration_options'):
                opts = h.get_configuration_options()
                if opts: schema.extend(opts)

        unique = []
        keys = set()
        for o in schema:
            if o['name'] not in keys: unique.append(o); keys.add(o['name'])
        return unique

    def emit_event(self, event_type: str, event_data: Dict[str, Any]):
        self.service._emit_sync("device_event", {"ieee": self.ieee, "event_type": event_type, "data": event_data})

    def get_device_discovery_configs(self) -> List[Dict]:
        """Aggregate HA discovery configs from all handlers."""
        configs = []
        seen_handlers = set()

        # === FIX: Single Device Info Block ===
        device_info = {
            "identifiers": [self.ieee],
            "name": self.state.get("manufacturer", "Zigbee") + " " + self.state.get("model", "Device"),
            "model": self.state.get("model", "Unknown"),
            "manufacturer": self.state.get("manufacturer", "Unknown")
        }

        for handler in self.handlers.values():
            if handler in seen_handlers: continue
            seen_handlers.add(handler)

            if hasattr(handler, 'get_discovery_configs'):
                c = handler.get_discovery_configs()
                if c:
                    for config in c:
                        if "device" not in config:
                            config["device"] = device_info

                        # === APPLY JSON SCHEMA DEFAULTS ===
                        self._apply_json_schema(config)

                    configs.extend(c)

        # Add generic Link Quality sensor
        configs.append({
            "component": "sensor",
            "object_id": "linkquality",
            "unique_id": f"{self.ieee}_linkquality",
            "device": device_info,
            "config": {
                "name": "Link Quality",
                "unit_of_measurement": "lqi",
                "value_template": "{{ value_json.lqi }}",
                "state_class": "measurement",
                "icon": "mdi:signal"
            }
        })
        return configs


    def _apply_json_schema(self, payload: Dict):
        """
        Helper to enforce JSON schema on Light/Cover configs.
        """
        component = payload.get('component')
        if component not in ['light', 'cover']:
            return

        config = payload.get('config', payload)

        if component == "light":
            if 'schema' not in config:
                config['schema'] = 'json'

            if config.get('schema') == 'json':
                keys_to_remove = [
                    'payload_on', 'payload_off', 'value_template',
                    'brightness_state_topic', 'brightness_command_topic',
                    'brightness_value_template', 'brightness_command_template',
                    'color_temp_state_topic', 'color_temp_command_topic',
                    'color_temp_value_template', 'color_temp_command_template'
                ]
                for key in keys_to_remove:
                    config.pop(key, None)

            if 'command_topic' not in config and 'state_topic' in config:
                config['command_topic'] = config['state_topic'] + "/set"

        elif component == "cover":
            # Command payloads
            if 'payload_open' not in config:
                config['payload_open'] = '{"command": "open"}'
            if 'payload_close' not in config:
                config['payload_close'] = '{"command": "close"}'
            if 'payload_stop' not in config:
                config['payload_stop'] = '{"command": "stop"}'

            # Position commands
            if 'set_position_template' not in config:
                config['set_position_template'] = '{"command": "position", "value": {{ position }}}'

            # State reading - CRITICAL for HA to show state
            if 'value_template' not in config:
                config['value_template'] = "{{ 'open' if value_json.is_open else 'closed' }}"
            if 'position_template' not in config:
                config['position_template'] = "{{ value_json.cover_position | default(value_json.position | default(0)) }}"


    def cleanup(self):
        """Cancel timers and cleanup on device removal."""
        if hasattr(self, '_motion_clear_task') and self._motion_clear_task:
            self._motion_clear_task.cancel()
            logger.debug(f"[{self.ieee}] Cancelled motion timer")