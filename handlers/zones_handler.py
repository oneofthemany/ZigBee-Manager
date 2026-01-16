"""
Zones Handler - Captures RSSI/LQI from incoming Zigbee messages.

Hooks into the zigpy message flow to extract link quality data
for zone-based presence detection.
"""

import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from modules.zones import ZoneManager

logger = logging.getLogger(__name__)


class ZonesMessageHandler:
    """
    Intercepts Zigbee messages to extract RSSI/LQI data for zones.

    This handler should be called from the main message processing
    pipeline (e.g., in handle_message or cluster handlers).
    """

    def __init__(self, zone_manager: 'ZoneManager'):
        self.zone_manager = zone_manager
        self._message_count = 0
        self._rssi_capture_count = 0

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
        """
        Process incoming message for RSSI/LQI data.

        This should be called from the main message handler.
        Does not modify the message flow - purely observational.
        """
        self._message_count += 1

        if rssi is None and lqi is None:
            return

        # Get device IEEE
        device_ieee = str(getattr(device, 'ieee', None))
        if not device_ieee or device_ieee == 'None':
            return

        # Default coordinator IEEE if not available
        coordinator_ieee = self._get_coordinator_ieee()

        # For direct coordinator->device messages, record the link
        if coordinator_ieee and (rssi is not None or lqi is not None):
            # Use LQI-derived RSSI if no direct RSSI
            if rssi is None and lqi is not None:
                rssi = self._lqi_to_rssi(lqi)
            if lqi is None and rssi is not None:
                lqi = self._rssi_to_lqi(rssi)

            self.zone_manager.record_link_quality(
                source_ieee=coordinator_ieee,
                target_ieee=device_ieee,
                rssi=rssi or 0,
                lqi=lqi or 0,
            )
            self._rssi_capture_count += 1

    def handle_neighbor_report(
            self,
            reporter_ieee: str,
            neighbor_ieee: str,
            lqi: int,
            rssi: Optional[int] = None,
    ) -> None:
        """
        Handle neighbor table reports from routers.

        Called when processing Mgmt_Lqi_rsp or similar.
        """
        if rssi is None:
            rssi = self._lqi_to_rssi(lqi)

        self.zone_manager.record_link_quality(
            source_ieee=reporter_ieee,
            target_ieee=neighbor_ieee,
            rssi=rssi,
            lqi=lqi,
        )
        self._rssi_capture_count += 1

    def handle_route_record(
            self,
            source_ieee: str,
            relay_ieees: list,
            lqi_values: Optional[list] = None,
    ) -> None:
        """
        Handle route record for multi-hop path quality.

        Route records show the path a message took through the mesh.
        """
        if not relay_ieees:
            return

        # Build chain: source -> relay1 -> relay2 -> ... -> coordinator
        path = [source_ieee] + relay_ieees

        for i in range(len(path) - 1):
            src = path[i]
            dst = path[i + 1]

            # Use provided LQI if available, otherwise estimate
            lqi = lqi_values[i] if lqi_values and i < len(lqi_values) else 200
            rssi = self._lqi_to_rssi(lqi)

            self.zone_manager.record_link_quality(
                source_ieee=src,
                target_ieee=dst,
                rssi=rssi,
                lqi=lqi,
            )

    def _get_coordinator_ieee(self) -> Optional[str]:
        """Get coordinator IEEE from zone manager's app controller."""
        if self.zone_manager.app_controller:
            return str(self.zone_manager.app_controller.ieee)
        return None

    def _lqi_to_rssi(self, lqi: int) -> int:
        """Convert LQI (0-255) to approximate RSSI (dBm)."""
        # Linear mapping: LQI 255 -> -30dBm, LQI 0 -> -100dBm
        return int(-100 + (lqi / 255) * 70)

    def _rssi_to_lqi(self, rssi: int) -> int:
        """Convert RSSI (dBm) to approximate LQI (0-255)."""
        # Inverse of lqi_to_rssi
        lqi = int((rssi + 100) * 255 / 70)
        return max(0, min(255, lqi))

    def get_stats(self) -> dict:
        """Get handler statistics."""
        return {
            'messages_processed': self._message_count,
            'rssi_captures': self._rssi_capture_count,
            'zones_active': len(self.zone_manager.zones),
        }


def patch_message_handler(core, zone_manager: 'ZoneManager') -> ZonesMessageHandler:
    """
    Patch the core message handler to include RSSI capture.

    This injects RSSI capture into the existing message flow without
    modifying the core behavior.

    Args:
        core: The ZigbeeCore instance
        zone_manager: The ZoneManager instance

    Returns:
        The ZonesMessageHandler instance
    """
    handler = ZonesMessageHandler(zone_manager)

    # Store original handler
    original_handler = getattr(core, 'handle_message', None)

    if original_handler:
        async def wrapped_handler(device, profile, cluster, src_ep, dst_ep, message, *args, **kwargs):
            # Extract RSSI/LQI from kwargs or device
            rssi = kwargs.get('rssi')
            lqi = kwargs.get('lqi')

            # Some implementations store on device
            if rssi is None and hasattr(device, 'rssi'):
                rssi = device.rssi
            if lqi is None and hasattr(device, 'lqi'):
                lqi = device.lqi

            # Capture for zones
            handler.handle_message(device, profile, cluster, src_ep, dst_ep, message, rssi, lqi)

            # Call original
            return await original_handler(device, profile, cluster, src_ep, dst_ep, message, *args, **kwargs)

        core.handle_message = wrapped_handler
        logger.info("Patched message handler for zone RSSI capture")

    return handler


def setup_rssi_listener(app_controller, zone_manager: 'ZoneManager') -> None:
    """
    Setup RSSI listener on the zigpy application controller.

    Uses zigpy's built-in message callbacks to capture RSSI/LQI.
    """
    handler = ZonesMessageHandler(zone_manager)

    # Hook into packet_received if available (zigpy 0.60+)
    if hasattr(app_controller, 'packet_received'):
        original_packet_received = app_controller.packet_received

        def wrapped_packet_received(packet):
            # Extract RSSI/LQI from packet if available
            rssi = getattr(packet, 'rssi', None)
            lqi = getattr(packet, 'lqi', None)
            src = getattr(packet, 'src', None)

            if src and (rssi is not None or lqi is not None):
                device = app_controller.devices.get(src.address)
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

            return original_packet_received(packet)

        app_controller.packet_received = wrapped_packet_received
        logger.info("Setup RSSI listener on packet_received")

    # Also hook device_initialized for initial neighbor scans
    #if hasattr(app_controller, 'device_initialized'):
    #    original_device_initialized = app_controller.device_initialized

    #    async def wrapped_device_initialized(device):
    #        result = await original_device_initialized(device)

    #        # If device is a router, scan its neighbors
    #        if device.node_desc and device.node_desc.is_router:
    #            logger.debug(f"Router initialized, will scan neighbors: {device.ieee}")

    #        return result

    #    app_controller.device_initialized = wrapped_device_initialized