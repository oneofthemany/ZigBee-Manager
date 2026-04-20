"""
Zigbee Device Cache - DuckDB-backed persistent storage
======================================================
Stores per-device topology, cluster lists, attribute metadata, and a
time-series history of attribute values.

Why this exists:
  - Querying a device over the air is slow, expensive, and wakes sleepy
    end devices. Doing it once per cluster and caching the result gives
    the frontend an instant response on repeat views.
  - Attribute *values* change; attribute *metadata* (name, type, R/W)
    is effectively static per (model, cluster). We split the two.
  - Historical values feed the analytics / trends views.

Tables (all in telemetry.duckdb):
  device_endpoints     — {ieee, endpoint_id, profile_id, device_type}
  device_clusters      — {ieee, endpoint_id, cluster_id, direction, name}
  device_attributes    — {ieee, ep, cluster, attr, name, type, R/W}
  attribute_history    — {ts, ieee, ep, cluster, attr, value_text, value_numeric}

Integration points:
  - record_topology(zigpy_dev)     : call from _async_device_initialized
  - record_attribute_metadata(...) : call from discover_cluster_attributes
  - record_value(...)              : call from ClusterHandler.attribute_updated
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from telemetry_db import _get_db  # reuse the singleton connection

logger = logging.getLogger("modules.zigbee_cache")

# Cluster name lookup - shared with debugger
try:
    from zigbee_debug import CLUSTER_NAMES
except Exception:
    CLUSTER_NAMES = {}

_INITIALISED = False


# ============================================================================
# SCHEMA
# ============================================================================

def _init_schema():
    """Create cache tables if they don't exist. Idempotent."""
    global _INITIALISED
    if _INITIALISED:
        return
    db = _get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS device_endpoints (
            ieee          VARCHAR NOT NULL,
            endpoint_id   INTEGER NOT NULL,
            profile_id    INTEGER,
            device_type   INTEGER,
            first_seen    TIMESTAMP DEFAULT now(),
            last_updated  TIMESTAMP DEFAULT now(),
            PRIMARY KEY (ieee, endpoint_id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS device_clusters (
            ieee          VARCHAR NOT NULL,
            endpoint_id   INTEGER NOT NULL,
            cluster_id    INTEGER NOT NULL,
            direction     VARCHAR NOT NULL,
            cluster_name  VARCHAR,
            first_seen    TIMESTAMP DEFAULT now(),
            last_updated  TIMESTAMP DEFAULT now(),
            PRIMARY KEY (ieee, endpoint_id, cluster_id, direction)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS device_attributes (
            ieee              VARCHAR NOT NULL,
            endpoint_id       INTEGER NOT NULL,
            cluster_id        INTEGER NOT NULL,
            attribute_id      INTEGER NOT NULL,
            attribute_name    VARCHAR,
            data_type         VARCHAR,
            readable          BOOLEAN,
            writable          BOOLEAN,
            reportable        BOOLEAN,
            manufacturer_code INTEGER,
            last_discovered   TIMESTAMP DEFAULT now(),
            PRIMARY KEY (ieee, endpoint_id, cluster_id, attribute_id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS attribute_history (
            ts            TIMESTAMP NOT NULL DEFAULT now(),
            ieee          VARCHAR NOT NULL,
            endpoint_id   INTEGER NOT NULL,
            cluster_id    INTEGER NOT NULL,
            attribute_id  INTEGER NOT NULL,
            value_text    VARCHAR,
            value_numeric DOUBLE
        )
    """)

    # Indexes to keep the history queryable at scale
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_attr_hist_device_ts
            ON attribute_history(ieee, ts DESC)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_attr_hist_lookup
            ON attribute_history(ieee, endpoint_id, cluster_id, attribute_id, ts DESC)
    """)

    _INITIALISED = True
    logger.info("Zigbee device cache schema initialised")


# ============================================================================
# WRITE: TOPOLOGY
# ============================================================================

def record_topology(zigpy_dev) -> Dict[str, int]:
    """
    Walk a zigpy device's endpoints and cache them.

    Zero device traffic - only reads the already-populated in_clusters /
    out_clusters dicts that zigpy built during the interview.

    Returns: counts dict for logging.
    """
    _init_schema()
    db = _get_db()

    ieee = str(zigpy_dev.ieee)
    ep_rows: List[tuple] = []
    cluster_rows: List[tuple] = []

    for ep_id, ep in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue  # skip ZDO

        profile_id = getattr(ep, 'profile_id', None)
        device_type = getattr(ep, 'device_type', None)
        ep_rows.append((ieee, ep_id, profile_id, device_type))

        # INPUT clusters (server - receives commands)
        for cid, cluster in getattr(ep, 'in_clusters', {}).items():
            name = CLUSTER_NAMES.get(cid) or getattr(cluster, 'name', None) or f"0x{cid:04X}"
            cluster_rows.append((ieee, ep_id, cid, 'in', name))

        # OUTPUT clusters (client - sends commands)
        for cid, cluster in getattr(ep, 'out_clusters', {}).items():
            name = CLUSTER_NAMES.get(cid) or getattr(cluster, 'name', None) or f"0x{cid:04X}"
            cluster_rows.append((ieee, ep_id, cid, 'out', name))

    # Upsert endpoints. DuckDB syntax: INSERT OR REPLACE
    # We preserve first_seen by reading existing row; use ON CONFLICT
    for row in ep_rows:
        db.execute("""
            INSERT INTO device_endpoints (ieee, endpoint_id, profile_id, device_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (ieee, endpoint_id) DO UPDATE SET
                profile_id = excluded.profile_id,
                device_type = excluded.device_type,
                last_updated = now()
        """, list(row))

    # Upsert clusters
    for row in cluster_rows:
        db.execute("""
            INSERT INTO device_clusters (ieee, endpoint_id, cluster_id, direction, cluster_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (ieee, endpoint_id, cluster_id, direction) DO UPDATE SET
                cluster_name = excluded.cluster_name,
                last_updated = now()
        """, list(row))

    counts = {"endpoints": len(ep_rows), "clusters": len(cluster_rows)}
    logger.info(f"[{ieee}] Cached topology: {counts['endpoints']} EPs, {counts['clusters']} clusters")
    return counts


# ============================================================================
# WRITE: ATTRIBUTE METADATA
# ============================================================================

def record_attribute_metadata(
        ieee: str,
        endpoint_id: int,
        cluster_id: int,
        attributes: List[Dict[str, Any]],
        manufacturer_code: Optional[int] = None,
) -> int:
    """
    Persist attribute metadata from a cluster discovery pass.

    `attributes` is the same list shape that discover_cluster_attributes
    returns to the frontend, i.e. each item has:
        id_int, name, type, readable, writable, value

    The value itself goes to attribute_history too (so the "discovered
    at T=x" value joins the time series).
    """
    _init_schema()
    db = _get_db()

    now_ts = time.time()
    written = 0

    for a in attributes:
        attr_id = a.get("id_int")
        if attr_id is None:
            continue

        db.execute("""
            INSERT INTO device_attributes (
                ieee, endpoint_id, cluster_id, attribute_id,
                attribute_name, data_type, readable, writable,
                reportable, manufacturer_code, last_discovered
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, now())
            ON CONFLICT (ieee, endpoint_id, cluster_id, attribute_id) DO UPDATE SET
                attribute_name = excluded.attribute_name,
                data_type = excluded.data_type,
                readable = excluded.readable,
                writable = excluded.writable,
                manufacturer_code = excluded.manufacturer_code,
                last_discovered = now()
        """, [
            ieee, endpoint_id, cluster_id, attr_id,
            a.get("name"), a.get("type"),
            a.get("readable"), a.get("writable"),
            manufacturer_code,
        ])
        written += 1

        # Record the fresh value into history as well
        val = a.get("value")
        if val is not None:
            record_value(ieee, endpoint_id, cluster_id, attr_id, val)

    logger.debug(f"[{ieee}] Cached {written} attrs for EP{endpoint_id} 0x{cluster_id:04X}")
    return written


# ============================================================================
# WRITE: ATTRIBUTE VALUE (time-series)
# ============================================================================

def record_value(
        ieee: str,
        endpoint_id: int,
        cluster_id: int,
        attribute_id: int,
        value: Any,
) -> None:
    """
    Append an attribute value observation to the time-series history.

    Called from ClusterHandler.attribute_updated so every zigpy-reported
    change is captured. Handles zigpy type wrappers and stores both a
    string form (for display) and a numeric form (for aggregation).
    """
    _init_schema()
    db = _get_db()

    # Unwrap zigpy's value objects
    if hasattr(value, 'value'):
        value = value.value

    # Stringify for display (bytes get hex)
    if isinstance(value, (bytes, bytearray)):
        value_text = value.hex()
    else:
        try:
            value_text = str(value)
            if len(value_text) > 2000:
                value_text = value_text[:2000]  # guard against huge blobs
        except Exception:
            value_text = repr(value)

    # Numeric form - best effort
    value_numeric: Optional[float] = None
    if isinstance(value, bool):
        value_numeric = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        try:
            value_numeric = float(value)
        except (ValueError, OverflowError):
            value_numeric = None

    try:
        db.execute("""
            INSERT INTO attribute_history (
                ieee, endpoint_id, cluster_id, attribute_id,
                value_text, value_numeric
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, [ieee, endpoint_id, cluster_id, attribute_id, value_text, value_numeric])
    except Exception as e:
        # Never let cache writes break the handler path
        logger.debug(f"[{ieee}] attribute_history insert failed: {e}")


# ============================================================================
# READ: TOPOLOGY
# ============================================================================

def get_topology(ieee: str) -> Dict[str, Any]:
    """
    Return cached topology for a device in the same shape the frontend
    expects from discover_attributes metadata - but without any device
    traffic.

    Shape:
        {
            "ieee": "...",
            "endpoints": [
                {"id": 1, "profile_id": 260, "device_type": 0x0402,
                 "in_clusters":  [{"id": 0x0500, "name": "IAS Zone"}, ...],
                 "out_clusters": [...]},
                ...
            ]
        }
    """
    _init_schema()
    db = _get_db()

    ep_rows = db.execute("""
        SELECT endpoint_id, profile_id, device_type, last_updated
        FROM device_endpoints
        WHERE ieee = ?
        ORDER BY endpoint_id
    """, [ieee]).fetchall()

    if not ep_rows:
        return {"ieee": ieee, "endpoints": [], "cached": False}

    cluster_rows = db.execute("""
        SELECT endpoint_id, cluster_id, direction, cluster_name
        FROM device_clusters
        WHERE ieee = ?
        ORDER BY endpoint_id, cluster_id
    """, [ieee]).fetchall()

    # Bucket clusters by (endpoint, direction)
    by_ep: Dict[int, Dict[str, List[Dict]]] = {}
    for ep_id, cid, direction, cname in cluster_rows:
        slot = by_ep.setdefault(ep_id, {"in": [], "out": []})
        slot[direction].append({"id": cid, "id_hex": f"0x{cid:04X}", "name": cname})

    endpoints = []
    for ep_id, profile_id, device_type, last_updated in ep_rows:
        slot = by_ep.get(ep_id, {"in": [], "out": []})
        endpoints.append({
            "id": ep_id,
            "profile_id": profile_id,
            "device_type": device_type,
            "in_clusters": slot["in"],
            "out_clusters": slot["out"],
            "last_updated": str(last_updated) if last_updated else None,
        })

    return {"ieee": ieee, "endpoints": endpoints, "cached": True}


# ============================================================================
# READ: ATTRIBUTE METADATA (+ latest value)
# ============================================================================

def get_cached_attributes(
        ieee: str,
        endpoint_id: int,
        cluster_id: int,
) -> Dict[str, Any]:
    """
    Return cached attribute metadata for a cluster, plus the latest
    observed value for each attribute (joined from attribute_history).

    Returns the same shape discover_cluster_attributes returns, with an
    extra "cached": True marker and "last_discovered" / "last_value_ts".
    """
    _init_schema()
    db = _get_db()

    rows = db.execute("""
        SELECT
            a.attribute_id,
            a.attribute_name,
            a.data_type,
            a.readable,
            a.writable,
            a.last_discovered,
            (SELECT h.value_text FROM attribute_history h
             WHERE h.ieee = a.ieee AND h.endpoint_id = a.endpoint_id
               AND h.cluster_id = a.cluster_id AND h.attribute_id = a.attribute_id
             ORDER BY h.ts DESC LIMIT 1) AS latest_value,
            (SELECT h.ts FROM attribute_history h
             WHERE h.ieee = a.ieee AND h.endpoint_id = a.endpoint_id
               AND h.cluster_id = a.cluster_id AND h.attribute_id = a.attribute_id
             ORDER BY h.ts DESC LIMIT 1) AS latest_ts
        FROM device_attributes a
        WHERE a.ieee = ? AND a.endpoint_id = ? AND a.cluster_id = ?
        ORDER BY a.attribute_id
    """, [ieee, endpoint_id, cluster_id]).fetchall()

    if not rows:
        return {
            "success": True,
            "cached": False,
            "ieee": ieee,
            "endpoint_id": endpoint_id,
            "cluster_id": f"0x{cluster_id:04X}",
            "attributes": [],
        }

    attributes = []
    for (attr_id, name, dtype, readable, writable,
         last_discovered, latest_value, latest_ts) in rows:
        attributes.append({
            "id": f"0x{attr_id:04X}",
            "id_int": attr_id,
            "name": name,
            "type": dtype,
            "readable": readable,
            "writable": writable,
            "value": latest_value,
            "last_discovered": str(last_discovered) if last_discovered else None,
            "last_value_ts": str(latest_ts) if latest_ts else None,
        })

    return {
        "success": True,
        "cached": True,
        "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": f"0x{cluster_id:04X}",
        "attributes": attributes,
    }


# ============================================================================
# READ: ATTRIBUTE HISTORY
# ============================================================================

def get_attribute_history(
        ieee: str,
        endpoint_id: int,
        cluster_id: int,
        attribute_id: int,
        since_seconds: int = 86400,
        limit: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Return time-series history for a single attribute.

    since_seconds: window in seconds back from now (default 24h)
    limit: max rows returned
    """
    _init_schema()
    db = _get_db()

    rows = db.execute(f"""
        SELECT ts, value_text, value_numeric
        FROM attribute_history
        WHERE ieee = ?
          AND endpoint_id = ?
          AND cluster_id = ?
          AND attribute_id = ?
          AND ts >= now() - INTERVAL '{int(since_seconds)} seconds'
        ORDER BY ts DESC
        LIMIT ?
    """, [ieee, endpoint_id, cluster_id, attribute_id, limit]).fetchall()

    return [
        {
            "ts": str(ts),
            "value": value_text,
            "numeric": value_numeric,
        }
        for ts, value_text, value_numeric in rows
    ]


# ============================================================================
# MAINTENANCE
# ============================================================================

def purge_device(ieee: str) -> None:
    """Delete all cached data for a device (called on device_removed)."""
    _init_schema()
    db = _get_db()
    db.execute("DELETE FROM device_endpoints WHERE ieee = ?", [ieee])
    db.execute("DELETE FROM device_clusters WHERE ieee = ?", [ieee])
    db.execute("DELETE FROM device_attributes WHERE ieee = ?", [ieee])
    db.execute("DELETE FROM attribute_history WHERE ieee = ?", [ieee])
    logger.info(f"[{ieee}] Purged all cached data")


def prune_history(retention_days: int = 30) -> int:
    """
    Drop attribute_history rows older than retention_days.
    Metadata tables are kept indefinitely (they're small and stable).
    """
    _init_schema()
    db = _get_db()
    result = db.execute(f"""
        DELETE FROM attribute_history
        WHERE ts < now() - INTERVAL '{int(retention_days)} days'
    """)
    count = result.fetchone()
    logger.info(f"Pruned attribute_history older than {retention_days}d")
    return int(count[0]) if count else 0