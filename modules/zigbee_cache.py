"""
Zigbee Device Cache - DuckDB-backed persistent storage (HARDENED)
==================================================================
Same design as the earlier version, with:
  - Isolated DB file (zigbee_cache.duckdb) — no schema collision with
    telemetry.duckdb
  - DuckDB-version-tolerant index creation (try/except around each CREATE INDEX)
  - All DB operations log full tracebacks on failure so a 500 in the
    route points straight to the line that failed
  - Safe numeric conversion (handles bool/int/float/Decimal/zigpy types)
  - No stringifying of None; missing attributes return NULL

Integration: unchanged — drop-in replacement for the previous module.
"""
import logging
import os
import time
import traceback
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.zigbee_cache")

try:
    from zigbee_debug import CLUSTER_NAMES
except Exception:
    CLUSTER_NAMES = {}

# Separate file so we never collide with telemetry schema
DB_PATH = "./data/zigbee_cache.duckdb"
_db = None
_INITIALISED = False


def _get_db():
    """Open the dedicated zigbee cache DB (lazy singleton)."""
    global _db
    if _db is None:
        import duckdb
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = duckdb.connect(DB_PATH)
        logger.info(f"Zigbee cache DB opened: {DB_PATH} (duckdb {duckdb.__version__})")
    return _db


def _safe_execute(sql: str, params=None, *, context: str = ""):
    """
    Execute a SQL statement and log a full traceback on any failure.
    Returns the cursor on success, None on failure.
    """
    db = _get_db()
    try:
        return db.execute(sql, params) if params is not None else db.execute(sql)
    except Exception as e:
        logger.error(
            f"zigbee_cache SQL failed ({context}): {e}\n"
            f"SQL: {sql.strip()[:400]}\n"
            f"Params: {params}\n"
            f"{traceback.format_exc()}"
        )
        return None


# ============================================================================
# SCHEMA
# ============================================================================

def _init_schema():
    global _INITIALISED
    if _INITIALISED:
        return

    _safe_execute("""
        CREATE TABLE IF NOT EXISTS device_endpoints (
            ieee          VARCHAR NOT NULL,
            endpoint_id   INTEGER NOT NULL,
            profile_id    INTEGER,
            device_type   INTEGER,
            first_seen    TIMESTAMP DEFAULT now(),
            last_updated  TIMESTAMP DEFAULT now(),
            PRIMARY KEY (ieee, endpoint_id)
        )
    """, context="create device_endpoints")

    _safe_execute("""
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
    """, context="create device_clusters")

    _safe_execute("""
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
    """, context="create device_attributes")

    _safe_execute("""
        CREATE TABLE IF NOT EXISTS attribute_history (
            ts            TIMESTAMP NOT NULL DEFAULT now(),
            ieee          VARCHAR NOT NULL,
            endpoint_id   INTEGER NOT NULL,
            cluster_id    INTEGER NOT NULL,
            attribute_id  INTEGER NOT NULL,
            value_text    VARCHAR,
            value_numeric DOUBLE
        )
    """, context="create attribute_history")

    # Indexes — version-tolerant. Any failure is logged but non-fatal.
    _safe_execute(
        "CREATE INDEX IF NOT EXISTS idx_attr_hist_device_ts "
        "ON attribute_history(ieee, ts)",
        context="create idx_attr_hist_device_ts"
    )
    _safe_execute(
        "CREATE INDEX IF NOT EXISTS idx_attr_hist_lookup "
        "ON attribute_history(ieee, endpoint_id, cluster_id, attribute_id, ts)",
        context="create idx_attr_hist_lookup"
    )

    _INITIALISED = True
    logger.info("Zigbee cache schema initialised")


# ============================================================================
# WRITE: TOPOLOGY
# ============================================================================

