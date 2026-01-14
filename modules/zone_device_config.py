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

# Critical Clusters for LQI generation
# We force these to report every 5 seconds
TARGET_CLUSTERS = {
    #0x0006: 0x0000,  # OnOff -> OnOff Status
    #0x0008: 0x0000,  # LevelControl -> Current Level
    #0x0300: 0x0003,  # ColorControl -> Current X (or 0x0007 Color Temp)
    #0x0B04: 0x050B,  # ElectricalMeasurement -> Active Power

    # Telemetry & Diagnostic Clusters (Preferred for LQI generation)
    0x0B04: 0x050B,  # ElectricalMeasurement -> Active Power
    0x0B05: 0x011C,  # Diagnostics -> Last Message LQI
}

async def configure_zone_device_reporting(zigbee_service, device_ieees: list):
    """Configure zone devices to report LQI changes."""
    configured_count = 0
    skipped_count = 0
    failed_count = 0

    for ieee in device_ieees:
        if ieee not in zigbee_service.devices:
            logger.warning(f"[{ieee}] Device not found")
            continue

        device = zigbee_service.devices[ieee]
        zigpy_dev = device.zigpy_dev

        # Use get_role() - the authoritative method
        role = device.get_role()
        if role not in ("Router", "Coordinator"):
            logger.info(f"[{ieee}] Skipping {role} - only routers support aggressive reporting")
            skipped_count += 1
            continue

        try:
            success = await _configure_aggressive_reporting(ieee, zigpy_dev)
            if success:
                configured_count += 1
                logger.info(f"[{ieee}] ✅ Aggressive LQI reporting configured")
            else:
                logger.warning(f"[{ieee}] No suitable clusters found for LQI reporting")
                failed_count += 1
        except Exception as e:
            logger.error(f"[{ieee}] Failed to configure: {e}")
            failed_count += 1

    logger.info(f"[Zone] Configured: {configured_count}, Skipped: {skipped_count}, Failed: {failed_count}")
    return {"configured": configured_count, "skipped": skipped_count, "failed": failed_count}


async def _configure_aggressive_reporting(ieee, zigpy_dev):
    """
    Iterate through device endpoints and force aggressive reporting on supported clusters.
    """
    success = False

    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0: continue # Skip ZDO

        # Loop through our target clusters (Light, Power, etc.)
        for cluster_id, attr_id in TARGET_CLUSTERS.items():
            if cluster_id in endpoint.out_clusters:
                # Some devices use Output clusters (rare for state, but possible)
                cluster = endpoint.out_clusters[cluster_id]
            elif cluster_id in endpoint.in_clusters:
                # Most devices use Input clusters for state (OnOff, Level)
                cluster = endpoint.in_clusters[cluster_id]
            else:
                continue

            try:
                # 1. Bind (Ensure we get the reports)
                await cluster.bind()

                # 2. Configure Reporting (The Magic Fix)
                # Min=1: Don't spam if changing instantly
                # Max=5: FORCE a report every 5 seconds (The Heartbeat)
                # Change=1: Report even tiny changes
                await cluster.configure_reporting(
                    attr_id,
                    min_interval=1,
                    max_interval=5,
                    reportable_change=1
                )

                logger.info(f"[{ieee}] ⚡ FAST LQI configured on Cluster 0x{cluster_id:04X} (EP{ep_id})")
                success = True

            except Exception as e:
                logger.debug(f"[{ieee}] Failed to config cluster 0x{cluster_id:04X}: {e}")
                continue

    return success

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
                    min_interval=2,
                    max_interval=5,
                    reportable_change=1
                )
                logger.info(f"[{ieee}] Configured fast LQI reporting on 0x{cluster_id:04X}")
                return

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