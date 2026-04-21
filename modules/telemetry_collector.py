"""
Telemetry Collector - Bridges live data into DuckDB
=====================================================
Periodically flushes in-memory packet_stats to DuckDB and handles
device state change recording. Also runs daily retention pruning.

Hook into main.py after system_monitor and telemetry_db are ready.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("modules.telemetry_collector")

FLUSH_INTERVAL = 60          # seconds between packet stats flushes
PRUNE_INTERVAL = 86400       # seconds between retention prune runs (24h)
DEFAULT_RETENTION_DAYS = 30


class TelemetryCollector:
    """
    Bridges live in-memory data into the DuckDB telemetry store.

    Responsibilities:
      1. Periodic flush of packet_stats singleton → DuckDB
      2. Device state change recording (called from core.py)
      3. Daily retention pruning
    """

    def __init__(self, device_registry_getter: Callable,
                 retention_days: int = DEFAULT_RETENTION_DAYS):
        self._get_devices = device_registry_getter
        self._retention_days = retention_days
        self._flush_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_packet_snapshot: Dict[str, Dict] = {}

    def start(self):
        """Start background flush and prune tasks."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())
            self._prune_task = asyncio.create_task(self._prune_loop())
            logger.info("Telemetry collector started")

    def stop(self):
        """Stop background tasks."""
        self._running = False
        for task in (self._flush_task, self._prune_task):
            if task:
                task.cancel()

    async def _flush_loop(self):
        """Periodically flush packet stats to DuckDB."""
        await asyncio.sleep(30)  # Initial delay

        while self._running:
            try:
                self._flush_packet_stats()
            except Exception as e:
                logger.debug(f"Packet stats flush error: {e}")

            try:
                await asyncio.sleep(FLUSH_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _prune_loop(self):
        """Daily retention pruning."""
        await asyncio.sleep(3600)  # First prune after 1 hour

        while self._running:
            try:
                from modules.telemetry_db import prune
                prune(retention_days=self._retention_days)
            except Exception as e:
                logger.warning(f"Telemetry prune error: {e}")

            try:
                await asyncio.sleep(PRUNE_INTERVAL)
            except asyncio.CancelledError:
                break

    def _flush_packet_stats(self):
        """
        Read current packet_stats singleton, compute deltas since last
        flush, and write the deltas to DuckDB.
        """
        from modules.packet_stats import packet_stats
        from modules.telemetry_db import write_packet_stats

        current = packet_stats.get_all_stats()
        batch = []

        for ieee, stats in current.items():
            prev = self._last_packet_snapshot.get(ieee, {})

            # Compute deltas
            delta_rx = stats.get("rx_packets", 0) - prev.get("rx_packets", 0)
            delta_tx = stats.get("tx_packets", 0) - prev.get("tx_packets", 0)
            delta_rx_bytes = stats.get("rx_bytes", 0) - prev.get("rx_bytes", 0)
            delta_tx_bytes = stats.get("tx_bytes", 0) - prev.get("tx_bytes", 0)
            delta_errors = stats.get("errors", 0) - prev.get("errors", 0)
            delta_retries = stats.get("retries", 0) - prev.get("retries", 0)

            # Only write if there was activity
            if delta_rx > 0 or delta_tx > 0 or delta_errors > 0:
                # Get LQI from device if available
                lqi = 0
                try:
                    devices = self._get_devices()
                    dev = devices.get(ieee)
                    if dev and hasattr(dev, 'zigpy_dev'):
                        lqi = getattr(dev.zigpy_dev, 'lqi', 0) or 0
                except Exception:
                    pass

                batch.append({
                    "ieee": ieee,
                    "rx_packets": delta_rx,
                    "tx_packets": delta_tx,
                    "rx_bytes": delta_rx_bytes,
                    "tx_bytes": delta_tx_bytes,
                    "errors": delta_errors,
                    "retries": delta_retries,
                    "lqi": lqi,
                })

        if batch:
            write_packet_stats(batch)
            logger.debug(f"Flushed packet stats: {len(batch)} devices")

        # Store current as the baseline for next delta
        self._last_packet_snapshot = current

    def record_state_change(self, ieee: str, changed_attrs: Dict[str, Any]):
        """
        Called from core.py when a device state changes.
        Records individual attribute changes to DuckDB.

        Only records attributes that are interesting for history —
        skips metadata fields.
        """
        skip = {"last_seen", "available", "manufacturer", "model", "power_source"}

        try:
            from modules.telemetry_db import write_device_state
            for attr, value in changed_attrs.items():
                if attr in skip or attr.endswith("_raw") or attr.startswith("attr_"):
                    continue
                write_device_state(ieee, attr, value)
        except Exception as e:
            logger.debug(f"Device state recording error: {e}")