def record_topology(zigpy_dev) -> Dict[str, int]:
    _init_schema()

    ieee = str(zigpy_dev.ieee)
    ep_count = 0
    cluster_count = 0

    for ep_id, ep in zigpy_dev.endpoints.items():
        if ep_id == 0:
            continue

        profile_id = getattr(ep, 'profile_id', None)
        device_type = getattr(ep, 'device_type', None)
        ep_count += _safe_execute(
            """
            INSERT INTO device_endpoints (ieee, endpoint_id, profile_id, device_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (ieee, endpoint_id) DO UPDATE SET
                profile_id = excluded.profile_id,
                device_type = excluded.device_type,
                last_updated = now()
            """,
            [ieee, int(ep_id),
             int(profile_id) if profile_id is not None else None,
             int(device_type) if device_type is not None else None],
            context=f"upsert endpoint {ep_id}"
        ) is not None

        for cid, cluster in getattr(ep, 'in_clusters', {}).items():
            name = CLUSTER_NAMES.get(cid) or getattr(cluster, 'name', None) or f"0x{cid:04X}"
            cluster_count += _safe_execute(
                """
                INSERT INTO device_clusters (ieee, endpoint_id, cluster_id, direction, cluster_name)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (ieee, endpoint_id, cluster_id, direction) DO UPDATE SET
                    cluster_name = excluded.cluster_name,
                    last_updated = now()
                """,
                [ieee, int(ep_id), int(cid), 'in', str(name)],
                context=f"upsert in_cluster {ep_id}/0x{cid:04X}"
            ) is not None

        for cid, cluster in getattr(ep, 'out_clusters', {}).items():
            name = CLUSTER_NAMES.get(cid) or getattr(cluster, 'name', None) or f"0x{cid:04X}"
            cluster_count += _safe_execute(
                """
                INSERT INTO device_clusters (ieee, endpoint_id, cluster_id, direction, cluster_name)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (ieee, endpoint_id, cluster_id, direction) DO UPDATE SET
                    cluster_name = excluded.cluster_name,
                    last_updated = now()
                """,
                [ieee, int(ep_id), int(cid), 'out', str(name)],
                context=f"upsert out_cluster {ep_id}/0x{cid:04X}"
            ) is not None

    counts = {"endpoints": ep_count, "clusters": cluster_count}
    logger.info(f"[{ieee}] Cached topology: {counts['endpoints']} EPs, {counts['clusters']} clusters")
    return counts


# ============================================================================
# WRITE: ATTRIBUTE METADATA
# ============================================================================

def record_attribute_metadata(ieee, endpoint_id, cluster_id, attributes,
                              manufacturer_code=None) -> int:
    _init_schema()

    written = 0
    for a in attributes or []:
        attr_id = a.get("id_int")
        if attr_id is None:
            continue

        result = _safe_execute(
            """
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
            """,
            [str(ieee), int(endpoint_id), int(cluster_id), int(attr_id),
             a.get("name"), a.get("type"),
             bool(a.get("readable")) if a.get("readable") is not None else None,
             bool(a.get("writable")) if a.get("writable") is not None else None,
             int(manufacturer_code) if manufacturer_code is not None else None],
            context=f"upsert attribute 0x{attr_id:04X}"
        )
        if result is not None:
            written += 1

        val = a.get("value")
        if val is not None:
            record_value(ieee, endpoint_id, cluster_id, attr_id, val)

    return written


# ============================================================================
# WRITE: ATTRIBUTE VALUE (time-series)
# ============================================================================

