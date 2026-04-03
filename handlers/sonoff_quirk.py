"""Sonoff ZBMINIR2 specific handling"""
import logging
from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.sonoff")

@register_handler(0xFC11)
class SonoffManufacturerHandler(ClusterHandler):
    """Handle Sonoff manufacturer cluster 0xFC11"""
    CLUSTER_ID = 0xFC11

    ATTR_EXTERNAL_TRIGGER = 0x0016
    ATTR_DETACH_RELAY = 0x0017
    ATTR_WORK_MODE = 0x0018

    async def configure(self):
        """Read device mode on startup"""
        try:
            result = await self.cluster.read_attributes([
                self.ATTR_WORK_MODE,
                self.ATTR_DETACH_RELAY
            ])
            logger.info(f"[{self.device.ieee}] Sonoff config: {result}")
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to read Sonoff config: {e}")
