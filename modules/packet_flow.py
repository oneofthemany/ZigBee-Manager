"""
Packet Flow Analyzer
====================

Lightweight, in-memory packet rate tracking for the Zigbee + Matter network.

Records every packet that enters `ZigbeeDebugger.capture_packet` regardless of
whether full debug capture is enabled, and exposes:

  - Global packets-per-second over 1s / 10s / 60s windows
  - Per-device chattiness ranking
  - Per-cluster aggregate breakdown
  - Per-device EWMA-baseline anomaly detection
  - Per-second history for a 60s sparkline

Pure stdlib. No DB writes. No locks (single-thread asyncio).

Cost per `record()`: a dict lookup + 3 deque appends + 2 ints. O(1).
Pruning is amortised on read; a hard GC drops devices that go silent.
"""

from __future__ import annotations
import time
from collections import deque, defaultdict
from typing import Dict, List, Optional, Any


# --- Tuning knobs ----------------------------------------------------------
WINDOW_60S = 60.0
WINDOW_10S = 10.0
WINDOW_1S  = 1.0

# EWMA smoothing for per-device baseline (packets per minute).
# Smaller = slower-moving baseline, more memory of the past.
_EWMA_ALPHA = 0.05

# Anomaly thresholds:
#   - current rate must be >= ANOMALY_MIN_RATE per minute (filter quiet devices)
#   - and >= ANOMALY_RATIO × baseline
ANOMALY_MIN_RATE = 30.0   # 30/min = 0.5/s — well above any healthy idle device
ANOMALY_RATIO    = 4.0

# Garbage-collect a device entry if it's been silent and its baseline has
# decayed close to zero.
DEVICE_GC_BASELINE = 0.1


def _prune(d: deque, cutoff: float) -> None:
    """Drop entries older than cutoff. O(k) where k = stale count."""
    while d and d[0] < cutoff:
        d.popleft()


