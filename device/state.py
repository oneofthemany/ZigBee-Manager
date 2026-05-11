"""
Device State Manager Mixin
Handles device state, caching, sanitization, and availability.
"""
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("device.state")

CONSIDER_UNAVAILABLE_BATTERY = 60 * 60 * 25   # 25 hours for battery devices
CONSIDER_UNAVAILABLE_MAINS = 60 * 60 * 25     # 25 hours for mains-powered devices
CONSIDER_UNAVAILABLE_PASSIVE = 60 * 60 * 72   # 72 hours for passive-only sensors

class DeviceStateManagerMixin:

    def sanitise_state(self):
        """Purges invalid fields from self.state based on current capabilities."""
        INFRASTRUCTURE_PREFIXES = ('cluster_0019_', 'cluster_0021_', 'cluster_0020_', 'cluster_000a_')
        infra_keys = [k for k in self.state if k.startswith(INFRASTRUCTURE_PREFIXES)]
        if infra_keys:
            logger.info(f"[{self.ieee}] 🧹 Purging infrastructure keys: {infra_keys}")
            for k in infra_keys: del self.state[k]

        OPPLE_PARSED_ATTRS = ('opple_0x00dc', 'opple_0x00df', 'opple_0x00e5', 'opple_0x00f7', 'opple_0x00ee', 'opple_0x0271', 'opple_0x0275', 'opple_0x027b', 'opple_0x027e', 'opple_0x0280', 'opple_0x040a')
        opple_stale = [k for k in self.state if k in OPPLE_PARSED_ATTRS]
        if opple_stale:
            logger.info(f"[{self.ieee}] 🧹 Purging stale opple struct keys: {opple_stale}")
            for k in opple_stale: del self.state[k]

        XIAOMI_BASIC_RAW = ('attr_0000_ff01', 'attr_0000_ff02')
        xiaomi_stale = [k for k in self.state if k in XIAOMI_BASIC_RAW]
        if xiaomi_stale:
            logger.info(f"[{self.ieee}] 🧹 Purging stale Xiaomi Basic keys: {xiaomi_stale}")
            for k in xiaomi_stale: del self.state[k]

        if not hasattr(self, 'capabilities'): return

        keys_to_remove = [key for key in self.state if not self.capabilities.allows_field(key)]
        if keys_to_remove:
            logger.info(f"[{self.ieee}] 🧹 SANITISING STATE: Removing unsupported keys: {keys_to_remove}")
            for key in keys_to_remove: del self.state[key]

        if (infra_keys or keys_to_remove) and hasattr(self, 'service') and hasattr(self.service, 'state_cache'):
            self.service.state_cache[self.ieee] = self.state.copy()
            self.service._cache_dirty = True

    def restore_state(self, cached_state):
        """Restore device state from cache."""
        if cached_state:
            self.state.update(cached_state)
            if any(k in cached_state for k in ['occupancy', 'motion', 'presence']):
                self.state['occupancy'] = False
                self.state['motion'] = False
                self.state['presence'] = False

            if self.zigpy_dev.manufacturer and ('manufacturer' not in self.state or self.state['manufacturer'] == 'Unknown'):
                self.state['manufacturer'] = str(self.zigpy_dev.manufacturer)
                self.manufacturer = self.zigpy_dev.manufacturer

            if self.zigpy_dev.model and ('model' not in self.state or self.state['model'] == 'Unknown'):
                self.state['model'] = str(self.zigpy_dev.model)
                self.model = self.zigpy_dev.model

            self.sanitise_state()

            if hasattr(self.service, 'state_cache'):
                self.service.state_cache[self.ieee] = self.state.copy()
                self.service._cache_dirty = True

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
        if hasattr(self, 'capabilities') and hasattr(self.capabilities, 'filter_state_update'):
            data = self.capabilities.filter_state_update(data)
        if not data: return

        TEMP_ALIASES = ('local_temperature', 'internal_temperature', 'current_temperature')
        if 'temperature' not in data:
            for alias in TEMP_ALIASES:
                v = data.get(alias)
                if v is not None and v != 0 and v != 0x8000:
                    data['temperature'] = v
                    break

        changed = {}
        duplicates_detected = []
        ALWAYS_REPORT = {'occupancy', 'presence', 'motion', 'contact', 'alarm', 'temperature', 'local_temperature', 'humidity', 'pressure', 'illuminance', 'pi_heating_demand', 'running_state', 'battery', 'battery_voltage', 'tamper', 'battery_low', 'vibration', 'on_with_timed_off', 'action'}
        ALWAYS_REPORT_PREFIXES = ('voltage_', 'current_', 'power_', 'local_temperature_', 'temperature_', 'humidity_', 'pressure_', 'illuminance_', 'battery_')

        for k, v in data.items():
            if endpoint_id is not None:
                if k not in self._attribute_sources:
                    self._attribute_sources[k] = {}
                self._attribute_sources[k][endpoint_id] = time.time()
                if len(self._attribute_sources[k]) > 1:
                    if k in self._preferred_endpoints:
                        if endpoint_id != self._preferred_endpoints[k]: continue
                    if isinstance(v, (int, float)) and v == 0:
                        has_better = any(eid != endpoint_id for eid in self._attribute_sources[k])
                        if has_better:
                            duplicates_detected.append({"attribute": k, "value": v, "endpoint": endpoint_id, "reason": "outlier_zero"})
                            continue

            if (k in ALWAYS_REPORT or k.startswith(ALWAYS_REPORT_PREFIXES) or self.state.get(k) != v):
                changed[k] = v

        has_light = hasattr(self, 'capabilities') and hasattr(self.capabilities, 'has_capability') and self.capabilities.has_capability('light')
        if has_light:
            light_attrs = {'state', 'on', 'brightness', 'level', 'color_temp', 'color_temperature', 'color_temperature_mireds', 'color_temp_kelvin', 'hue', 'saturation', 'x', 'y'}
            if any(k in light_attrs or any(k.startswith(f"{attr}_") for attr in light_attrs) for k in data.keys()):
                for attr in ['state', 'on', 'brightness', 'level', 'color_temp']:
                    if attr not in changed and attr in self.state: changed[attr] = self.state[attr]
                if endpoint_id is not None:
                    for attr in ['state', 'on']:
                        key = f"{attr}_{endpoint_id}"
                        if key in self.state and key not in changed: changed[key] = self.state[key]

        self.state.update(data)
        if 'manufacturer' not in self.state and self.manufacturer: self.state['manufacturer'] = str(self.manufacturer)
        if 'model' not in self.state and self.model: self.state['model'] = str(self.model)
        if 'last_seen' not in data:
            self.last_seen = int(time.time() * 1000)
            self.state['last_seen'] = self.last_seen

        self._available = True

        if changed:
            changed['last_seen'] = self.last_seen
            self.service.state_cache[self.ieee] = self.state.copy()
            self.service._cache_dirty = True
            self.service._schedule_save()
            self.service.handle_device_update(self, changed, qos=qos, endpoint_id=endpoint_id)

            collector = getattr(self.service, "telemetry_collector", None)
            if collector is not None:
                try: collector.record_state_change(self.ieee, changed)
                except Exception as e: logger.debug(f"[{self.ieee}] telemetry record skipped: {e}")

            if has_light: self._publish_json_state(changed, endpoint_id)
            if duplicates_detected: self.service._emit_sync("duplicate_attribute_warning", {"ieee": self.ieee, "details": duplicates_detected})

        import asyncio
        if self._pending_configure and not self._awake_proof_received:
            self._awake_proof_received = True
            self._pending_configure = False
            asyncio.create_task(self.configure())

    def set_preferred_endpoint(self, attribute: str, endpoint_id: int):
        """Pin a specific endpoint for an attribute."""
        self._preferred_endpoints[attribute] = endpoint_id
        if self.ieee not in self.service.device_settings:
            self.service.device_settings[self.ieee] = {}
        self.service.device_settings[self.ieee]['preferred_endpoints'] = self._preferred_endpoints
        self.service._save_json("./data/device_settings.json", self.service.device_settings)
        logger.info(f"[{self.ieee}] Pinned {attribute} to Endpoint {endpoint_id}")

    def is_available(self) -> bool:
        role = self.get_role()
        if role == "Coordinator": return True
        if self.last_seen == 0: return False

        elapsed = (time.time() * 1000) - self.last_seen
        if self._is_passive_device(): threshold = CONSIDER_UNAVAILABLE_PASSIVE
        elif self._is_battery_powered(): threshold = CONSIDER_UNAVAILABLE_BATTERY
        else: threshold = CONSIDER_UNAVAILABLE_MAINS

        return elapsed < (threshold * 1000)

    def _is_passive_device(self) -> bool:
        passive_capabilities = {'occupancy', 'motion', 'presence', 'contact', 'water_leak', 'vibration', 'smoke', 'gas', 'tamper', 'sos'}
        device_caps = set(self.capabilities.get_capabilities())
        has_passive = device_caps & passive_capabilities
        has_active = device_caps & {'temperature', 'humidity', 'battery'}
        return bool(has_passive) and not has_active

    @property
    def is_battery(self) -> bool:
        return self._is_battery_powered()

    def _is_battery_powered(self) -> bool:
        is_battery = True
        role = self.get_role()
        if role in ["Router", "Coordinator"]: is_battery = False

        try:
            for ep_id, ep in self.zigpy_dev.endpoints.items():
                if ep_id == 0: continue
                in_cl = getattr(ep, 'in_clusters', None) or {}
                out_cl = getattr(ep, 'out_clusters', None) or {}
                if 0x0021 in in_cl or 0x0021 in out_cl:
                    is_battery = False
                    break
        except Exception: pass

        model = str(self.model).lower() if self.model else ""
        if any(x in model for x in ['plug', 'socket', 'outlet', 'switch', 'light', 'bulb']): is_battery = False

        current = self.state.get('power_source', 'Unknown')
        if not is_battery: self.state['power_source'] = 'Mains'
        elif is_battery and current == 'Unknown': self.state['power_source'] = 'Battery'

        return is_battery
