"""
Device Handler Manager Mixin
Handles cluster handler identification, configuration, polling, and zombie cleanup.
"""
import logging
import asyncio
from typing import Dict, Any

from handlers.base import HANDLER_REGISTRY, ClusterHandler

# fire and populate the HANDLER_REGISTRY. Without them, no devices will work!
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
from handlers.generic import *
from handlers.poll_control import *

logger = logging.getLogger("device.handlers")

SKIP_GENERIC_CLUSTERS = {
    0x0019,  # OTA Upgrade
    0x0021,  # Green Power Proxy
    0x000A,  # Time
}

class DeviceHandlerManagerMixin:

    def _detach_handlers(self):
        """Aggressively clean up listeners to prevent 'Zombie Handler' duplication."""
        if self.handlers:
            for handler in self.handlers.values():
                if hasattr(handler, 'cluster') and handler.cluster:
                    if handler in handler.cluster._listeners:
                        handler.cluster._listeners.remove(handler)
            self.handlers.clear()

        cleaned_count = 0
        try:
            for ep_id, ep in self.zigpy_dev.endpoints.items():
                if ep_id == 0: continue

                all_clusters = []
                if hasattr(ep, 'in_clusters'): all_clusters.extend(ep.in_clusters.values())
                if hasattr(ep, 'out_clusters'): all_clusters.extend(ep.out_clusters.values())

                for cluster in all_clusters:
                    if not hasattr(cluster, '_listeners'): continue
                    current_listeners = list(cluster._listeners)
                    for listener in current_listeners:
                        is_zombie = isinstance(listener, ClusterHandler) or (hasattr(listener, '__module__') and 'handlers' in listener.__module__)
                        if is_zombie and listener in cluster._listeners:
                            cluster._listeners.remove(listener)
                            cleaned_count += 1
        except Exception as e:
            logger.error(f"[{self.ieee}] Error during zombie cleanup: {e}")

        if cleaned_count > 0:
            logger.warning(f"[{self.ieee}] 🧟 Removed {cleaned_count} zombie handlers from zigpy clusters")

    def get_handlers_info(self) -> Dict:
        return {
            "count": len(self.handlers),
            "clusters": [f"0x{k[1] if isinstance(k, tuple) else k:04x}" for k in self.handlers.keys()]
        }

    def _identify_handlers(self):
        """Scan device endpoints and attach appropriate cluster handlers."""
        self._detach_handlers()

        binding_prefs = self.get_binding_preferences()
        preferred_endpoints = {}
        if 'target_endpoints' in binding_prefs:
            for cluster_id, ep_id in binding_prefs['target_endpoints'].items():
                preferred_endpoints[cluster_id] = ep_id

        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        if "philips" in manufacturer or "signify" in manufacturer:
            if "sml" in model:
                preferred_endpoints[0x0406] = 2
                preferred_endpoints[0x0400] = 2
                preferred_endpoints[0x0402] = 2
                logger.info(f"[{self.ieee}] Applied Philips Hue Motion quirk: sensors on EP2")

        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0: continue

            def attach_handler(cluster, is_server=True):
                cid = cluster.cluster_id
                handler_cls = HANDLER_REGISTRY.get(cid)

                if not handler_cls:
                    if cid in SKIP_GENERIC_CLUSTERS: return
                    from handlers.generic import GenericClusterHandler
                    handler_cls = GenericClusterHandler

                if cid in preferred_endpoints and ep_id != preferred_endpoints[cid]: return

                try:
                    handler_key = (ep_id, cid)
                    handler = handler_cls(self, cluster)
                    self.handlers[handler_key] = handler
                    if cid not in self.handlers or ep_id == 1:
                        self.handlers[cid] = handler
                except Exception as e:
                    logger.error(f"[{self.ieee}] Failed to attach handler for EP{ep_id} 0x{cid:04x}: {e}")

            in_clusters = getattr(ep, 'in_clusters', None) or {}
            out_clusters = getattr(ep, 'out_clusters', None) or {}

            for cluster in in_clusters.values(): attach_handler(cluster, is_server=True)
            for cluster in out_clusters.values():
                if (ep_id, cluster.cluster_id) in self.handlers: continue
                attach_handler(cluster, is_server=False)

        self._is_battery_powered()

        if hasattr(self, 'capabilities'):
            self.capabilities._detect_capabilities()
            self.sanitise_state()

    async def configure(self, config=None):
        logger.info(f"[{self.ieee}] Configuring device...")
        if config and config.get('updates'):
            updates = config['updates']
            for handler in self.handlers.values():
                if hasattr(handler, 'apply_configuration'):
                    try: await handler.apply_configuration(updates)
                    except Exception as e: logger.warning(f"[{self.ieee}] Config failed for {handler.__class__.__name__}: {e}")
            if 'qos' in config:
                self.service.device_settings.setdefault(self.ieee, {})['qos'] = config['qos']
            return

        stats = {'configured': 0, 'skipped_not_configurable': 0, 'skipped_controller': 0, 'failed': 0}
        configured = set()

        for h in self.handlers.values():
            if h in configured: continue
            ep_id = h.endpoint.endpoint_id
            cluster_id = h.cluster_id

            if not self.capabilities.is_endpoint_configurable(ep_id):
                role = self.capabilities.get_endpoint_role(ep_id)
                if role == 'controller': stats['skipped_controller'] += 1
                else: stats['skipped_not_configurable'] += 1
                continue

            if not self.capabilities.is_cluster_configurable(cluster_id, ep_id):
                stats['skipped_not_configurable'] += 1
                continue

            try:
                await h.configure()
                configured.add(h)
                stats['configured'] += 1
            except Exception as e:
                stats['failed'] += 1
                logger.warning(f"[{self.ieee}] Config failed EP{ep_id}:0x{cluster_id:04x}: {e}")

        total_skipped = stats['skipped_not_configurable'] + stats['skipped_controller']
        logger.info(f"[{self.ieee}] Config: {stats['configured']} configured, {total_skipped} skipped, {stats['failed']} failed")

    async def interview(self):
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
            except Exception as e:
                success = False
                logger.debug(f"[{self.ieee}] Handler poll failed: {e}")

        if results:
            poll_data = {k: v for k, v in results.items() if not (k.endswith('_raw') or k.startswith('attr_'))}
            if poll_data:
                try:
                    self.update_state(poll_data)
                    logger.info(f"[{self.ieee}] Poll applied: {len(poll_data)} attrs")
                except Exception as e:
                    logger.warning(f"[{self.ieee}] Poll state update failed: {e}")

        return results if success else {}