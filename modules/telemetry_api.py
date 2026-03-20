"""
Telemetry API - FastAPI routes for system and network telemetry.

Endpoints:
  GET  /api/telemetry/system/current    — Latest system metrics snapshot
  GET  /api/telemetry/system/history    — Historical system metrics (bucketed)
  GET  /api/telemetry/packets           — Network packet stats history
  GET  /api/telemetry/device/{ieee}     — Device state change history
  GET  /api/telemetry/db/stats          — Database size and row counts
  POST /api/telemetry/db/prune          — Manual retention cleanup
  GET  /api/telemetry/thresholds        — Alert threshold config
  POST /api/telemetry/thresholds        — Update alert thresholds
"""

import logging
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

_get_system_monitor = None


def register_telemetry_routes(app, system_monitor_getter: Callable):
    """Register telemetry routes on the FastAPI app."""
    global _get_system_monitor
    _get_system_monitor = system_monitor_getter
    app.include_router(router)
    logger.info("Telemetry API routes registered")


# ============================================================================
# SYSTEM METRICS
# ============================================================================

@router.get("/system/current")
async def system_current():
    """Get the latest system metrics snapshot (no DB query)."""
    mon = _get_system_monitor() if _get_system_monitor else None
    if not mon:
        return {"error": "System monitor not running"}
    return mon.get_current()


@router.get("/system/history")
async def system_history(hours: int = 1, bucket: int = 1):
    """
    Get historical system metrics, aggregated by time bucket.
    Args:
        hours: lookback window (max 168 = 7 days)
        bucket: aggregation bucket in minutes (default 1)
    """
    hours = min(max(hours, 1), 168)
    bucket = min(max(bucket, 1), 60)

    try:
        from modules.telemetry_db import query_system_metrics
        data = query_system_metrics(hours=hours, bucket_minutes=bucket)
        # Serialise timestamps to ISO strings
        for row in data:
            if row.get("ts"):
                row["ts"] = str(row["ts"])
        return {"success": True, "hours": hours, "bucket_minutes": bucket, "data": data}
    except Exception as e:
        logger.error(f"System history query failed: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# PACKET STATS
# ============================================================================

@router.get("/packets")
async def packet_history(ieee: Optional[str] = None, hours: int = 1):
    """Get packet stats history, optionally filtered by device."""
    hours = min(max(hours, 1), 168)
    try:
        from modules.telemetry_db import query_packet_stats
        data = query_packet_stats(ieee=ieee, hours=hours)
        for row in data:
            if row.get("ts"):
                row["ts"] = str(row["ts"])
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# DEVICE STATE HISTORY
# ============================================================================

@router.get("/device/{ieee}")
async def device_state_history(ieee: str, attribute: str = "state", hours: int = 24):
    """Get state change history for a specific device attribute."""
    hours = min(max(hours, 1), 168)
    try:
        from modules.telemetry_db import query_device_state_history
        data = query_device_state_history(ieee=ieee, attribute=attribute, hours=hours)
        for row in data:
            if row.get("ts"):
                row["ts"] = str(row["ts"])
        return {"success": True, "ieee": ieee, "attribute": attribute, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# DATABASE MANAGEMENT
# ============================================================================

@router.get("/db/stats")
async def db_stats():
    """Get database size and row counts per table."""
    try:
        from modules.telemetry_db import get_db_stats
        return {"success": True, **get_db_stats()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/db/prune")
async def db_prune(days: int = 7):
    """Manually trigger retention cleanup."""
    days = min(max(days, 1), 90)
    try:
        from modules.telemetry_db import prune
        prune(retention_days=days)
        return {"success": True, "retention_days": days}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# THRESHOLDS
# ============================================================================

@router.get("/thresholds")
async def get_thresholds():
    """Get current alert thresholds."""
    mon = _get_system_monitor() if _get_system_monitor else None
    if not mon:
        return {"error": "System monitor not running"}
    return mon.get_thresholds()


@router.post("/thresholds")
async def update_thresholds(data: dict):
    """Update alert thresholds."""
    mon = _get_system_monitor() if _get_system_monitor else None
    if not mon:
        raise HTTPException(503, "System monitor not running")
    mon.update_thresholds(data)
    return {"success": True, "thresholds": mon.get_thresholds()}