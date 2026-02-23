"""
modules/spectrum_monitor.py

Background spectrum scanner — runs periodic energy scans and stores
results in zigbee.db for historical analysis and interference correlation.
"""

import asyncio
import logging
import sqlite3
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from zigpy.application import ControllerApplication

logger = logging.getLogger(__name__)

DB_PATH = "zigbee.db"
DEFAULT_INTERVAL = 3600  # 1 hour
CHANNELS = list(range(11, 27))


# ============================================================================
# DATABASE
# ============================================================================

def init_spectrum_table(db_path: str = DB_PATH):
    """Create spectrum_history table if it doesn't exist."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spectrum_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                channel   INTEGER NOT NULL,
                energy    INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_spectrum_ts ON spectrum_history(timestamp)")
        conn.commit()


def save_scan(results: dict, db_path: str = DB_PATH):
    """
    Persist one scan's worth of channel→energy pairs.
    results: {channel(int): energy(int 0-255)}
    """
    ts = int(time.time())
    rows = [(ts, ch, int(energy)) for ch, energy in results.items()]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO spectrum_history (timestamp, channel, energy) VALUES (?,?,?)",
            rows
        )
        conn.commit()
    logger.debug(f"Spectrum scan saved: {len(rows)} channels at ts={ts}")


def get_history(hours: int = 24, db_path: str = DB_PATH) -> list:
    """
    Return scan records for the past N hours.
    Returns list of {timestamp, channel, energy} dicts.
    """
    since = int(time.time()) - (hours * 3600)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT timestamp, channel, energy FROM spectrum_history WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_channel_averages(hours: int = 24, db_path: str = DB_PATH) -> dict:
    """
    Return average energy per channel over the past N hours.
    Returns {channel: avg_energy}
    """
    since = int(time.time()) - (hours * 3600)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT channel, AVG(energy) as avg_energy FROM spectrum_history WHERE timestamp >= ? GROUP BY channel",
            (since,)
        ).fetchall()
    return {row[0]: round(row[1], 1) for row in rows}


def prune_old_records(keep_days: int = 7, db_path: str = DB_PATH):
    """Remove records older than keep_days to prevent unbounded growth."""
    cutoff = int(time.time()) - (keep_days * 86400)
    with sqlite3.connect(db_path) as conn:
        deleted = conn.execute(
            "DELETE FROM spectrum_history WHERE timestamp < ?", (cutoff,)
        ).rowcount
        conn.commit()
    if deleted:
        logger.info(f"Spectrum history pruned: {deleted} old records removed")


# ============================================================================
# BACKGROUND TASK
# ============================================================================

class SpectrumMonitor:
    """
    Runs periodic background energy scans and stores results in zigbee.db.
    Attach to zigbee_service after radio start.
    """

    def __init__(self, app_getter, interval: int = DEFAULT_INTERVAL, db_path: str = DB_PATH):
        """
        Args:
            app_getter: callable returning the zigpy ControllerApplication (or None)
            interval:   seconds between scans (default 3600 = 1 hour)
            db_path:    SQLite database path
        """
        self._get_app = app_getter
        self.interval = interval
        self.db_path = db_path
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_scan: Optional[dict] = None
        self.last_scan_ts: Optional[int] = None

        init_spectrum_table(db_path)

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

            save_scan(clean, self.db_path)
            prune_old_records(keep_days=7, db_path=self.db_path)

            self.last_scan = clean
            self.last_scan_ts = int(time.time())

            logger.info(f"Background spectrum scan complete — best channel estimate: "
                        f"{min(clean, key=lambda c: clean[c]) if clean else 'N/A'}")
            return clean

        except Exception as e:
            logger.warning(f"Background spectrum scan failed: {e}")
            return {}