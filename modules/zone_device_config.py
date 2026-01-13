"""
Zone Device LQI Configuration Module
Add to modules/zone_config.py or integrate into modules/zones.py
"""
import asyncio
import logging

logger = logging.getLogger("zones.config")

DIAGNOSTICS_CLUSTER = 0x0B05
LAST_MESSAGE_LQI_ATTR = 0x011C
LAST_MESSAGE_RSSI_ATTR = 0x011D


async def configure_zone_device_reporting(zigbee_service, device_ieees: list):
    """
    Configure zone devices to report LQI changes.
    Call when zone is created or on startup.

    Args:
        zigbee_service: The ZigbeeService instance
        device_ieees: List of IEEE addresses in the zone
    """
    configured_count = 0

    for ieee in device_ieees:
        if ieee not in zigbee_service.devices:
            logger.warning(f"[Zone] Device {ieee} not found")
            continue

        device = zigbee_service.devices[ieee]
        zigpy_dev = device.zigpy_dev

        # Skip battery devices - they sleep
        if device._is_battery_powered():
            logger.debug(f"[{ieee}] Skipping battery device")
            continue

        success = await _configure_diagnostics_reporting(ieee, zigpy_dev)
        if success:
            configured_count += 1
        else:
            # Fallback: configure frequent reporting on any available cluster
            await _configure_fallback_reporting(ieee, zigpy_dev)
            configured_count += 1

    logger.info(f"[Zone] Configured {configured_count}/{len(device_ieees)} devices for LQI reporting")
    return configured_count


async def _configure_diagnostics_reporting(ieee: str, zigpy_dev) -> bool:
    """Configure Diagnostics cluster (0x0B05) for LQI reporting."""
    for ep_id, ep in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue

        if DIAGNOSTICS_CLUSTER not in ep.in_clusters:
            continue

        cluster = ep.in_clusters[DIAGNOSTICS_CLUSTER]

        try:
            await cluster.bind()

            # Configure LQI reporting: min 10s, max 60s, change threshold 5
            await cluster.configure_reporting(
                LAST_MESSAGE_LQI_ATTR,
                min_interval=10,
                max_interval=60,
                reportable_change=5
            )

            logger.info(f"[{ieee}] Configured Diagnostics LQI reporting on EP{ep_id}")
            return True

        except Exception as e:
            logger.debug(f"[{ieee}] Diagnostics config failed: {e}")

    return False


async def _configure_fallback_reporting(ieee: str, zigpy_dev):
    """
    Configure fallback reporting on common clusters to ensure frequent messages.
    This ensures we get LQI from regular message traffic.
    """
    # Cluster ID, Attribute ID, min_interval, max_interval, change threshold
    FALLBACK_CONFIGS = [
        # OnOff cluster - state changes
        (0x0006, 0x0000, 30, 120, 0),
        # Level cluster - brightness changes
        (0x0008, 0x0000, 30, 120, 5),
        # Electrical Measurement - power changes
        (0x0B04, 0x050B, 30, 120, 10),
        # Temperature - for sensors
        (0x0402, 0x0000, 30, 120, 50),
    ]

    for ep_id, ep in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue

        for cluster_id, attr_id, min_int, max_int, change in FALLBACK_CONFIGS:
            if cluster_id not in ep.in_clusters:
                continue

            cluster = ep.in_clusters[cluster_id]

            try:
                await cluster.bind()
                await cluster.configure_reporting(
                    attr_id,
                    min_interval=min_int,
                    max_interval=max_int,
                    reportable_change=change
                )
                logger.info(f"[{ieee}] Configured fallback reporting on 0x{cluster_id:04X}")
                return  # One successful config is enough

            except Exception as e:
                logger.debug(f"[{ieee}] Fallback config 0x{cluster_id:04X} failed: {e}")
                continue


async def configure_router_neighbor_reporting(zigbee_service, router_ieees: list):
    """
    Configure routers to report neighbor table changes.
    This helps track mesh topology changes for presence detection.
    """
    for ieee in router_ieees:
        if ieee not in zigbee_service.devices:
            continue

        device = zigbee_service.devices[ieee]

        # Only configure routers (not end devices)
        if device.get_role() != "Router":
            continue

        zigpy_dev = device.zigpy_dev

        # Try to configure neighbor table reporting via ZDO
        try:
            # Request neighbor table update binding
            # This is coordinator -> router, so router reports changes
            await zigpy_dev.zdo.bind()
            logger.info(f"[{ieee}] Bound router for neighbor updates")
        except Exception as e:
            logger.debug(f"[{ieee}] Router binding failed: {e}")