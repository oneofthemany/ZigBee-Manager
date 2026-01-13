"""
Diagnostics Cluster Handler (0x0B05)
Captures LQI/RSSI attribute reports for zone presence detection.
"""
import logging
from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.diagnostics")


@register_handler(0x0B05)
class DiagnosticsHandler(ClusterHandler):
    """Handler for Diagnostics cluster - provides LQI/RSSI data."""

    CLUSTER_ID = 0x0B05
    CLUSTER_NAME = "Diagnostics"

    # Key attributes
    LAST_MESSAGE_LQI = 0x011C
    LAST_MESSAGE_RSSI = 0x011D

    def attribute_updated(self, attrid: int, value):
        """Handle diagnostic attribute reports."""
        if attrid == self.LAST_MESSAGE_LQI:
            logger.debug(f"[{self.device.ieee}] Diagnostics LQI: {value}")
            self._forward_to_zones(lqi=value)

        elif attrid == self.LAST_MESSAGE_RSSI:
            logger.debug(f"[{self.device.ieee}] Diagnostics RSSI: {value}")
            self._forward_to_zones(rssi=value)

    def _forward_to_zones(self, lqi=None, rssi=None):
        """Forward LQI/RSSI to zone manager."""
        zone_mgr = getattr(self.device.service, 'zone_manager', None)
        if not zone_mgr:
            return

        coordinator_ieee = str(self.device.service.app.ieee)

        if lqi is not None and rssi is None:
            rssi = int(-100 + (lqi / 255) * 70)
        elif rssi is not None and lqi is None:
            lqi = int((rssi + 100) * 255 / 70)
            lqi = max(0, min(255, lqi))

        if lqi is not None:
            zone_mgr.record_link_quality(
                source_ieee=coordinator_ieee,
                target_ieee=self.device.ieee,
                rssi=rssi,
                lqi=lqi
            )

    async def configure(self):
        """Configure reporting for diagnostics."""
        try:
            await self.cluster.bind()
            await self.cluster.configure_reporting(
                self.LAST_MESSAGE_LQI,
                min_interval=10,
                max_interval=60,
                reportable_change=5
            )
            logger.info(f"[{self.device.ieee}] Configured Diagnostics LQI reporting")
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Diagnostics config failed: {e}")

    async def poll(self):
        """Poll diagnostic attributes."""
        try:
            result = await self.cluster.read_attributes([
                self.LAST_MESSAGE_LQI,
                self.LAST_MESSAGE_RSSI
            ])
            return {}
        except Exception:
            return {}

    def get_discovery_configs(self):
        return []