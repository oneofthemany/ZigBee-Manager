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


def _get_db():
    """Get or create the DuckDB connection (lazy singleton)."""
    global _db
    if _db is None:
        import duckdb
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = duckdb.connect(DB_PATH)
        _init_tables(_db)
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

    logger.debug("Telemetry tables initialised")


# ============================================================================
# WRITE OPERATIONS
# ============================================================================

def write_system_metrics(metrics: Dict[str, Any]):
    """Insert a system metrics sample."""
    db = _get_db()
    db.execute("""
        INSERT INTO system_metrics (
            cpu_percent, cpu_freq, mem_total, mem_used, mem_percent,
            swap_used, swap_percent, disk_total, disk_used, disk_percent,
            cpu_temp, gpu_temp, load_1m, load_5m, load_15m,
            uptime_secs, process_rss, process_threads
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        metrics.get("cpu_percent"),
        metrics.get("cpu_freq"),
        metrics.get("mem_total"),
        metrics.get("mem_used"),
        metrics.get("mem_percent"),
        metrics.get("swap_used"),
        metrics.get("swap_percent"),
        metrics.get("disk_total"),
        metrics.get("disk_used"),
        metrics.get("disk_percent"),
        metrics.get("cpu_temp"),
        metrics.get("gpu_temp"),
        metrics.get("load_1m"),
        metrics.get("load_5m"),
        metrics.get("load_15m"),
        metrics.get("uptime_secs"),
        metrics.get("process_rss"),
        metrics.get("process_threads"),
    ])


def write_packet_stats(stats_batch: List[Dict[str, Any]]):
    """Bulk insert packet stats snapshot for all devices."""
    if not stats_batch:
        return
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
    db = _get_db()
    str_val = str(value) if value is not None else None
    num_val = None
    try:
        num_val = float(value)
    except (TypeError, ValueError):
        pass
    db.execute("""
        INSERT INTO device_states (ieee, attribute, value, numeric_val)
        VALUES (?, ?, ?, ?)
    """, [ieee, attribute, str_val, num_val])


def write_spectrum_scan(results: Dict[int, int]):
    """Persist a spectrum scan (channel → energy)."""
    if not results:
        return
    db = _get_db()
    db.executemany("""
        INSERT INTO spectrum_scans (channel, energy) VALUES (?, ?)
    """, [(int(ch), int(e)) for ch, e in results.items()])


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


def close():
    """Close the database connection."""
    global _db
    if _db:
        _db.close()
        _db = None
        logger.info("Telemetry database closed")