"""
packet_stats.py
Per-device packet load statistics tracking for mesh analysis.
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional
import threading


@dataclass
class DeviceStats:
    """Statistics for a single device."""
    ieee: str
    rx_packets: int = 0
    tx_packets: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    last_rx: float = 0.0
    last_tx: float = 0.0
    errors: int = 0
    retries: int = 0
    # Rolling window for rate calculation (last 60 seconds)
    rx_timestamps: list = field(default_factory=list)
    tx_timestamps: list = field(default_factory=list)

    def record_rx(self, size: int = 0):
        """Record a received packet."""
        now = time.time()
        self.rx_packets += 1
        self.rx_bytes += size
        self.last_rx = now
        self.rx_timestamps.append(now)
        self._trim_timestamps()

    def record_tx(self, size: int = 0):
        """Record a transmitted packet."""
        now = time.time()
        self.tx_packets += 1
        self.tx_bytes += size
        self.last_tx = now
        self.tx_timestamps.append(now)
        self._trim_timestamps()

    def record_error(self):
        """Record a transmission error."""
        self.errors += 1

    def record_retry(self):
        """Record a retry attempt."""
        self.retries += 1

    def _trim_timestamps(self, window: int = 60):
        """Keep only timestamps within the rolling window."""
        cutoff = time.time() - window
        self.rx_timestamps = [t for t in self.rx_timestamps if t > cutoff]
        self.tx_timestamps = [t for t in self.tx_timestamps if t > cutoff]

    def get_rx_rate(self, window: int = 60) -> float:
        """Get packets/minute received rate."""
        self._trim_timestamps(window)
        if not self.rx_timestamps:
            return 0.0
        return len(self.rx_timestamps) * (60 / window)

    def get_tx_rate(self, window: int = 60) -> float:
        """Get packets/minute transmitted rate."""
        self._trim_timestamps(window)
        if not self.tx_timestamps:
            return 0.0
        return len(self.tx_timestamps) * (60 / window)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "ieee": self.ieee,
            "rx_packets": self.rx_packets,
            "tx_packets": self.tx_packets,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "total_packets": self.rx_packets + self.tx_packets,
            "rx_rate_per_min": round(self.get_rx_rate(), 2),
            "tx_rate_per_min": round(self.get_tx_rate(), 2),
            "last_rx": self.last_rx,
            "last_tx": self.last_tx,
            "errors": self.errors,
            "retries": self.retries,
            "error_rate": round(self.errors / max(self.tx_packets, 1) * 100, 2)
        }


class PacketStatisticsTracker:
    """
    Global packet statistics tracker.
    Thread-safe tracking of per-device packet load.
    """

    def __init__(self):
        self._stats: Dict[str, DeviceStats] = {}
        self._lock = threading.Lock()
        self._start_time = time.time()

    def _get_or_create(self, ieee: str) -> DeviceStats:
        """Get or create stats for a device."""
        if ieee not in self._stats:
            self._stats[ieee] = DeviceStats(ieee=ieee)
        return self._stats[ieee]

    def record_rx(self, ieee: str, size: int = 0):
        """Record a received packet from a device."""
        with self._lock:
            self._get_or_create(ieee).record_rx(size)

    def record_tx(self, ieee: str, size: int = 0):
        """Record a transmitted packet to a device."""
        with self._lock:
            self._get_or_create(ieee).record_tx(size)

    def record_error(self, ieee: str):
        """Record an error for a device."""
        with self._lock:
            self._get_or_create(ieee).record_error()

    def record_retry(self, ieee: str):
        """Record a retry for a device."""
        with self._lock:
            self._get_or_create(ieee).record_retry()

    def get_device_stats(self, ieee: str) -> Optional[dict]:
        """Get statistics for a specific device."""
        with self._lock:
            if ieee in self._stats:
                return self._stats[ieee].to_dict()
            return None

    def get_all_stats(self) -> Dict[str, dict]:
        """Get statistics for all devices."""
        with self._lock:
            return {ieee: stats.to_dict() for ieee, stats in self._stats.items()}

    def get_summary(self) -> dict:
        """Get summary statistics."""
        with self._lock:
            total_rx = sum(s.rx_packets for s in self._stats.values())
            total_tx = sum(s.tx_packets for s in self._stats.values())
            total_errors = sum(s.errors for s in self._stats.values())

            # Find busiest devices
            by_traffic = sorted(
                self._stats.values(),
                key=lambda s: s.rx_packets + s.tx_packets,
                reverse=True
            )

            uptime = time.time() - self._start_time

            return {
                "uptime_seconds": round(uptime, 0),
                "total_devices": len(self._stats),
                "total_rx_packets": total_rx,
                "total_tx_packets": total_tx,
                "total_errors": total_errors,
                "avg_packets_per_device": round((total_rx + total_tx) / max(len(self._stats), 1), 1),
                "busiest_devices": [s.ieee for s in by_traffic[:5]]
            }

    def reset(self):
        """Reset all statistics."""
        with self._lock:
            self._stats.clear()
            self._start_time = time.time()


# Global singleton
packet_stats = PacketStatisticsTracker()