"""
modules/spectrum_monitor.py

Background spectrum scanner — runs periodic energy scans and stores
results in DuckDB (via telemetry_db) for historical analysis and
interference correlation.

Migration from SQLite: This module previously stored data in zigbee.db.
On first run after migration, existing SQLite data is copied to DuckDB
automatically, then the SQLite table is left untouched (zigpy still
uses zigbee.db for its own tables).
"""

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from zigpy.application import ControllerApplication

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 3600  # 1 hour
CHANNELS = list(range(11, 27))
SQLITE_DB_PATH = "zigbee.db"
MIGRATION_MARKER = "./data/.spectrum_migrated"


# ============================================================================
# DUCKDB-BACKED FUNCTIONS (replace old SQLite versions)
# ============================================================================

def save_scan(results: dict, db_path: str = None):
    """
    Persist one scan's worth of channel→energy pairs to DuckDB.
    db_path parameter kept for backward compatibility but ignored.
    """
    if not results:
        return
    try:
        from modules.telemetry_db import write_spectrum_scan
        write_spectrum_scan(results)
        logger.debug(f"Spectrum scan saved: {len(results)} channels")
    except Exception as e:
        logger.warning(f"Failed to save spectrum scan: {e}")


def get_history(hours: int = 24, db_path: str = None) -> list:
    """
    Return scan records for the past N hours from DuckDB.
    Returns list of {timestamp, channel, energy} dicts.
    """
    try:
        from modules.telemetry_db import query_spectrum_history
        rows = query_spectrum_history(hours=hours)
        # Convert to the format the frontend expects
        return [
            {
                "timestamp": int(r["ts"].timestamp()) if hasattr(r["ts"], "timestamp") else int(r["ts"]),
                "channel": r["channel"],
                "energy": r["energy"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"Failed to query spectrum history: {e}")
        return []


def get_channel_averages(hours: int = 24, db_path: str = None) -> dict:
    """
    Return average energy per channel over the past N hours.
    Returns {channel: avg_energy}
    """
    try:
        from modules.telemetry_db import _get_db
        db = _get_db()
        result = db.execute(f"""
            SELECT channel, AVG(energy) as avg_energy
            FROM spectrum_scans
            WHERE ts >= now() - INTERVAL '{int(hours)} hours'
            GROUP BY channel
        """).fetchall()
        return {int(row[0]): round(float(row[1]), 1) for row in result}
    except Exception as e:
        logger.warning(f"Failed to query spectrum averages: {e}")
        return {}


def get_channel_stats(hours: int = 24, db_path: str = None) -> dict:
    """
    Return per-channel statistics over the past N hours.
    Returns {channel: {min, max, mean, stddev, p25, p75, median, count}}
    """
    import math

    try:
        from modules.telemetry_db import _get_db
        db = _get_db()
        rows = db.execute(f"""
            SELECT channel, energy
            FROM spectrum_scans
            WHERE ts >= now() - INTERVAL '{int(hours)} hours'
            ORDER BY channel, energy
        """).fetchall()
    except Exception as e:
        logger.warning(f"Failed to query spectrum stats: {e}")
        return {}

    # Group by channel
    by_channel = {}
    for ch, energy in rows:
        ch = int(ch)
        if ch not in by_channel:
            by_channel[ch] = []
        by_channel[ch].append(int(energy))

    stats = {}
    for ch in sorted(by_channel.keys()):
        vals = sorted(by_channel[ch])
        n = len(vals)
        if n == 0:
            continue

        mean_val = sum(vals) / n
        variance = sum((v - mean_val) ** 2 for v in vals) / n
        stddev = math.sqrt(variance)

        # Percentiles (nearest-rank)
        p25_idx = max(0, int(n * 0.25) - 1)
        p75_idx = min(n - 1, int(n * 0.75))
        med_idx = n // 2

        stats[ch] = {
            "min": vals[0],
            "max": vals[-1],
            "mean": round(mean_val, 1),
            "stddev": round(stddev, 1),
            "median": vals[med_idx],
            "p25": vals[p25_idx],
            "p75": vals[p75_idx],
            "count": n
        }

    return stats


def prune_old_records(keep_days: int = 7, db_path: str = None):
    """Prune is now handled by telemetry_db.prune() — this is a no-op for compatibility."""
    pass


# ============================================================================
# ONE-TIME MIGRATION FROM SQLITE
# ============================================================================

def _migrate_from_sqlite():
    """
    Copy existing spectrum_history data from zigbee.db (SQLite) to DuckDB.
    Runs once, then writes a marker file so it never runs again.
    """
    if os.path.isfile(MIGRATION_MARKER):
        return  # Already migrated

    if not os.path.isfile(SQLITE_DB_PATH):
        _write_migration_marker(0)
        return

    try:
        import sqlite3
        conn = sqlite3.connect(SQLITE_DB_PATH)

        # Check if the table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spectrum_history'"
        )
        if not cursor.fetchone():
            conn.close()
            _write_migration_marker(0)
            return

        # Read all existing data
        rows = conn.execute(
            "SELECT timestamp, channel, energy FROM spectrum_history ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()

        if not rows:
            _write_migration_marker(0)
            return

        # Write to DuckDB in batches
        from modules.telemetry_db import _get_db
        db = _get_db()

        batch_size = 1000
        total = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            db.executemany("""
                INSERT INTO spectrum_scans (ts, channel, energy)
                VALUES (to_timestamp(?), ?, ?)
            """, batch)
            total += len(batch)

        logger.info(f"Spectrum migration complete: {total} records copied from SQLite to DuckDB")
        _write_migration_marker(total)

    except Exception as e:
        logger.error(f"Spectrum migration failed: {e}")
        # Don't write marker — retry on next startup


def _write_migration_marker(count: int):
    """Write marker file to prevent re-migration."""
    os.makedirs(os.path.dirname(MIGRATION_MARKER), exist_ok=True)
    with open(MIGRATION_MARKER, "w") as f:
        f.write(f"migrated={count} ts={int(time.time())}\n")


# ============================================================================
# BACKGROUND TASK
# ============================================================================

class SpectrumMonitor:
    """
    Runs periodic background energy scans and stores results in DuckDB.
    Attach to zigbee_service after radio start.
    """

    def __init__(self, app_getter, interval: int = DEFAULT_INTERVAL, db_path: str = None):
        """
        Args:
            app_getter: callable returning the zigpy ControllerApplication (or None)
            interval:   seconds between scans (default 3600 = 1 hour)
            db_path:    Ignored (kept for backward compatibility)
        """
        self._get_app = app_getter
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_scan: Optional[dict] = None
        self.last_scan_ts: Optional[int] = None

        # Run one-time migration from SQLite
        _migrate_from_sqlite()

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info(f"Spectrum monitor started (interval={self.interval}s)")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def run_scan_now(self) -> dict:
        """Trigger an immediate scan outside of the schedule. Returns raw results."""
        return await self._do_scan()

    async def _loop(self):
        # Initial delay — wait for network to settle after startup
        await asyncio.sleep(60)

        while self._running:
            try:
                await self._do_scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Spectrum background scan error: {e}")

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def _do_scan(self) -> dict:
        app = self._get_app()
        if not app:
            logger.debug("Spectrum scan skipped — app not ready")
            return {}

        logger.info("Background spectrum scan starting...")
        try:
            results = await app.energy_scan(
                channels=range(11, 27),
                count=3,
                duration_exp=4
            )
            clean = {int(ch): int(e) for ch, e in results.items()}

            save_scan(clean)

            self.last_scan = clean
            self.last_scan_ts = int(time.time())

            logger.info(f"Background spectrum scan complete — best channel estimate: "
                        f"{min(clean, key=lambda c: clean[c]) if clean else 'N/A'}")
            return clean

        except Exception as e:
            logger.warning(f"Background spectrum scan failed: {e}")
            return {}