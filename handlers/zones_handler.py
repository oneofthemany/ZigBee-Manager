"""
Zones Handler - Capture per-device RSSI/LQI from frames received at the
coordinator. Feeds the ZoneManager with single-device samples; link-pair
capture and neighbor-table polling are NOT used for presence detection.
"""

import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from modules.zones import ZoneManager

logger = logging.getLogger(__name__)


class ZonesMessageHandler:
    """
    Observer that extracts (device_ieee, rssi, lqi) from every frame the
    coordinator receives from a device, and forwards it to the ZoneManager.
    """

    def __init__(self, zone_manager: 'ZoneManager'):
        self.zone_manager = zone_manager
        self._message_count = 0
        self._rssi_capture_count = 0

    # ------------------------------------------------------------------ #
    def handle_message(
            self,
            device: Any,
            profile: int,
            cluster: int,
            src_ep: int,
            dst_ep: int,
            message: bytes,
            rssi: Optional[int] = None,
            lqi: Optional[int] = None,
    ) -> None:
        """Purely observational. Does not modify the message flow."""
        self._message_count += 1
        if rssi is None and lqi is None:
            return
        device_ieee = str(getattr(device, 'ieee', None))
        if not device_ieee or device_ieee == 'None':
            return

        # Derive whichever value is missing — we always feed both to the
        # ZoneManager, but only RSSI is used as the primary signal.
        if rssi is None and lqi is not None:
            rssi = self._lqi_to_rssi(lqi)
        if lqi is None and rssi is not None:
            lqi = self._rssi_to_lqi(rssi)

        self.zone_manager.record_device_rssi(
            ieee=device_ieee, rssi=rssi or 0, lqi=lqi or 0
        )
        self._rssi_capture_count += 1

    # ------------------------------------------------------------------ #
    def _lqi_to_rssi(self, lqi: int) -> int:
        return int(-100 + (lqi / 255) * 70)

    def _rssi_to_lqi(self, rssi: int) -> int:
        lqi = int((rssi + 100) * 255 / 70)
        return max(0, min(255, lqi))

    def get_stats(self) -> dict:
        return {
            'messages_processed': self._message_count,
            'rssi_captures': self._rssi_capture_count,
            'zones_active': len(self.zone_manager.zones),
        }


# ---------------------------------------------------------------------- #
def setup_rssi_listener(app_controller, zone_manager: 'ZoneManager') -> ZonesMessageHandler:
    """
    Wrap app_controller.packet_received to observe every inbound packet and
    feed RSSI/LQI into the zone manager. Non-destructive: the original
    callback is always invoked.
    """
    handler = ZonesMessageHandler(zone_manager)

    if hasattr(app_controller, 'packet_received'):
        original_packet_received = app_controller.packet_received

        def wrapped_packet_received(packet):
            try:
                rssi = getattr(packet, 'rssi', None)
                lqi = getattr(packet, 'lqi', None)
                src = getattr(packet, 'src', None)
                if src and (rssi is not None or lqi is not None):
                    device = app_controller.devices.get(getattr(src, 'address', src))
                    if device:
                        handler.handle_message(
                            device=device,
                            profile=getattr(packet, 'profile_id', 0),
                            cluster=getattr(packet, 'cluster_id', 0),
                            src_ep=getattr(src, 'endpoint', 0),
                            dst_ep=0,
                            message=b'',
                            rssi=rssi,
                            lqi=lqi,
                        )
            except Exception as e:
                logger.debug(f"RSSI observer error: {e}")
            return original_packet_received(packet)

        app_controller.packet_received = wrapped_packet_received
        logger.info("Setup RSSI listener on packet_received")

    return handler