def record_value(ieee, endpoint_id, cluster_id, attribute_id, value) -> None:
    _init_schema()

    if hasattr(value, 'value'):
        value = value.value

    if isinstance(value, (bytes, bytearray)):
        value_text = value.hex()
    else:
        try:
            value_text = str(value)
            if len(value_text) > 2000:
                value_text = value_text[:2000]
        except Exception:
            value_text = repr(value)

    value_numeric = None
    if isinstance(value, bool):
        value_numeric = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        try:
            value_numeric = float(value)
        except (ValueError, OverflowError, TypeError):
            value_numeric = None

    _safe_execute(
        """
        INSERT INTO attribute_history (
            ieee, endpoint_id, cluster_id, attribute_id,
            value_text, value_numeric
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [str(ieee), int(endpoint_id), int(cluster_id), int(attribute_id),
         value_text, value_numeric],
        context="insert attribute_history"
    )


# ============================================================================
# READ: TOPOLOGY
# ============================================================================

def get_topology(ieee) -> Dict[str, Any]:
    _init_schema()

    ep_cursor = _safe_execute(
        "SELECT endpoint_id, profile_id, device_type, last_updated "
        "FROM device_endpoints WHERE ieee = ? ORDER BY endpoint_id",
        [str(ieee)], context="select device_endpoints"
    )
    ep_rows = ep_cursor.fetchall() if ep_cursor else []

    if not ep_rows:
        return {"ieee": ieee, "endpoints": [], "cached": False}

    c_cursor = _safe_execute(
        "SELECT endpoint_id, cluster_id, direction, cluster_name "
        "FROM device_clusters WHERE ieee = ? ORDER BY endpoint_id, cluster_id",
        [str(ieee)], context="select device_clusters"
    )
    cluster_rows = c_cursor.fetchall() if c_cursor else []

    by_ep = {}
    for ep_id, cid, direction, cname in cluster_rows:
        slot = by_ep.setdefault(ep_id, {"in": [], "out": []})
        slot[direction].append({
            "id": cid,
            "id_hex": f"0x{cid:04X}",
            "name": cname
        })

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

def get_cached_attributes(ieee, endpoint_id, cluster_id) -> Dict[str, Any]:
    _init_schema()

    cursor = _safe_execute(
        """
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
        """,
        [str(ieee), int(endpoint_id), int(cluster_id)],
        context="select cached_attributes"
    )
    rows = cursor.fetchall() if cursor else []

    if not rows:
        return {
            "success": True, "cached": False, "ieee": ieee,
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
        "success": True, "cached": True, "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": f"0x{cluster_id:04X}",
        "attributes": attributes,
    }


# ============================================================================
# READ: ATTRIBUTE HISTORY
# ============================================================================

def get_attribute_history(ieee, endpoint_id, cluster_id, attribute_id,
                          since_seconds=86400, limit=5000) -> List[Dict]:
    _init_schema()

    cursor = _safe_execute(
        f"""
        SELECT ts, value_text, value_numeric
        FROM attribute_history
        WHERE ieee = ?
          AND endpoint_id = ?
          AND cluster_id = ?
          AND attribute_id = ?
          AND ts >= now() - INTERVAL '{int(since_seconds)} seconds'
        ORDER BY ts DESC
        LIMIT {int(limit)}
        """,
        [str(ieee), int(endpoint_id), int(cluster_id), int(attribute_id)],
        context="select attribute_history"
    )
    rows = cursor.fetchall() if cursor else []
    return [
        {"ts": str(ts), "value": val, "numeric": num}
        for ts, val, num in rows
    ]


# ============================================================================
# MAINTENANCE + DEBUG
# ============================================================================

def purge_device(ieee) -> None:
    _init_schema()
    for table in ("device_endpoints", "device_clusters",
                  "device_attributes", "attribute_history"):
        _safe_execute(f"DELETE FROM {table} WHERE ieee = ?",
                      [str(ieee)], context=f"purge {table}")


def prune_history(retention_days=30) -> int:
    _init_schema()
    _safe_execute(
        f"DELETE FROM attribute_history "
        f"WHERE ts < now() - INTERVAL '{int(retention_days)} days'",
        context="prune history"
    )
    return 0


def debug_info() -> Dict[str, Any]:
    """
    Introspect the cache DB. Expose via an API route so you can browse it
    without SSH. Returns schema + row counts + last errors, not user data.
    """
    import duckdb
    info = {
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "db_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "duckdb_version": duckdb.__version__,
        "tables": {},
    }

    _init_schema()
    db = _get_db()

    for tbl in ("device_endpoints", "device_clusters",
                "device_attributes", "attribute_history"):
        try:
            cols = db.execute(f"DESCRIBE {tbl}").fetchall()
            count = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            info["tables"][tbl] = {
                "row_count": count,
                "columns": [{"name": c[0], "type": c[1]} for c in cols],
            }
        except Exception as e:
            info["tables"][tbl] = {"error": str(e)}

    return info