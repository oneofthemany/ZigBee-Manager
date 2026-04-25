"""
Zone Device LQI Configuration.

Aggressive LQI/RSSI reporting (1-5s) is ONLY applied to mains-fed router
devices. End devices (sleepy/battery) are always skipped regardless of
what callers pass in — they are not suitable for tight reporting windows
and attempting to bind them drains batteries and generally fails anyway.
"""

import asyncio
import logging

logger = logging.getLogger("zones.config")

# PRIORITY 1: Telemetry / diagnostics clusters (no side effects)
TELEMETRY_CLUSTERS = {
    0x0B04: 0x050B,   # ElectricalMeasurement -> Active Power
    0x0B05: 0x011C,   # Diagnostics -> Last Message LQI
}

# PRIORITY 2: Functional clusters (fallback)
FUNCTIONAL_CLUSTERS = {
    0x0006: 0x0000,   # OnOff -> OnOff status
    0x0008: 0x0000,   # LevelControl -> Current Level
    0x0300: 0x0003,   # ColorControl -> Current X
    0x0000: 0x0001,   # Basic -> Application Version
    0x0003: 0x0000,   # Identify -> IdentifyTime
}

# Baseline (relaxed) intervals
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


def _is_mains_router(zigpy_dev) -> bool:
    """True if this device is a router AND not an end-device per node_desc."""
    nd = getattr(zigpy_dev, 'node_desc', None)
    if not nd:
        return False
    # node_desc.is_end_device is the authoritative "sleepy" flag
    if getattr(nd, 'is_end_device', False):
        return False
    # logical_type == 1 means Router in Zigbee node descriptor
    return getattr(nd, 'logical_type', None) == 1


async def configure_zone_device_reporting(zigbee_service, device_ieees: list):
    """
    Configure the given IEEEs for aggressive (1-5s) LQI reporting.
    Silently skips anything that isn't a mains-fed router.
    """
    configured = 0
    skipped = 0
    failed = 0

    for ieee in device_ieees:
        try:
            if ieee not in zigbee_service.devices:
                skipped += 1
                continue

            device = zigbee_service.devices[ieee]
            zigpy_dev = device.zigpy_dev

            if not _is_mains_router(zigpy_dev):
                logger.info(f"[{ieee}] Skipping — not a mains-fed router")
                skipped += 1
                continue

            if await _apply_config(ieee, zigpy_dev, TELEMETRY_CLUSTERS, "Telemetry", stop_on_first=True):
                configured += 1
                continue

            logger.info(f"[{ieee}] No telemetry clusters; trying functional fallback")
            if await _apply_config(ieee, zigpy_dev, FUNCTIONAL_CLUSTERS, "Fallback", stop_on_first=False):
                configured += 1
            else:
                logger.warning(f"[{ieee}] No suitable clusters found for LQI reporting")
                failed += 1

        except Exception as e:
            logger.error(f"[{ieee}] Error during zone config: {e}")
            failed += 1

    logger.info(f"[Zone] Aggressive reporting — configured: {configured}, skipped: {skipped}, failed: {failed}")
    return {"configured": configured, "skipped": skipped, "failed": failed}


async def remove_aggressive_reporting(zigbee_service, device_ieees):
    """
    Restore baseline reporting intervals. Accepts a list or a single ieee.
    """
    if isinstance(device_ieees, str):
        device_ieees = [device_ieees]

    restored = 0
    failed = 0
    for ieee in device_ieees:
        if ieee not in zigbee_service.devices:
            continue
        device = zigbee_service.devices[ieee]
        try:
            await _restore_baseline_reporting(ieee, device.zigpy_dev)
            restored += 1
            logger.info(f"[{ieee}] Baseline reporting restored")
        except Exception as e:
            logger.error(f"[{ieee}] Failed to restore: {e}")
            failed += 1
    return {"restored": restored, "failed": failed}


async def _restore_baseline_reporting(ieee, zigpy_dev):
    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue
        for cluster_id, (attr_id, min_int, max_int, change) in BASELINE_CONFIG.items():
            cluster = endpoint.in_clusters.get(cluster_id) or endpoint.out_clusters.get(cluster_id)
            if not cluster:
                continue
            try:
                await cluster.configure_reporting(
                    attr_id, min_interval=min_int, max_interval=max_int, reportable_change=change,
                )
                logger.info(f"[{ieee}] Restored baseline on 0x{cluster_id:04X} (EP{ep_id})")
            except Exception as e:
                logger.debug(f"[{ieee}] Baseline restore failed on 0x{cluster_id:04X}: {e}")


async def _apply_config(ieee, zigpy_dev, cluster_map, config_type, stop_on_first=False):
    success = False
    for ep_id, endpoint in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue
        for cluster_id, attr_id in cluster_map.items():
            cluster = endpoint.in_clusters.get(cluster_id) or endpoint.out_clusters.get(cluster_id)
            if not cluster:
                continue
            try:
                await cluster.bind()
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