"""
Heating anomaly watcher.

Runs on a timer. For each room with a known baseline tau, pulls recent
telemetry (last ~3 hours), looks for fast-cool / slow-heat anomalies, and
stores active ones in memory for the dashboard + tips to consume.

Resolved anomalies (where the condition has ended) are moved into a short
history buffer so the UI can show "was" cards briefly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional
# Used to build the heating-off gate passed into compute_profile, so cool-down
# windows that overlapped active heating are excluded from the baseline fit.
try:
    from modules.telemetry_db import query_room_heating_state
except Exception:
    query_room_heating_state = None

logger = logging.getLogger("modules.heating_anomaly_watcher")

SCAN_INTERVAL_SEC = 300        # every 5 minutes
HISTORY_KEEP_SEC = 6 * 3600    # keep resolved anomalies for 6h on dashboard


# ── Heating-state gate builder ────────────────────────────────────────
# Ticks are ~1/min; telemetry samples can be at different cadences. We
# build a piecewise lookup that returns True for any timestamp falling
# inside or immediately after a "heating active" tick, with a short
# staleness grace so gaps in the tick log don't silently flip to "off".

GATE_STALENESS_SEC = 180   # 3× expected tick interval — if the last tick
# is older than this, we don't trust the gate
# and conservatively return True (i.e. assume
# heating was on, rejecting the window).


def _build_heating_state_getter(tick_rows: List[Dict[str, Any]]):
    """
    Return a callable `getter(ts_seconds) -> bool` where True means
    "heating was likely active at ts, so this sample is not a clean
    natural cool-down and should not feed the baseline fit."

    tick_rows come from telemetry_db.query_room_heating_state and are
    already sorted ascending by ts. Each row carries a boolean
    'heating_active'.

    If we have no tick data at all (e.g. the new schema was just added
    and nothing has been written yet), the getter returns False — i.e.
    no gate is applied. This preserves today's behaviour for a fresh
    install, so we never regress the data flow.
    """
    if not tick_rows:
        return lambda _ts: False

    # Pre-extract as two parallel lists for bisect.
    import bisect
    import datetime as _dt

    tick_ts: List[float] = []
    tick_active: List[bool] = []
    for r in tick_rows:
        t = r.get("ts")
        if isinstance(t, _dt.datetime):
            tick_ts.append(t.timestamp())
        elif isinstance(t, (int, float)):
            tick_ts.append(float(t))
        else:
            continue
        tick_active.append(bool(r.get("heating_active")))

    if not tick_ts:
        return lambda _ts: False

    def _getter(ts_seconds: float) -> bool:
        # Find the last tick at or before ts_seconds.
        idx = bisect.bisect_right(tick_ts, ts_seconds) - 1
        if idx < 0:
            # ts is before our earliest tick → we don't know.
            # Conservative: assume on (exclude from baseline).
            return True
        last_ts = tick_ts[idx]
        # If the tick is too stale, don't trust it.
        if ts_seconds - last_ts > GATE_STALENESS_SEC:
            return True
        return tick_active[idx]

    return _getter

class HeatingAnomalyWatcher:
    def __init__(
            self,
            config_getter: Callable[[], Dict[str, Any]],
            advisor_getter: Callable[[], Any],
            telemetry_query: Callable[[str, str, int], List[Dict[str, Any]]],
    ):
        self.config_getter = config_getter
        self.advisor_getter = advisor_getter
        self.telemetry_query = telemetry_query

        # Keyed by (circuit_id, room_id, kind) so the same room can't have
        # two fast-cool anomalies stacking.
        self._active: Dict[tuple, Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._last_scan_ts: float = 0.0

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heating anomaly watcher started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Anomaly scan failed: {e}", exc_info=True)
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def scan_once(self) -> int:
        """Perform one scan across all configured rooms. Returns new anomaly count."""
        from modules.thermal_profile import (
            detect_fast_cooling, detect_slow_heating,
            compute_profile,
        )

        cfg = self.config_getter() or {}
        heating = cfg.get("heating") or {}
        insulation = (heating.get("property") or {}).get("insulation", "partial")
        adv = self.advisor_getter()

        outdoor = None
        if adv and getattr(adv, "weather", None):
            try:
                outdoor = adv.weather.get_outdoor_temperature()
            except Exception:
                pass
        if outdoor is None:
            outdoor = 10.0

        new_count = 0
        seen_keys = set()

        for circuit in (heating.get("circuits") or []):
            cid = str(circuit.get("id"))
            for room in (circuit.get("rooms") or []):
                rid = str(room.get("id"))
                dimensions = room.get("dimensions")

                # Pick sensor
                sensor_ieee = room.get("temperature_sensor_ieee")
                if not sensor_ieee:
                    trvs = room.get("trvs") or []
                    if trvs and isinstance(trvs[0], dict):
                        sensor_ieee = trvs[0].get("ieee")
                if not sensor_ieee:
                    continue

                # Baseline tau from 14-day profile
                long_series = []
                try:
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = self.telemetry_query(sensor_ieee, attr, 14 * 24) or []
                        if rows:
                            long_series = rows
                            break
                except Exception as e:
                    logger.debug(f"long-series fetch failed for {sensor_ieee}: {e}")
                    continue

                outdoor_getter = lambda _ts, _v=outdoor: _v

                # Build the heating-off gate from persisted controller ticks.
                # If the schema/function isn't available (fresh install, old
                # build), or there are simply no ticks yet, the getter is
                # None and compute_profile falls back to today's behaviour.
                heating_state_getter = None
                if query_room_heating_state is not None:
                    try:
                        tick_rows = query_room_heating_state(
                            circuit_id=cid, room_id=rid, hours=14 * 24,
                        )
                        if tick_rows:
                            heating_state_getter = _build_heating_state_getter(tick_rows)
                    except Exception as e:
                        logger.debug(
                            f"heating-state gate unavailable for {cid}/{rid}: {e}"
                        )

                profile = compute_profile(
                    room_id=rid,
                    dimensions=dimensions,
                    insulation=insulation,
                    temperature_series=long_series,
                    outdoor_temp_getter=outdoor_getter,
                    heating_state_getter=heating_state_getter,
                )
                baseline_tau = profile.tau_seconds
                if baseline_tau is None:
                    continue  # no baseline yet, skip this room

                # Recent (last 3h) — used for both cool and heat detection
                recent = []
                try:
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = self.telemetry_query(sensor_ieee, attr, 3) or []
                        if rows:
                            recent = rows
                            break
                except Exception as e:
                    logger.debug(f"recent-series fetch failed for {sensor_ieee}: {e}")
                    continue
                if len(recent) < 4:
                    continue

                # Check both detectors
                fast = detect_fast_cooling(
                    room_id=rid,
                    recent_temperature_series=recent,
                    outdoor_temp_c=outdoor,
                    baseline_tau_seconds=baseline_tau,
                )
                slow = detect_slow_heating(
                    room_id=rid,
                    recent_temperature_series=recent,
                    expected_tau_seconds=baseline_tau,
                )

                for anomaly in (fast, slow):
                    if anomaly is None:
                        continue
                    key = (cid, rid, anomaly.kind)
                    seen_keys.add(key)

                    existing = self._active.get(key)
                    if existing and \
                            existing.get("window_end_ts") == anomaly.window_end_ts:
                        continue  # already reported this exact window

                    # New or refreshed anomaly
                    record = {
                        "circuit_id": cid,
                        "circuit_name": circuit.get("name"),
                        "room_id": rid,
                        "room_name": room.get("name"),
                        **anomaly.to_dict(),
                    }
                    self._active[key] = record
                    new_count += 1
                    logger.warning(
                        f"Anomaly [{anomaly.severity}] {anomaly.kind} in "
                        f"{circuit.get('name')}/{room.get('name')}: "
                        f"{anomaly.message}"
                    )

        # Resolve: anything in _active that we didn't re-detect this pass
        now = time.time()
        resolved = []
        for key in list(self._active.keys()):
            if key not in seen_keys:
                rec = self._active.pop(key)
                rec["resolved_at"] = now
                resolved.append(rec)
                self._history.insert(0, rec)

        # Trim history
        cutoff = now - HISTORY_KEEP_SEC
        self._history = [h for h in self._history
                         if h.get("resolved_at", now) >= cutoff][:50]

        self._last_scan_ts = now
        return new_count

    def get_snapshot(self) -> Dict[str, Any]:
        return {
            "last_scan_ts": self._last_scan_ts,
            "last_scan_age_seconds": (time.time() - self._last_scan_ts) if self._last_scan_ts else None,
            "active": list(self._active.values()),
            "recently_resolved": self._history[:10],
        }