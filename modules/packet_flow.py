"""
Packet Flow Analyzer
====================

Lightweight, in-memory packet rate tracking for the Zigbee + Matter network.

Records every packet that enters `ZigbeeDebugger.capture_packet` (and every
TX command sent through `device.send_command`) regardless of whether full
debug capture is enabled, and exposes:

  - Global packets-per-second over 1s / 10s / 60s windows
  - Per-second history for a 60s sparkline
  - Peak 1s rate over the last hour (single highest second)
  - Top-N peak history over the last hour, with timestamps + dominant device
  - Statistical summary: mean, std dev, P50, P95, P99 over the last hour
  - Burst counter: number of seconds exceeding mean + 2σ
  - Per-device chattiness ranking
  - Per-cluster aggregate breakdown
  - Per-device EWMA-baseline anomaly detection

Pure stdlib. No DB writes. No locks (single-thread asyncio).

Cost per `record()`: a dict lookup + 3 deque appends + a few ints. O(1).
Pruning is amortised on read; a hard GC drops devices that go silent.

Statistical methods are computed lazily on read and cached for ~1 second
to keep the snapshot path cheap when the WS pushes every 2s.
"""

from __future__ import annotations
import math
import time
from collections import deque, defaultdict
from typing import Dict, List, Optional, Any, Tuple


# --- Tuning knobs ----------------------------------------------------------
WINDOW_60S = 60.0
WINDOW_10S = 10.0
WINDOW_1S  = 1.0

# Peak / stats history: keep one sample per second for an hour.
# 3600 ints + 3600 dict slots ≈ 100 KB — negligible.
PEAK_HISTORY_SECONDS = 3600

# How many top-peak seconds to surface in the snapshot.
TOP_PEAKS_N = 5

# Burst threshold: a "burst second" is one where the rate exceeded
# mean + (BURST_SIGMA × stddev) over the analysis window.
BURST_SIGMA = 2.0

