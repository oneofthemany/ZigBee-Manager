"""
Device Lifecycle Mixin
Handles device joining, initialization, updates, and removal.
"""
import logging
import asyncio
from typing import Dict, Any

logger = logging.getLogger("core.lifecycle")

class DeviceLifecycleMixin:

    def device_joined(self, device):
        """Called when a device joins the network."""
        ieee = str(device.ieee)
        logger.info(f"[{ieee}] Device joined network")
        self._emit_sync("device_joined", {
            "ieee": ieee,
            "nwk": hex(device.nwk),
            "model": device.model or "Unknown",
            "manufacturer": device.manufacturer or "Unknown"
        })

    def device_initialized(self, device):
        """Called when a device is fully initialized by zigpy."""
        ieee = str(device.ieee)
        logger.info(f"[{ieee}] Device initialized")
        asyncio.create_task(self._async_device_initialized(device))

    async def _async_device_initialized(self, device):
        ieee = str(device.ieee)
        if ieee not in self.devices:
            from device import ZigManDevice
            zdev = ZigManDevice(self, device)
            self.devices[ieee] = zdev
            if ieee in self.state_cache:
                zdev.restore_state(self.state_cache[ieee])

            # Defer configuring handlers to avoid blocking startup
            await asyncio.sleep(2)
            await zdev.configure()

        await self.announce_device(ieee)
        self._emit_sync("device_initialized", {
            "ieee": ieee,
            "model": device.model,
            "manufacturer": device.manufacturer
        })

    def raw_device_initialized(self, device):
        pass

    def device_removed(self, device):
        """Called when a device leaves the network."""
        ieee = str(device.ieee)
        logger.info(f"[{ieee}] Device left network")
        if ieee in self.devices:
            self.devices[ieee].cleanup()
            del self.devices[ieee]
            if ieee in self.state_cache:
                del self.state_cache[ieee]
                self._cache_dirty = True
            self._emit_sync("device_removed", {"ieee": ieee})

    def handle_device_update(self, zha_device, changed_data: Dict[str, Any], full_state=None, qos=None, endpoint_id=None):
        """Route state updates to MQTT and automation engine."""
        ieee = zha_device.ieee
        safe_name = self.get_safe_name(ieee)

        if changed_data:
            self._debounced_device_update(ieee, changed_data)

        if self.mqtt:
            from modules.json_helpers import sanitise_device_state
            state_to_publish = full_state if full_state else zha_device.state.copy()
            state_to_publish = sanitise_device_state(state_to_publish)

            if endpoint_id is not None:
                if f'state_{endpoint_id}' in state_to_publish and 'state' not in state_to_publish:
                    state_to_publish['state'] = state_to_publish[f'state_{endpoint_id}']

            if 'state_1' in state_to_publish and 'state' not in state_to_publish:
                state_to_publish['state'] = state_to_publish['state_1']
            elif 'state_11' in state_to_publish and 'state' not in state_to_publish:
                state_to_publish['state'] = state_to_publish['state_11']

            try:
                import json
                asyncio.create_task(self.mqtt.publish(safe_name, json.dumps(state_to_publish), ieee=ieee, qos=qos))
            except Exception as e:
                logger.error(f"[{ieee}] Publish failed: {e}")

        # Map to unified events for websockets
        event_payload = {
            "ieee": ieee,
            "friendly_name": self.friendly_names.get(ieee, ieee),
            "state": changed_data,
            "full_state": zha_device.state,
            "available": zha_device.is_available()
        }
        self._emit_sync("device_updated", event_payload)
