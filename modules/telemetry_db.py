"""
Telemetry Database - DuckDB-backed time-series storage
=======================================================
Replaces scattered JSON/in-memory persistence with a single analytical DB.

Tables:
  system_metrics   — CPU, memory, temperature, disk (sampled every 30s)
  packet_stats     — per-device RX/TX/error counters (flushed every 60s)
  device_states    — device attribute changes (on state change only)
  spectrum_scans   — channel energy levels (per background scan)

Retention: configurable per table, default 7 days.
Location:  ./data/telemetry.duckdb

DuckDB was chosen over SQLite because:
  - Columnar storage is 5-10x more efficient for time-series aggregation
  - Automatic compression (ZSTD) keeps disk usage low
  - Concurrent reads don't block writes
  - Built-in time-bucket aggregation functions
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.telemetry_db")

DB_PATH = "./data/telemetry.duckdb"
DEFAULT_RETENTION_DAYS = 90

# Lazy import — duckdb is only needed when this module is used
_db = None


# ── Optional Rust appender ──
# Backend selection precedence (highest first):
#   1. ZMM_TELEMETRY_BACKEND=python  → force Python executemany fallback
#   2. zmm_telemetry wheel installed → use Rust appender
#   3. Otherwise                     → Python executemany fallback
#
# This lets you revert from Rust to Python without rebuilding the image:
# just set ZMM_TELEMETRY_BACKEND=python in the systemd unit / container env
# and restart. Schema is identical between backends, so the existing
# telemetry.duckdb file continues to work either way.
_FORCE_PY = os.environ.get("ZMM_TELEMETRY_BACKEND", "").strip().lower() == "python"
try:
    if _FORCE_PY:
        raise ImportError("ZMM_TELEMETRY_BACKEND=python — forcing Python fallback")
    import zmm_telemetry as _zt
    _USE_RUST = True
except ImportError as _imp_err:
    _zt = None
    _USE_RUST = False
    if _FORCE_PY:
        logger.info("zmm_telemetry disabled by ZMM_TELEMETRY_BACKEND=python — using Python executemany fallback")
    else:
        logger.info("zmm_telemetry not available — using Python executemany fallback")

_appender = None  # zmm_telemetry.Appender singleton

def _get_db():
    """Get or create the DuckDB connection (lazy singleton)."""
    global _db
    if _db is None:
        import duckdb
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = duckdb.connect(DB_PATH)
        _init_tables(_db)
        # Initialise Rust appender against the now-existing tables
        global _appender
        if _USE_RUST and _appender is None:
            try:
                _appender = _zt.Appender(DB_PATH)
                logger.info(f"Telemetry: zmm_telemetry appender active ({DB_PATH})")
            except Exception as e:
                logger.warning(f"zmm_telemetry init failed, falling back to INSERT: {e}")
                _appender = None
        logger.info(f"Telemetry database opened: {DB_PATH}")
    return _db


def _init_tables(db):
    """Create tables if they don't exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS system_metrics (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            cpu_percent FLOAT,
            cpu_freq    FLOAT,
            mem_total   BIGINT,
            mem_used    BIGINT,
            mem_percent FLOAT,
            swap_used   BIGINT,
            swap_percent FLOAT,
            disk_total  BIGINT,
            disk_used   BIGINT,
            disk_percent FLOAT,
            cpu_temp    FLOAT,
            gpu_temp    FLOAT,
            load_1m     FLOAT,
            load_5m     FLOAT,
            load_15m    FLOAT,
            uptime_secs BIGINT,
            process_rss BIGINT,
            process_threads INTEGER
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS packet_stats (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            ieee        VARCHAR NOT NULL,
            rx_packets  BIGINT DEFAULT 0,
            tx_packets  BIGINT DEFAULT 0,
            rx_bytes    BIGINT DEFAULT 0,
            tx_bytes    BIGINT DEFAULT 0,
            errors      INTEGER DEFAULT 0,
            retries     INTEGER DEFAULT 0,
            lqi         INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS device_states (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            ieee        VARCHAR NOT NULL,
            attribute   VARCHAR NOT NULL,
            value       VARCHAR,
            numeric_val DOUBLE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS spectrum_scans (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            channel     INTEGER NOT NULL,
            energy      INTEGER NOT NULL
        )
    """)

    db.execute("""
            CREATE TABLE IF NOT EXISTS heating_tick_rooms (
                ts                  TIMESTAMP NOT NULL DEFAULT now(),
                circuit_id          VARCHAR NOT NULL,
                room_id             VARCHAR NOT NULL,
                classification      VARCHAR,
                current_temp_c      DOUBLE,
                setpoint_c          DOUBLE,
                outdoor_temp_c      DOUBLE,
                calling_for_heat    BOOLEAN,
                trv_setpoint_c      DOUBLE,
                trv_valve_open_pct  DOUBLE,
                dry_run             BOOLEAN DEFAULT FALSE,
                reason              VARCHAR
            )
        """)

    db.execute("""
            CREATE TABLE IF NOT EXISTS heating_tick_boiler (
                ts                  TIMESTAMP NOT NULL DEFAULT now(),
                circuit_id          VARCHAR NOT NULL,
                boiler_called       BOOLEAN NOT NULL,
                rooms_cold          INTEGER DEFAULT 0,
                rooms_ontarget      INTEGER DEFAULT 0,
                rooms_hot           INTEGER DEFAULT 0,
                receiver_command    VARCHAR,
                dry_run             BOOLEAN DEFAULT FALSE
            )
        """)

    logger.debug("Telemetry tables initialised")


# ============================================================================
# WRITE OPERATIONS
# ============================================================================

def write_system_metrics(metrics: Dict[str, Any]):
    """Insert a system metrics sample."""
    _get_db()  # ensure init
    if _appender is not None:
        _appender.append_system_metrics(metrics)
        return
    # ── Python fallback ──
    db = _get_db()
    db.executemany("""
        INSERT INTO system_metrics (
            cpu_percent, cpu_freq, mem_total, mem_used, mem_percent,
            swap_used, swap_percent, disk_total, disk_used, disk_percent,
            cpu_temp, gpu_temp, load_1m, load_5m, load_15m,
            uptime_secs, process_rss, process_threads
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(
        metrics.get("cpu_percent"), metrics.get("cpu_freq"),
        metrics.get("mem_total"), metrics.get("mem_used"), metrics.get("mem_percent"),
        metrics.get("swap_used"), metrics.get("swap_percent"),
        metrics.get("disk_total"), metrics.get("disk_used"), metrics.get("disk_percent"),
        metrics.get("cpu_temp"), metrics.get("gpu_temp"),
        metrics.get("load_1m"), metrics.get("load_5m"), metrics.get("load_15m"),
        metrics.get("uptime_secs"), metrics.get("process_rss"), metrics.get("process_threads"),
    )])


def write_packet_stats(stats_batch: List[Dict[str, Any]]):
    """Bulk insert packet stats snapshot for all devices."""
    if not stats_batch:
        return
    _get_db()
    if _appender is not None:
        for s in stats_batch:
            _appender.append_packet_stats(
                s["ieee"],
                int(s.get("rx_packets", 0)), int(s.get("tx_packets", 0)),
                int(s.get("rx_bytes", 0)),   int(s.get("tx_bytes", 0)),
                int(s.get("errors", 0)),     int(s.get("retries", 0)),
                int(s.get("lqi", 0)),
            )
        return
    # ── Python fallback ──
    db = _get_db()
    db.executemany("""
        INSERT INTO packet_stats (ieee, rx_packets, tx_packets, rx_bytes, tx_bytes, errors, retries, lqi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (s["ieee"], s.get("rx_packets", 0), s.get("tx_packets", 0),
         s.get("rx_bytes", 0), s.get("tx_bytes", 0),
         s.get("errors", 0), s.get("retries", 0), s.get("lqi", 0))
        for s in stats_batch
    ])


def write_device_state(ieee: str, attribute: str, value: Any):
    """Record a device attribute change."""
    _get_db()
    str_val = str(value) if value is not None else None
    num_val = None
    try:
        num_val = float(value)
    except (TypeError, ValueError):
        pass
    if _appender is not None:
        _appender.append_device_state(ieee, attribute, str_val, num_val)
        return
    # ── Python fallback ──
    db = _get_db()
    db.executemany("""
        INSERT INTO device_states (ieee, attribute, value, numeric_val)
        VALUES (?, ?, ?, ?)
    """, [(ieee, attribute, str_val, num_val)])


def write_spectrum_scan(results: Dict[int, int]):
    """Persist a spectrum scan (channel → energy)."""
    if not results:
        return
    _get_db()
    if _appender is not None:
        for ch, e in results.items():
            _appender.append_spectrum_scan(int(ch), int(e))
        return
    # ── Python fallback ──
    db = _get_db()
    db.executemany("""
        INSERT INTO spectrum_scans (channel, energy) VALUES (?, ?)
    """, [(int(ch), int(e)) for ch, e in results.items()])


def write_heating_tick(
        ts: float,
        dry_run: bool,
        circuits: List[Dict[str, Any]],
) -> None:
    """
    Persist one controller tick for later analysis.
    ... (docstring unchanged) ...
    """
    if not circuits:
        return

    _get_db()  # ensure init (creates tables, initialises appender)

    # Prepare flat row tuples in one pass. Shape is the same regardless of
    # backend — the branch below decides whether to call the Rust appender
    # or fall back to Python INSERT.
    import datetime as _dt
    tick_dt = _dt.datetime.fromtimestamp(ts)

    room_rows = []
    boiler_rows = []

    for c in circuits:
        cid = str(c.get("id") or "")
        if not cid:
            continue

        recv = c.get("receiver_action") or {}
        recv_cmd = recv.get("command") if isinstance(recv, dict) else None

        rooms = c.get("rooms") or []
        n_cold = sum(1 for r in rooms if r.get("status") == "cold")
        n_ok   = sum(1 for r in rooms if r.get("status") == "ontarget")
        n_hot  = sum(1 for r in rooms if r.get("status") == "hot")

        boiler_rows.append({
            "circuit_id": cid,
            "boiler_called": bool(c.get("calling_for_heat")),
            "rooms_cold": n_cold,
            "rooms_ontarget": n_ok,
            "rooms_hot": n_hot,
            "receiver_command": recv_cmd,
        })

        for r in rooms:
            rid = str(r.get("room_id") or "")
            if not rid:
                continue

            trvs = r.get("trvs") or []
            trv_sp = None
            trv_open = None
            if trvs:
                first = trvs[0] if isinstance(trvs[0], dict) else {}
                trv_sp = first.get("intended_setpoint")
                for k in ("valve_opening_degree", "pi_heating_demand",
                          "valve_open_degree"):
                    if first.get(k) is not None:
                        try:
                            trv_open = float(first[k])
                        except (TypeError, ValueError):
                            pass
                        break

            room_rows.append({
                "circuit_id": cid,
                "room_id": rid,
                "classification": r.get("status"),
                "current_temp_c": r.get("current_temp"),
                "setpoint_c": r.get("target_temp"),
                "outdoor_temp_c": None,
                "calling_for_heat": bool(r.get("calling_for_heat")),
                "trv_setpoint_c": trv_sp,
                "trv_valve_open_pct": trv_open,
                "reason": r.get("temp_source"),
            })

    # ── Fast path: Rust appender ──
    if _appender is not None:
        try:
            for row in room_rows:
                _appender.append_heating_room(
                    ts,
                    row["circuit_id"], row["room_id"],
                    row["classification"],
                    _to_float(row["current_temp_c"]),
                    _to_float(row["setpoint_c"]),
                    _to_float(row["outdoor_temp_c"]),
                    row["calling_for_heat"],
                    _to_float(row["trv_setpoint_c"]),
                    _to_float(row["trv_valve_open_pct"]),
                    dry_run,
                    row["reason"],
                )
            for row in boiler_rows:
                _appender.append_heating_boiler(
                    ts,
                    row["circuit_id"],
                    row["boiler_called"],
                    int(row["rooms_cold"]),
                    int(row["rooms_ontarget"]),
                    int(row["rooms_hot"]),
                    row["receiver_command"],
                    dry_run,
                )
            return
        except Exception as e:
            logger.error(f"write_heating_tick appender failed, falling back: {e}")
            # fall through to Python INSERT

    # ── Python fallback ──
    db = _get_db()
    try:
        if room_rows:
            db.executemany("""
                INSERT INTO heating_tick_rooms (
                    ts, circuit_id, room_id, classification,
                    current_temp_c, setpoint_c, outdoor_temp_c,
                    calling_for_heat, trv_setpoint_c, trv_valve_open_pct,
                    dry_run, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (tick_dt, r["circuit_id"], r["room_id"], r["classification"],
                 r["current_temp_c"], r["setpoint_c"], r["outdoor_temp_c"],
                 r["calling_for_heat"], r["trv_setpoint_c"], r["trv_valve_open_pct"],
                 dry_run, r["reason"])
                for r in room_rows
            ])
        if boiler_rows:
            db.executemany("""
                INSERT INTO heating_tick_boiler (
                    ts, circuit_id, boiler_called,
                    rooms_cold, rooms_ontarget, rooms_hot,
                    receiver_command, dry_run
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (tick_dt, r["circuit_id"], r["boiler_called"],
                 r["rooms_cold"], r["rooms_ontarget"], r["rooms_hot"],
                 r["receiver_command"], dry_run)
                for r in boiler_rows
            ])
    except Exception as e:
        logger.error(f"write_heating_tick failed: {e}", exc_info=True)


def _to_float(v):
    """Helper — coerce to Optional[float] for the Rust appender."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# ============================================================================
# READ OPERATIONS
# ============================================================================

def query_system_metrics(hours: int = 1, bucket_minutes: int = 1) -> List[Dict]:
    """
    Get system metrics aggregated by time bucket.
    Returns one row per bucket with averaged values.
    """
    db = _get_db()
    result = db.execute(f"""
        SELECT
            time_bucket(INTERVAL '{bucket_minutes} minutes', ts) AS bucket,
            AVG(cpu_percent) AS cpu_percent,
            AVG(mem_percent) AS mem_percent,
            MAX(mem_used) AS mem_used,
            AVG(cpu_temp) AS cpu_temp,
            AVG(gpu_temp) AS gpu_temp,
            AVG(load_1m) AS load_1m,
            AVG(load_5m) AS load_5m,
            AVG(swap_percent) AS swap_percent,
            AVG(disk_percent) AS disk_percent,
            MAX(process_rss) AS process_rss,
            MAX(process_threads) AS process_threads
        FROM system_metrics
        WHERE ts >= now() - INTERVAL '{hours} hours'
        GROUP BY bucket
        ORDER BY bucket ASC
    """).fetchall()

    columns = ["ts", "cpu_percent", "mem_percent", "mem_used", "cpu_temp",
               "gpu_temp", "load_1m", "load_5m", "swap_percent", "disk_percent",
               "process_rss", "process_threads"]
    return [dict(zip(columns, row)) for row in result]


def query_packet_stats(ieee: Optional[str] = None, hours: int = 1) -> List[Dict]:
    """Get packet stats history for a device or all devices."""
    db = _get_db()
    hours = int(hours)
    if ieee:
        result = db.execute(f"""
            SELECT ts, ieee, rx_packets, tx_packets, errors, retries, lqi
            FROM packet_stats
            WHERE ieee = ? AND ts >= now() - INTERVAL '{hours} hours'
            ORDER BY ts ASC
        """, [ieee]).fetchall()
    else:
        result = db.execute(f"""
            SELECT
                time_bucket(INTERVAL '5 minutes', ts) AS bucket,
                SUM(rx_packets) AS rx_packets,
                SUM(tx_packets) AS tx_packets,
                SUM(errors) AS errors
            FROM packet_stats
            WHERE ts >= now() - INTERVAL '{hours} hours'
            GROUP BY bucket
            ORDER BY bucket ASC
        """).fetchall()

    if ieee:
        cols = ["ts", "ieee", "rx_packets", "tx_packets", "errors", "retries", "lqi"]
    else:
        cols = ["ts", "rx_packets", "tx_packets", "errors"]
    return [dict(zip(cols, row)) for row in result]


def query_device_state_history(ieee: str, attribute: str, hours: int = 24) -> List[Dict]:
    """Get state change history for a specific device attribute."""
    db = _get_db()
    hours = int(hours)
    result = db.execute(f"""
        SELECT ts, value, numeric_val
        FROM device_states
        WHERE ieee = ? AND attribute = ? AND ts >= now() - INTERVAL '{hours} hours'
        ORDER BY ts ASC
    """, [ieee, attribute]).fetchall()
    return [{"ts": r[0], "value": r[1], "numeric_val": r[2]} for r in result]

def query_device_attributes(ieee: str, hours: int = 168) -> List[str]:
    """Distinct attribute names recorded for a device within lookback."""
    db = _get_db()
    hours = int(hours)
    result = db.execute(f"""
        SELECT DISTINCT attribute
        FROM device_states
        WHERE ieee = ? AND ts >= now() - INTERVAL '{hours} hours'
        ORDER BY attribute
    """, [ieee]).fetchall()
    return [r[0] for r in result]


def query_device_state_bucketed(ieee: str, attribute: str,
                                hours: int = 24,
                                bucket_minutes: int = 5) -> List[Dict]:
    """
    Time-bucketed aggregation of a numeric attribute for chart rendering.
    Falls back to the last string value per bucket for non-numeric attrs.
    """
    db = _get_db()
    hours = int(hours)
    bucket_minutes = int(bucket_minutes)
    result = db.execute(f"""
        SELECT
            time_bucket(INTERVAL '{bucket_minutes} minutes', ts) AS bucket,
            AVG(numeric_val) AS avg_val,
            MIN(numeric_val) AS min_val,
            MAX(numeric_val) AS max_val,
            COUNT(*) AS samples,
            ANY_VALUE(value) AS last_str
        FROM device_states
        WHERE ieee = ? AND attribute = ?
          AND ts >= now() - INTERVAL '{hours} hours'
        GROUP BY bucket
        ORDER BY bucket ASC
    """, [ieee, attribute]).fetchall()
    cols = ["ts", "avg", "min", "max", "samples", "last_str"]
    return [dict(zip(cols, row)) for row in result]


def query_spectrum_history(hours: int = 24) -> List[Dict]:
    """Get spectrum scan history."""
    db = _get_db()
    hours = int(hours)
    result = db.execute(f"""
        SELECT ts, channel, energy
        FROM spectrum_scans
        WHERE ts >= now() - INTERVAL '{hours} hours'
        ORDER BY ts ASC
    """).fetchall()
    return [{"ts": r[0], "channel": r[1], "energy": r[2]} for r in result]


def get_db_stats() -> Dict[str, Any]:
    """Get database size and row counts per table."""
    db = _get_db()
    stats = {}
    for table in ["system_metrics", "packet_stats", "device_states", "spectrum_scans"]:
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stats[table] = count

    # File size
    try:
        stats["file_size_mb"] = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
    except OSError:
        stats["file_size_mb"] = 0

    return stats


def query_room_heating_state(
        circuit_id: str,
        room_id: str,
        hours: int = 14 * 24,
) -> List[Dict[str, Any]]:
    """
    Return per-tick heating state for a room over the last N hours.

    Used by the heating anomaly watcher to build a heating_state_getter(ts)
    closure, so baseline-τ fitting can reject cool-down windows that
    overlapped a period when heating was actively running.

    Rows are returned in ascending ts order (oldest first) to make bisect
    lookups cheap on the caller side. Dry-run ticks are excluded because
    their decisions didn't actually drive TRVs or the boiler.

    The 'heating_active' column is derived: a room counts as "being heated"
    if it was calling for heat OR its TRV valve was reported open. Either
    signal is sufficient — we want a conservative gate (bias toward
    rejecting windows, not including contaminated ones).
    """
    db = _get_db()
    hours = int(hours)
    rows = db.execute(f"""
        SELECT
            ts,
            calling_for_heat,
            trv_valve_open_pct,
            classification,
            current_temp_c,
            setpoint_c,
            (
                COALESCE(calling_for_heat, FALSE)
                OR COALESCE(trv_valve_open_pct, 0) > 0
            ) AS heating_active
        FROM heating_tick_rooms
        WHERE circuit_id = ?
          AND room_id = ?
          AND ts >= now() - INTERVAL '{hours} hours'
          AND dry_run = FALSE
        ORDER BY ts ASC
    """, [circuit_id, room_id]).fetchall()

    cols = ["ts", "calling_for_heat", "trv_valve_open_pct",
            "classification", "current_temp_c", "setpoint_c", "heating_active"]
    return [dict(zip(cols, r)) for r in rows]

# ============================================================================
# MAINTENANCE
# ============================================================================

def prune(retention_days: int = DEFAULT_RETENTION_DAYS):
    """Remove records older than retention period."""
    db = _get_db()
    cutoff = f"{retention_days} days"
    for table in ["system_metrics", "packet_stats", "device_states", "spectrum_scans"]:
        deleted = db.execute(
            f"DELETE FROM {table} WHERE ts < now() - INTERVAL '{cutoff}'"
        ).fetchone()
        logger.debug(f"Pruned {table}: retention={retention_days}d")

    logger.info(f"Telemetry pruned (retention={retention_days} days)")


def flush_appender():
    """Drain the Rust appender's row buffers to disk. No-op when fallback active."""
    if _appender is not None:
        try:
            _appender.flush()
        except Exception as e:
            logger.warning(f"Appender flush failed: {e}")

def close():
    """Close the database connection."""
    global _db, _appender
    if _appender is not None:
        try:
            _appender.flush()
        except Exception as e:
            logger.warning(f"Final appender flush failed: {e}")
        _appender = None
    if _db:
        _db.close()
        _db = None
        logger.info("Telemetry database closed")