class PacketFlowAnalyzer:
    """
    Tracks raw packet timestamps in ring buffers keyed by (global / device /
    cluster), and computes rate/anomaly snapshots on demand.
    """

    def __init__(self) -> None:
        self._global: deque = deque()
        self._by_device: Dict[str, deque] = defaultdict(deque)
        self._by_cluster: Dict[int, deque] = defaultdict(deque)
        # Per-device EWMA of packets-per-minute.
        self._baseline: Dict[str, float] = {}
        # Direction counters (RX / TX), simple cumulative.
        self._dir_counts: Dict[str, int] = {"RX": 0, "TX": 0}
        # Total packets ever seen (cheap counter).
        self._total: int = 0
        # When we last updated baselines.
        self._last_baseline_tick: float = 0.0

    # ------------------------------------------------------------------
    # Recording (hot path)
    # ------------------------------------------------------------------
    def record(
            self,
            ieee: Optional[str],
            cluster: Optional[int],
            direction: str = "RX",
            timestamp: Optional[float] = None,
    ) -> None:
        """O(1). Called from capture_packet on every packet."""
        if timestamp is None:
            timestamp = time.time()
        self._global.append(timestamp)
        if ieee:
            self._by_device[ieee].append(timestamp)
        if cluster is not None:
            self._by_cluster[cluster].append(timestamp)
        self._dir_counts[direction] = self._dir_counts.get(direction, 0) + 1
        self._total += 1

    # ------------------------------------------------------------------
    # Pruning + windowed counting
    # ------------------------------------------------------------------
    def _prune_all(self, now: float) -> None:
        cutoff = now - WINDOW_60S
        _prune(self._global, cutoff)

        for ieee in list(self._by_device.keys()):
            dq = self._by_device[ieee]
            _prune(dq, cutoff)
            if not dq and self._baseline.get(ieee, 0.0) < DEVICE_GC_BASELINE:
                # Silent + decayed baseline — drop the entry to bound memory.
                self._by_device.pop(ieee, None)
                self._baseline.pop(ieee, None)

        for cid in list(self._by_cluster.keys()):
            dq = self._by_cluster[cid]
            _prune(dq, cutoff)
            if not dq:
                self._by_cluster.pop(cid, None)

    @staticmethod
    def _count_in_window(dq: deque, now: float, window: float) -> int:
        """Count entries in `dq` with ts >= now - window. Right-side scan."""
        cutoff = now - window
        c = 0
        for ts in reversed(dq):
            if ts >= cutoff:
                c += 1
            else:
                break
        return c

    def _update_baselines(self, now: float) -> None:
        """Update per-device EWMA baseline (packets per minute)."""
        # Tick at most once per second — keeps the snapshot path cheap.
        if now - self._last_baseline_tick < 1.0:
            return
        self._last_baseline_tick = now
        for ieee, dq in self._by_device.items():
            count_60s = self._count_in_window(dq, now, WINDOW_60S)
            current_pm = float(count_60s)  # window=60s → already per-minute
            if ieee not in self._baseline:
                # Seed with current observation so first sample doesn't anomaly.
                self._baseline[ieee] = current_pm
                continue
            old = self._baseline[ieee]
            self._baseline[ieee] = (
                    _EWMA_ALPHA * current_pm + (1.0 - _EWMA_ALPHA) * old
            )

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------
    def get_global_rate(self) -> Dict[str, Any]:
        now = time.time()
        self._prune_all(now)
        c1 = self._count_in_window(self._global, now, WINDOW_1S)
        c10 = self._count_in_window(self._global, now, WINDOW_10S)
        c60 = self._count_in_window(self._global, now, WINDOW_60S)
        return {
            "rate_1s": c1 / WINDOW_1S,
            "rate_10s": c10 / WINDOW_10S,
            "rate_60s": c60 / WINDOW_60S,
            "total": self._total,
            "rx": self._dir_counts.get("RX", 0),
            "tx": self._dir_counts.get("TX", 0),
            "tracked_devices": len(self._by_device),
            "tracked_clusters": len(self._by_cluster),
        }

    def get_device_rates(self, top_n: int = 20) -> List[Dict[str, Any]]:
        now = time.time()
        self._prune_all(now)
        rows: List[Dict[str, Any]] = []
        for ieee, dq in self._by_device.items():
            if not dq:
                continue
            c1 = self._count_in_window(dq, now, WINDOW_1S)
            c10 = self._count_in_window(dq, now, WINDOW_10S)
            c60 = self._count_in_window(dq, now, WINDOW_60S)
            rows.append({
                "ieee": ieee,
                "rate_1s": c1 / WINDOW_1S,
                "rate_10s": c10 / WINDOW_10S,
                "rate_60s": c60 / WINDOW_60S,
                "baseline": self._baseline.get(ieee, 0.0),
            })
        rows.sort(key=lambda r: r["rate_60s"], reverse=True)
        return rows[:top_n]

    def get_cluster_rates(self) -> List[Dict[str, Any]]:
        # Lazy import — avoids a circular when zigbee_debug imports this module.
        from modules.zigbee_debug import CLUSTER_NAMES
        now = time.time()
        self._prune_all(now)
        rows: List[Dict[str, Any]] = []
        for cid, dq in self._by_cluster.items():
            c10 = self._count_in_window(dq, now, WINDOW_10S)
            c60 = self._count_in_window(dq, now, WINDOW_60S)
            rows.append({
                "cluster": cid,
                "cluster_name": CLUSTER_NAMES.get(cid, f"0x{cid:04X}"),
                "rate_10s": c10 / WINDOW_10S,
                "rate_60s": c60 / WINDOW_60S,
            })
        rows.sort(key=lambda r: r["rate_60s"], reverse=True)
        return rows

    def get_anomalies(self) -> List[Dict[str, Any]]:
        now = time.time()
        self._prune_all(now)
        self._update_baselines(now)
        anoms: List[Dict[str, Any]] = []
        for ieee, dq in self._by_device.items():
            c60 = self._count_in_window(dq, now, WINDOW_60S)
            current_pm = float(c60)
            if current_pm < ANOMALY_MIN_RATE:
                continue
            baseline_pm = self._baseline.get(ieee, current_pm)
            if baseline_pm <= 0.5:
                # Effectively unknown baseline — flag if traffic is high.
                ratio = float("inf")
            else:
                ratio = current_pm / baseline_pm
            if ratio >= ANOMALY_RATIO:
                anoms.append({
                    "ieee": ieee,
                    "current": current_pm,
                    "baseline": baseline_pm,
                    "ratio": None if ratio == float("inf") else ratio,
                })
        # Highest ratio (or unknown=infinite, which we sort first) on top.
        anoms.sort(
            key=lambda a: (a["ratio"] is None, a["ratio"] or 0.0),
            reverse=True,
        )
        return anoms

    def get_history(self, seconds: int = 60) -> List[int]:
        """Per-second packet counts for the last `seconds` (oldest first)."""
        now = time.time()
        self._prune_all(now)
        end_bucket = int(now)
        start_bucket = end_bucket - seconds + 1
        buckets = [0] * seconds
        for ts in self._global:
            b = int(ts) - start_bucket
            if 0 <= b < seconds:
                buckets[b] += 1
        return buckets

    def get_snapshot(
            self,
            top_n: int = 10,
            history_seconds: int = 60,
    ) -> Dict[str, Any]:
        """One-shot read for periodic broadcast / API endpoint."""
        return {
            "global":    self.get_global_rate(),
            "devices":   self.get_device_rates(top_n=top_n),
            "clusters":  self.get_cluster_rates(),
            "anomalies": self.get_anomalies(),
            "history":   self.get_history(history_seconds),
            "ts":        time.time(),
        }

    def reset(self) -> None:
        self._global.clear()
        self._by_device.clear()
        self._by_cluster.clear()
        self._baseline.clear()
        self._dir_counts = {"RX": 0, "TX": 0}
        self._total = 0


# --- Singleton (mirrors the debugger pattern) -----------------------------
_analyzer: Optional[PacketFlowAnalyzer] = None


def get_flow_analyzer() -> PacketFlowAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = PacketFlowAnalyzer()
    return _analyzer