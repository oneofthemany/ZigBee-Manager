"""
ZHA Device Wrapper - Handles cluster handlers and state management.
Based on ZHA's device architecture.
"""
import time
import logging
import asyncio
from typing import Dict, Any, Optional, List

# Import the registry from handlers package
from handlers.base import HANDLER_REGISTRY
from error_handler import with_retries, CommandWrapper
from device_capabilities import DeviceCapabilities
from zigpy.zcl.clusters.general import Basic

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

logger = logging.getLogger("device")

# How long before a device is considered unavailable (5 minutes)
CONSIDER_UNAVAILABLE_BATTERY = 60 * 60 * 6  # 6 hours for battery devices
CONSIDER_UNAVAILABLE_MAINS = 60 * 60 * 2    # 2 hours for mains-powered devices


class ZHADevice:
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
        # Ensure 'state' has manufacturer info from the start
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
        """
        Actively purges invalid fields from self.state based on current capabilities.
        This fixes the 'occupancy: false' on lights issue by cleaning memory.
        """
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

    def _identify_handlers(self):
        """Scan device endpoints and attach appropriate cluster handlers."""
        self.handlers.clear()
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

                        # direction = "Input/Server" if is_server else "Output/Client"
                        # logger.debug(f"[{self.ieee}] Attached {handler_cls.__name__} for EP{ep_id} 0x{cid:04x}")
                    except Exception as e:
                        logger.error(f"[{self.ieee}] Failed to attach handler for EP{ep_id} 0x{cid:04x}: {e}")

            for cluster in ep.in_clusters.values(): attach_handler(cluster, is_server=True)
            for cluster in ep.out_clusters.values(): attach_handler(cluster, is_server=False)

        if hasattr(self, 'capabilities'):
            self.capabilities._detect_capabilities()
            self.sanitize_state()

    def restore_state(self, cached_state: Dict[str, Any]):
        """
        Restore state from cache on startup.
        Purges invalid data and syncs cleanup back to the Service cache.
        """
        if cached_state:
            # 1. Load the raw cached state
            self.state.update(cached_state)

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
        """
        Update device state and notify the service.
        Includes smart duplicate detection logic and capability filtering.
        """
        # === FILTERING ===
        if hasattr(self, 'capabilities'):
            data = self.capabilities.filter_state_update(data)

        if not data:
            return

        changed = {}
        duplicates_detected = []

        for k, v in data.items():
            # --- DUPLICATE DETECTION START ---
            if endpoint_id is not None:
                if k not in self._attribute_sources: self._attribute_sources[k] = {}
                self._attribute_sources[k][endpoint_id] = time.time()

                if len(self._attribute_sources[k]) > 1:
                    if k in self._preferred_endpoints:
                        if endpoint_id != self._preferred_endpoints[k]: continue

                    # Outlier detection (ignore 0 if we have better data)
                    if (isinstance(v, (int, float)) and v == 0):
                        has_better = any(eid != endpoint_id for eid in self._attribute_sources[k])
                        if has_better:
                            duplicates_detected.append({"attribute": k, "value": v, "endpoint": endpoint_id, "reason": "outlier_zero"})
                            continue
                            # --- DUPLICATE DETECTION END ---

            always_report = ['occupancy', 'presence', 'motion', 'contact', 'alarm',
                             'tamper', 'battery_low', 'vibration', 'on_with_timed_off', 'action']

            if k in always_report or self.state.get(k) != v:
                changed[k] = v

        # --- INTELLIGENT STATE MERGING ---
        if self.capabilities.has_capability('light'):
            # List of light-related attributes that should trigger state inclusion
            light_attrs = ['state', 'on', 'brightness', 'level', 'color_temp', 'color_temperature',
                           'color_temperature_mireds', 'color_temp_kelvin', 'hue', 'saturation', 'x', 'y']

            # Check if ANY light attribute is being updated
            has_light_update = any(k in light_attrs or any(k.startswith(f"{attr}_") for attr in light_attrs)
                                   for k in data.keys())

            if has_light_update:
                # CRITICAL: Always include state when publishing light updates
                if 'state' not in changed and 'state' in self.state:
                    changed['state'] = self.state['state']
                if 'on' not in changed and 'on' in self.state:
                    changed['on'] = self.state['on']

                # Also include other light attributes to maintain consistency
                if 'brightness' in self.state and 'brightness' not in data:
                    changed['brightness'] = self.state['brightness']
                if 'level' in self.state and 'level' not in data:
                    changed['level'] = self.state['level']
                if 'color_temp' in self.state and 'color_temp' not in data:
                    changed['color_temp'] = self.state['color_temp']

        # Handle multi-endpoint devices - add endpoint-specific state fields
        if endpoint_id is not None and self.capabilities.has_capability('light'):
            state_key = f"state_{endpoint_id}"
            on_key = f"on_{endpoint_id}"

            # Ensure endpoint-specific state is present
            if state_key in self.state and state_key not in changed:
                changed[state_key] = self.state[state_key]
            if on_key in self.state and on_key not in changed:
                changed[on_key] = self.state[on_key]

        self.state.update(data)

        # Ensure Manufacturer/Model are present in every update to keep cache sync complete
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
            self.service.handle_device_update(self, changed, qos=qos, endpoint_id=endpoint_id)

            if duplicates_detected:
                self.service._emit_sync("duplicate_attribute_warning", {
                    "ieee": self.ieee, "details": duplicates_detected
                })

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
        if role == "Coordinator": return True
        if self.last_seen == 0: return False

        elapsed = (time.time() * 1000) - self.last_seen
        threshold = CONSIDER_UNAVAILABLE_BATTERY if self._is_battery_powered() else CONSIDER_UNAVAILABLE_MAINS
        return elapsed < (threshold * 1000)

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
        """Configure cluster handlers (bindings/reporting) and apply settings."""
        logger.info(f"[{self.ieee}] Configuring device...")

        # Fast Path: Targeted Updates
        if config and config.get('updates'):
            updates = config['updates']
            # Tuya
            tuya = self.handlers.get(0xEF00)
            if tuya: await tuya.apply_settings(updates)

            # Startup Behavior
            for key, val in updates.items():
                if key.startswith("startup_behavior_"):
                    try:
                        ep = int(key.split("_")[-1])
                        h = self.handlers.get((ep, 0x0006))
                        if h: await h.cluster.write_attributes({0x4003: int(val)})
                    except: pass

            # PIR Settings
            occ = self.handlers.get(0x0406)
            if occ:
                if 'motion_timeout' in updates:
                    try: await occ.cluster.write_attributes({0x0010: int(updates['motion_timeout'])})
                    except: pass
                if 'sensitivity' in updates:
                    try: await occ.cluster.write_attributes({0x0030: int(updates['sensitivity'])})
                    except: pass
            return

        # Full Config
        if config and 'tuya_settings' in config:
            tuya = self.handlers.get(0xEF00)
            if tuya: await tuya.apply_settings(config['tuya_settings'])

        configured = set()
        for h in self.handlers.values():
            if h in configured: continue
            try:
                await h.configure()
                configured.add(h)
            except Exception as e:
                logger.warning(f"[{self.ieee}] Config failed for 0x{h.cluster_id:04x}: {e}")

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
    async def send_command(self, command: str, value: Any = None, endpoint_id: Optional[int] = None) -> Any:
        """Send a command to the device."""
        logger.info(f"[{self.ieee}] CMD: {command}={value} EP={endpoint_id}")
        command = command.lower()

        def get_handler(cid):
            if endpoint_id:
                if (endpoint_id, cid) in self.handlers: return self.handlers[(endpoint_id, cid)]
            return self.handlers.get(cid)

        if command in ['on', 'off', 'toggle']:
            h = get_handler(0x0006)
            if h:
                if command == 'on': await h.turn_on()
                elif command == 'off': await h.turn_off()
                else: await h.toggle()
                return True
        elif command == 'brightness' and value is not None:
            h = get_handler(0x0008)
            if h: await h.set_brightness_pct(int(value)); return True
        elif command == 'color_temp' and value is not None:
            h = get_handler(0x0300)
            if h: await h.set_color_temp_kelvin(int(value)); return True
        elif command == 'temperature' and value is not None:
            h = get_handler(0x0201)
            if h: await h.set_heating_setpoint(float(value)); return True
        elif command == 'identify':
            h = get_handler(0x0003)
            if h: await h.identify(5); return True
        elif command in ['open', 'close', 'stop']:
            h = get_handler(0x0102)
            if h:
                if command == 'open': await h.open()
                elif command == 'close': await h.close()
                else: await h.stop()
                return True
        elif command == 'position' and value is not None:
            h = get_handler(0x0102)
            if h: await h.set_position(int(value)); return True

        return False

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
        # This ensures all entities (light, switch, sensor) are grouped under ONE device in HA
        device_info = {
            "identifiers": [self.ieee], # Use IEEE as the unique device ID
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
                    # Inject standardized device_info into each entity config
                    for config in c:
                        if "device" not in config:
                            config["device"] = device_info
                    configs.extend(c)

        # Add generic Link Quality sensor, properly linked to the device
        configs.append({
            "component": "sensor",
            "object_id": "linkquality",
            "unique_id": f"{self.ieee}_linkquality",
            "device": device_info,
            "config": {
                "name": "Link Quality",
                "unit_of_measurement": "lqi",
                "value_template": "{{ value_json.lqi }}",
                "state_class": "measurement",  # Optional: allows graphing in HA
                "icon": "mdi:signal"           # Optional: nice icon
            }
        })
        return configs