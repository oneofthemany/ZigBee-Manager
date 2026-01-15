"""
Zone Device LQI Configuration Module
Forces devices to report frequently (5s) to maintain live LQI/RSSI stats.
"""
import asyncio
import logging

logger = logging.getLogger("zones.config")

# PRIORITY 1: Telemetry & Diagnostics (Preferred)
TELEMETRY_CLUSTERS = {
    0x0B04: 0x050B,  # ElectricalMeasurement -> Active Power
    0x0B05: 0x011C,  # Diagnostics -> Last Message LQI
}

# PRIORITY 2: Functional Clusters (Fallback)
FUNCTIONAL_CLUSTERS = {
    0x0006: 0x0000,  # OnOff -> OnOff Status
    0x0008: 0x0000,  # LevelControl -> Current Level
    0x0300: 0x0003,  # ColorControl -> Current X
    0x0000: 0x0001,  # Basic -> Application Version
    0x0003: 0x0000,  # Identify -> IdentifyTime
}

# Standard (non-aggressive) intervals
BASELINE_TELEMETRY = {
    0x0B04: (0x050B, 30, 300, 10),
    0x0B05: (0x011C, 60, 300, 5),
}

BASELINE_FUNCTIONAL = {
    0x0006: (0x0000, 0, 3600, 0),
    0x0008: (0x0000, 1, 300, 5),
    0x0300: (0x0003, 1, 300, 1),
    0x0000: (0x0001, 0, 0xFFFF, 0),
    0x0003: (0x0000, 0, 0xFFFF, 0),
}

BASELINE_CONFIG = {**BASELINE_TELEMETRY, **BASELINE_FUNCTIONAL}

async def configure_zone_device_reporting(zigbee_service, device_ieees: list):
    """
    Configure zone devices to report LQI changes.
    """
    configured_count = 0
    skipped_count = 0
    failed_count = 0

    for ieee in device_ieees:
        try:
            if ieee not in zigbee_service.devices:
                continue

            device = zigbee_service.devices[ieee]
            zigpy_dev = device.zigpy_dev

            # Check for End Device (Battery/Sleepy)
            is_end_device = False
            if hasattr(zigpy_dev, 'node_desc') and zigpy_dev.node_desc:
                is_end_device = zigpy_dev.node_desc.is_end_device

            # Skip battery devices (they sleep)
            if is_end_device:
                logger.debug(f"[{ieee}] Skipping End Device (likely battery)")
                skipped_count += 1
                continue

            # Try configuring
            success = await _configure_aggressive_reporting(ieee, zigpy_dev)
            if success:
                configured_count += 1
            else:
                logger.warning(f"[{ieee}] No suitable clusters found for LQI reporting")
                failed_count += 1

        except Exception as e:
            logger.error(f"[{ieee}] Error during zone config: {e}")
            failed_count += 1

    logger.info(f"[Zone] Configured: {configured_count}, Skipped: {skipped_count}, Failed: {failed_count}")
    return {"configured": configured_count, "skipped": skipped_count, "failed": failed_count}


async def _configure_aggressive_reporting(ieee, zigpy_dev):
    """
    Try Telemetry first, then fallback to Functional clusters.
    """
    # 1. Try Telemetry Clusters First (Power/Diagnostics)
    if await _apply_config(ieee, zigpy_dev, TELEMETRY_CLUSTERS, "Telemetry", stop_on_first=True):
        return True

    # 2. Fallback to Functional Clusters
    logger.info(f"[{ieee}] No telemetry clusters; configuring ALL Functional clusters")
    if await _apply_config(ieee, zigpy_dev, FUNCTIONAL_CLUSTERS, "Fallback", stop_on_first=False):
        return True

    return False


async def remove_aggressive_reporting(zigbee_service, device_ieees: list):
    """Restore baseline reporting (removes aggressive config)."""
    restored_count = 0
    failed_count = 0

    for ieee in device_ieees:
        if ieee not in zigbee_service.devices:
            continue

        device = zigbee_service.devices[ieee]
        zigpy_dev = device.zigpy_dev

        try:
            await _restore_baseline_reporting(ieee, zigpy_dev)
            restored_count += 1
            logger.info(f"[{ieee}] Baseline reporting restored")
        except Exception as e:
            logger.error(f"[{ieee}] Failed to restore: {e}")
            failed_count += 1

    return {"restored": restored_count, "failed": failed_count}


async def _restore_baseline_reporting(ieee, zigpy_dev):
    """Restore standard reporting intervals."""
    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue

        for cluster_id, (attr_id, min_int, max_int, change) in BASELINE_CONFIG.items():
            cluster = endpoint.in_clusters.get(cluster_id) or endpoint.out_clusters.get(cluster_id)
            if not cluster:
                continue

            try:
                await cluster.configure_reporting(
                    attr_id,
                    min_interval=min_int,
                    max_interval=max_int,
                    reportable_change=change
                )
                logger.info(f"[{ieee}] Restored baseline on 0x{cluster_id:04X} (EP{ep_id})")
            except Exception as e:
                logger.debug(f"[{ieee}] Baseline restore failed on 0x{cluster_id:04X}: {e}")


async def _remove_config(ieee, zigpy_dev, cluster_map):
    """Remove reporting config from a cluster set."""
    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0: continue

        for cluster_id, attr_id in cluster_map.items():
            cluster = endpoint.in_clusters.get(cluster_id) or endpoint.out_clusters.get(cluster_id)
            if not cluster:
                continue

            try:
                # Reset to normal: min=0, max=0xFFFF (disabled periodic)
                await cluster.configure_reporting(
                    attr_id,
                    min_interval=0,
                    max_interval=0xFFFF,
                    reportable_change=0
                )
                logger.info(f"[{ieee}] Removed aggressive config on 0x{cluster_id:04X} (EP{ep_id})")
            except Exception as e:
                logger.debug(f"[{ieee}] Failed to remove 0x{cluster_id:04X}: {e}")

async def _apply_config(ieee, zigpy_dev, cluster_map, config_type, stop_on_first=False):
    """Helper to apply config to a set of clusters."""
    success = False
    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0: continue

        for cluster_id, attr_id in cluster_map.items():
            # Find the cluster (Input or Output)
            cluster = None
            if cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[cluster_id]
            elif cluster_id in endpoint.out_clusters:
                cluster = endpoint.out_clusters[cluster_id]

            if not cluster:
                continue

            try:
                await cluster.bind()
                # Max Interval 5s = The Heartbeat
                await cluster.configure_reporting(
                    attr_id, min_interval=1, max_interval=5, reportable_change=0
                )
                logger.info(f"[{ieee}] ⚡ {config_type} LQI configured on 0x{cluster_id:04X} (EP{ep_id})")
                success = True

                if stop_on_first:
                    return True

            except Exception as e:
                logger.debug(f"[{ieee}] Failed to config 0x{cluster_id:04X}: {e}")
                continue

    return success

