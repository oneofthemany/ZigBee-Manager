"""
Zigbee Manager Device Wrapper - Handles cluster handlers and state management.
Proposed Refactored Version.

This version uses mixins to keep the core class clean while retaining the identical API.
"""
import time
import logging
from typing import Dict, Any

from device.state import DeviceStateManagerMixin
from device.handlers import DeviceHandlerManagerMixin
from device.commands import DeviceCommandExecutorMixin
from device.discovery import DeviceDiscoveryProviderMixin

from modules.device_capabilities import DeviceCapabilities
from modules.error_handler import CommandWrapper

logger = logging.getLogger("device")


class ZigManDevice(
    DeviceStateManagerMixin,
    DeviceHandlerManagerMixin,
    DeviceCommandExecutorMixin,
    DeviceDiscoveryProviderMixin,
):
    """
    Wrapper around a zigpy device that manages cluster handlers
    and state aggregation. Refactored to use focused mixins.
    """

    def __init__(self, service, zigpy_dev):
        self.service = service
        self.zigpy_dev = zigpy_dev
        self.ieee = str(zigpy_dev.ieee)

        # Handlers stored by (endpoint_id, cluster_id) tuple
        self.handlers: Dict[Any, Any] = {}

        self.state: Dict[str, Any] = {}

        self._pending_configure = False
        self._awake_proof_received = False

        # Initialize basic info from Zigpy device
        self.manufacturer = zigpy_dev.manufacturer
        self.model = zigpy_dev.model

        # Hydrate from state cache if zigpy DB lost them (sleepy devices, partial interviews)
        cached = self.service.state_cache.get(self.ieee, {}) if hasattr(self.service, 'state_cache') else {}
        if not self.manufacturer and cached.get('manufacturer') and cached['manufacturer'] != 'Unknown':
            self.manufacturer = cached['manufacturer']
            try: zigpy_dev.manufacturer = self.manufacturer
            except Exception: pass
        if not self.model and cached.get('model') and cached['model'] != 'Unknown':
            self.model = cached['model']
            try: zigpy_dev.model = self.model
            except Exception: pass

        if not self.model or not self.manufacturer:
            for ep_id, ep in zigpy_dev.endpoints.items():
                if ep_id == 0:
                    continue
                basic = (getattr(ep, 'in_clusters', {}) or {}).get(0x0000)
                if not basic:
                    continue
                cache = getattr(basic, '_attr_cache', {}) or {}
                if not self.model and 0x0005 in cache:
                    v = cache[0x0005]
                    self.model = str(getattr(v, 'value', v))
                    try: zigpy_dev.model = self.model
                    except Exception: pass
                if not self.manufacturer and 0x0004 in cache:
                    v = cache[0x0004]
                    self.manufacturer = str(getattr(v, 'value', v))
                    try: zigpy_dev.manufacturer = self.manufacturer
                    except Exception: pass

        # Initialize to 0 so devices appear Offline until they communicate
        self.last_seen = 0

        self.quirk_name = "None"
        self._available = True

        # Track sources of attributes to detect duplicates
        self._attribute_sources: Dict[str, Dict[int, float]] = {}

        # User preferences for specific endpoints (loaded from settings)
        self._preferred_endpoints: Dict[str, int] = {}

        # Command wrapper for resilient operations
        self._cmd_wrapper = None

        # Check if quirk is applied
        if hasattr(zigpy_dev, 'quirk_class'):
            self.quirk_name = zigpy_dev.quirk_class.__name__

        # Identify and attach handlers (from DeviceHandlerManagerMixin)
        self._identify_handlers()

        # Initialize Capabilities Logic
        self.capabilities = DeviceCapabilities(self)

        # Initialize command wrapper
        try:
            self._cmd_wrapper = CommandWrapper(self)
        except Exception as e:
            logger.debug(f"[{self.ieee}] Could not create command wrapper: {e}")

        # Load preferred endpoints from settings if available
        if self.ieee in self.service.device_settings:
            settings = self.service.device_settings[self.ieee]
            if isinstance(settings, dict) and 'preferred_endpoints' in settings:
                self._preferred_endpoints = settings['preferred_endpoints']

        # Schedule query only if absolutely nothing is known
        if self.manufacturer is None or self.model is None:
            self._schedule_basic_info_query()

        # Perform initial cleanup of state (from DeviceStateManagerMixin)
        self.sanitise_state()

        logger.info(f"[{self.ieee}] Device wrapper created - "
                    f"Model: {self.model}, Manufacturer: {self.manufacturer}, "
                    f"Quirk: {self.quirk_name}")

    def get_role(self) -> str:
        d = self.zigpy_dev
        if self.service.app.state.node_info.ieee == d.ieee:
            return "Coordinator"
        if "_TZE" in str(d.manufacturer):
            return "Router"

        if hasattr(d, 'node_desc') and d.node_desc:
            role = "Router" if d.node_desc.logical_type == 1 else "EndDevice"
            return role

        return "EndDevice"

    def get_details(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee,
            "manufacturer": str(self.manufacturer) if self.manufacturer else "Unknown",
            "model": str(self.model) if self.model else "Unknown",
            "quirk": self.quirk_name
        }
    def emit_event(self, event_type: str, event_data: Dict[str, Any]):
        self.service._emit_sync("device_event", {"ieee": self.ieee, "event_type": event_type, "data": event_data})

    def cleanup(self):
        """Cancel timers and cleanup on device removal."""
        if hasattr(self, '_motion_clear_task') and self._motion_clear_task:
            self._motion_clear_task.cancel()
            logger.debug(f"[{self.ieee}] Cancelled motion timer")