# Stats are recomputed at most this often. Keeps the snapshot path cheap
# when the WS pushes every 2s.
STATS_CACHE_TTL = 1.0

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
    cluster), plus per-second peak history with statistical analysis.
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

        # Per-second packet count. Sparse: only seconds with packets exist.
        # Trimmed to the last PEAK_HISTORY_SECONDS on read.
        self._per_second: Dict[int, int] = {}
        # Per-second dominant device (the IEEE that contributed the most
        # packets that second). Used to attribute peak seconds.
        self._per_second_dom: Dict[int, Tuple[str, int]] = {}
        # Per-(second,device) accumulator used to track the dominant device
        # for the current second. Cleared when the second rolls over.
        self._cur_sec: int = 0
        self._cur_sec_devs: Dict[str, int] = {}
        # First second we ever observed traffic — used to bound stats
        # window so a fresh process isn't penalised by 0-padding.
        self._first_observed_sec: Optional[int] = None

        # Cached stats — recomputed at most every STATS_CACHE_TTL seconds.
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_ts: float = 0.0

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

        # Per-second bucket — used for peak / stats tracking.
        sec = int(timestamp)
        self._per_second[sec] = self._per_second.get(sec, 0) + 1
        if self._first_observed_sec is None:
            self._first_observed_sec = sec

        # Track which device dominated this second. When the second rolls
        # over, lock in the leader.
        if sec != self._cur_sec:
            if self._cur_sec_devs:
                # Finalise the previous second.
                top_ieee = max(self._cur_sec_devs, key=self._cur_sec_devs.get)
                top_count = self._cur_sec_devs[top_ieee]
                self._per_second_dom[self._cur_sec] = (top_ieee, top_count)
            self._cur_sec = sec
            self._cur_sec_devs = {}
        if ieee:
            self._cur_sec_devs[ieee] = self._cur_sec_devs.get(ieee, 0) + 1

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

        # Trim the per-second peak ring to the last hour.
        peak_cutoff = int(now) - PEAK_HISTORY_SECONDS
        if self._per_second:
            stale = [s for s in self._per_second if s < peak_cutoff]
            for s in stale:
                self._per_second.pop(s, None)
                self._per_second_dom.pop(s, None)

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
    # Statistical analysis (hourly per-second history)
    # ------------------------------------------------------------------
    def _compute_stats(self, now: float) -> Dict[str, Any]:
        """
        Compute mean, stddev, P50/P95/P99, peak history, burst count over
        the last PEAK_HISTORY_SECONDS. Includes zero-count seconds in the
        sample so the mean isn't biased high.

        Cached for STATS_CACHE_TTL seconds — typical call cost is O(N) on
        cache miss (N=3600), well under 1 ms on a Rock 5B.
        """
        if (self._stats_cache is not None
                and (now - self._stats_cache_ts) < STATS_CACHE_TTL):
            return self._stats_cache

        if not self._per_second or self._first_observed_sec is None:
            empty = self._empty_stats()
            self._stats_cache = empty
            self._stats_cache_ts = now
            return empty

        current_sec = int(now)
        # Sample window: the last PEAK_HISTORY_SECONDS, EXCLUDING the
        # current in-flight second (it's a partial sample and would skew low).
        end_sec = current_sec - 1
        start_sec = end_sec - PEAK_HISTORY_SECONDS + 1

        # Constrain to seconds we've actually been running. Without this,
        # a fresh process gets a 0-padded mean across 3600 samples, hiding
        # any real burst it just witnessed.
        start_sec = max(start_sec, self._first_observed_sec)
        n_seconds = max(1, end_sec - start_sec + 1)

        # Build the dense sample (zero-fill for quiet seconds).
        samples: List[int] = []
        peaks: List[Tuple[int, int]] = []   # (sec, count) for non-zero seconds
        for sec in range(start_sec, end_sec + 1):
            v = self._per_second.get(sec, 0)
            samples.append(v)
            if v > 0:
                peaks.append((sec, v))

        # Mean, variance, stddev (population, not sample — we have the full
        # observation window, not a sample of a larger population).
        total = sum(samples)
        mean = total / n_seconds if n_seconds else 0.0
        if n_seconds > 1:
            var = sum((x - mean) ** 2 for x in samples) / n_seconds
            stddev = math.sqrt(var)
        else:
            stddev = 0.0

        # Percentiles — only meaningful with non-trivial sample size.
        sorted_samples = sorted(samples)
        p50 = self._percentile(sorted_samples, 50)
        p95 = self._percentile(sorted_samples, 95)
        p99 = self._percentile(sorted_samples, 99)
        max_v = sorted_samples[-1] if sorted_samples else 0

        # Coefficient of variation — unitless burstiness indicator.
        # CV < 0.5 → steady; 0.5–1.5 → moderately bursty; >1.5 → very bursty.
        cv = (stddev / mean) if mean > 0 else 0.0

        # Burst count: seconds where count > mean + (BURST_SIGMA × stddev).
        # Skip when stddev is ~0 (steady traffic, no meaningful threshold).
        if stddev > 0.5:
            burst_threshold: float = mean + (BURST_SIGMA * stddev)
        else:
            burst_threshold = float("inf")
        burst_count = sum(1 for x in samples if x > burst_threshold)

        # Top-N peaks with attribution.
        peaks.sort(key=lambda t: t[1], reverse=True)
        top_peaks: List[Dict[str, Any]] = []
        for sec, count in peaks[:TOP_PEAKS_N]:
            dom = self._per_second_dom.get(sec)
            top_peaks.append({
                "ts": sec,
                "rate": count,
                "age_sec": current_sec - sec,
                "dominant_ieee": dom[0] if dom else None,
                "dominant_count": dom[1] if dom else 0,
                # % of the second's packets attributable to the top device
                "dominant_pct": round(100.0 * dom[1] / count, 1)
                if (dom and count) else None,
            })

        out = {
            "window_seconds":  n_seconds,
            "samples":         n_seconds,
            "total":           total,
            "mean":            round(mean, 3),
            "stddev":          round(stddev, 3),
            "cv":              round(cv, 3),
            "p50":             p50,
            "p95":             p95,
            "p99":             p99,
            "max":             max_v,
            "burst_threshold": (round(burst_threshold, 2)
                                if burst_threshold != float("inf") else None),
            "burst_count":     burst_count,
            "burst_pct":       (round(100.0 * burst_count / n_seconds, 2)
                                if n_seconds else 0.0),
            "top_peaks":       top_peaks,
        }
        self._stats_cache = out
        self._stats_cache_ts = now
        return out

    @staticmethod
    def _empty_stats() -> Dict[str, Any]:
        return {
            "window_seconds":  0,
            "samples":         0,
            "total":           0,
            "mean":            0.0,
            "stddev":          0.0,
            "cv":              0.0,
            "p50":             0,
            "p95":             0,
            "p99":             0,
            "max":             0,
            "burst_threshold": None,
            "burst_count":     0,
            "burst_pct":       0.0,
            "top_peaks":       [],
        }

    @staticmethod
    def _percentile(sorted_samples: List[int], p: float) -> int:
        """Nearest-rank percentile. Sorted input required."""
        if not sorted_samples:
            return 0
        # Nearest-rank: ceil(p/100 × N) → 1-indexed
        k = max(1, math.ceil(p / 100.0 * len(sorted_samples)))
        return sorted_samples[k - 1]

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------
    def get_global_rate(self) -> Dict[str, Any]:
        now = time.time()
        self._prune_all(now)
        c1 = self._count_in_window(self._global, now, WINDOW_1S)
        c10 = self._count_in_window(self._global, now, WINDOW_10S)
        c60 = self._count_in_window(self._global, now, WINDOW_60S)

        # Peak 1s rate over the last hour.
        # Exclude the current (in-progress) second so an in-flight burst
        # doesn't masquerade as a confirmed peak.
        current_sec = int(now)
        peak_1s = 0
        peak_at: Optional[int] = None
        for sec, count in self._per_second.items():
            if sec == current_sec:
                continue
            if count > peak_1s:
                peak_1s = count
                peak_at = sec

        return {
            "rate_1s": c1 / WINDOW_1S,
            "rate_10s": c10 / WINDOW_10S,
            "rate_60s": c60 / WINDOW_60S,
            "peak_1s_last_hour": peak_1s,
            "peak_1s_at": peak_at,
            "peak_1s_age_sec": (current_sec - peak_at) if peak_at else None,
            "total": self._total,
            "rx": self._dir_counts.get("RX", 0),
            "tx": self._dir_counts.get("TX", 0),
            "tracked_devices": len(self._by_device),
            "tracked_clusters": len(self._by_cluster),
        }

    def get_stats(self) -> Dict[str, Any]:
        """
        Statistical summary over the last hour of per-second samples.
        Returns mean / stddev / coefficient-of-variation / percentiles /
        peak history / burst count.
        """
        now = time.time()
        self._prune_all(now)
        return self._compute_stats(now)

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
            "stats":     self.get_stats(),
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
        self._per_second.clear()
        self._per_second_dom.clear()
        self._cur_sec = 0
        self._cur_sec_devs.clear()
        self._first_observed_sec = None
        self._stats_cache = None
        self._stats_cache_ts = 0.0


# --- Singleton (mirrors the debugger pattern) -----------------------------
_analyzer: Optional[PacketFlowAnalyzer] = None


def get_flow_analyzer() -> PacketFlowAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = PacketFlowAnalyzer()
    return _analyzer