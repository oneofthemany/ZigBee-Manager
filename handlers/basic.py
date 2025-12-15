"""
Basic cluster handlers.
Handles: Basic (0x0000), Identify (0x0003)
"""
import logging
from typing import Any, Dict

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.basic")

# ============================================================
# BASIC CLUSTER (0x0000)
# ============================================================
@register_handler(0x0000)
class BasicHandler(ClusterHandler):
    CLUSTER_ID = 0x0000
    ATTR_MANUFACTURER = 0x0004
    ATTR_MODEL = 0x0005
    ATTR_POWER_SOURCE = 0x0007
    ATTR_SW_BUILD_ID = 0x4000

    POWER_SOURCES = {
        0x00: "Unknown", 0x01: "Mains (Single Phase)", 0x02: "Mains (3 Phase)",
        0x03: "Battery", 0x04: "DC Source", 0x05: "Emergency Mains (Constant)",
        0x06: "Emergency Mains (Transferring)",
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_POWER_SOURCE:
            source_name = self.POWER_SOURCES.get(value, f"Unknown({value})")
            self.device.update_state({"power_source": source_name})

    async def poll(self) -> Dict[str, Any]:
        results = {}
        try:
            attrs = [self.ATTR_MANUFACTURER, self.ATTR_MODEL, self.ATTR_POWER_SOURCE, self.ATTR_SW_BUILD_ID]
            result = await self.cluster.read_attributes(attrs)
            if result and result[0]:
                data = result[0]
                if self.ATTR_MANUFACTURER in data: results["manufacturer"] = str(data[self.ATTR_MANUFACTURER])
                if self.ATTR_MODEL in data: results["model"] = str(data[self.ATTR_MODEL])
                if self.ATTR_POWER_SOURCE in data:
                    results["power_source"] = self.POWER_SOURCES.get(data[self.ATTR_POWER_SOURCE], "Unknown")
                if self.ATTR_SW_BUILD_ID in data: results["sw_version"] = str(data[self.ATTR_SW_BUILD_ID])
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to poll basic cluster: {e}")
        return results

# ============================================================
# IDENTITY CLUSTER (0x0003)
# ============================================================
@register_handler(0x0003)
class IdentifyHandler(ClusterHandler):
    CLUSTER_ID = 0x0003
    ATTR_IDENTIFY_TIME = 0x0000
    async def identify(self, duration=5): await self.cluster.identify(duration)
    async def trigger_effect(self, effect_id=0, variant=0): await self.cluster.trigger_effect(effect_id, variant)

