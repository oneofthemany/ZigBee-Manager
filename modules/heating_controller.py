"""
Heating Controller — Active control of receivers and TRVs.
==========================================================
Sits alongside HeatingAdvisor (which is read-only/analytical).
The Controller actually sends commands to make heating happen.

Model:
    Circuit (a receiver/zone valve calling for boiler heat)
      └── Room (a heated space with target temp)
            ├── Room sensor (optional — external thermostat/temp sensor)
            └── TRV(s) (regulate flow into that room's radiators)

Per-tick decision flow:
    1. Snapshot device states
    2. For each room:
         a. Pick the room temperature source:
              - temperature_sensor_ieee if present & online  → authoritative
              - otherwise, mean of TRV local_temperature
         b. Classify: COLD / ONTARGET / HOT (with hysteresis)
    3. Decide each circuit: CALLING (any room cold) / IDLE (all rooms ok)
    4. Decide each TRV's setpoint:
         - room COLD     → setpoint = target         (open via own thermostat)
         - room HOT      → setpoint = current - 1.0  (force close, prevent stealing)
         - room ONTARGET → setpoint = target         (idle)
    5. Apply receiver state changes (only if differ from last command)
    6. Apply TRV setpoint changes (only if differ from last command + larger than 0.5°C)
    7. (Background) Push external temp to Aqara TRVs if external_temp_mode=='push'

External sensor modes (per-room, config.external_temp_mode):
    - "off"      : TRV local temps decide everything (legacy behaviour)
    - "advisory" : controller uses external sensor for its own classification,
                   but TRVs continue using their own internal sensor. This is
                   the safe default when an external sensor is configured —
                   it immediately fixes "TRV reads hot pipe, not air".
    - "push"     : advisory + controller writes the external temperature into
                   each Aqara TRV's manufacturer cluster (0xFCC0, attr 0x0280)
                   and flips sensor_type to external. Requires Aqara TRV.

Per-TRV config (applied on start and via API):
    - window_detection : bool  → Aqara 0xFCC0 attr 0x0273
    - child_lock       : bool  → Aqara 0xFCC0 attr 0x0277
    - valve_detection  : bool  → Aqara 0xFCC0 attr 0x0274
  (motor_calibration is one-shot via API, not persisted as "always on")

Config (config.yaml under heating):
  heating:
    controller:
      enabled: true
      dry_run: false
    circuits:
      - id: downstairs
        name: "Downstairs"
        receiver_ieee: "00:15:8d:00:00:aa:bb:cc"
        receiver_command: thermostat      # 'thermostat' or 'switch'
        receiver_endpoint: 1
        rooms:
          - id: living
            name: "Living"
            target_temp: 20.5
            night_setback: 17.0
            min_temp: 16.0
            temperature_sensor_ieee: "00:1e:5e:09:02:a3:e4:c1"
            external_temp_mode: advisory          # off | advisory | push
            external_temp_push_interval_sec: 300
            # Legacy: trv_ieees: ["54:ef:44:..."]     (still supported)
            trvs:
              - ieee: "54:ef:44:10:00:67:3e:a6"
                window_detection: true
                child_lock: false
                valve_detection: true
            schedule:
              - days: [mon,tue,wed,thu,fri]
                start: "07:00"
                end:   "22:00"
                temp:  20.5
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.thermal_profile import (
    correct_sensor_reading,
    stratification_offset_c,
    STRATIFICATION_MAX_PLAUSIBLE_HEIGHT_M,
)

# Telemetry persistence for tick decisions. Lazy import inside _tick would
# also work; top-level is fine because telemetry_db has no heavy deps.
try:
    from modules.telemetry_db import write_heating_tick as _write_heating_tick
except Exception:
    _write_heating_tick = None

# Per-attribute freshness query — used by the health check to detect a
# device that's still in the snapshot but hasn't reported its temperature
# in a while (the "frozen attribute" failure mode that last_seen can't
# distinguish from a healthy device that's just reporting battery).
try:
    from modules.telemetry_db import query_device_state_history as _query_state_history
except Exception:
    _query_state_history = None

logger = logging.getLogger("modules.heating_controller")

# Hysteresis bands (°C) — prevents oscillation
COLD_BAND = 0.5     # room is COLD if temp < target - 0.5
HOT_BAND = 0.3      # room is HOT  if temp > target + 0.3

# Per-room data freshness — if the configured external sensor hasn't reported
# its temperature attribute in this many seconds, the room is flagged
# critical and the user is alerted. Per-room override via room config
# 'freshness_threshold_minutes'. 15 min default is forgiving enough for
# Hue motion sensors (which only report on movement + periodic heartbeat)
# but tight enough to catch an unpaired/dead sensor within one tick.
DEFAULT_FRESHNESS_THRESHOLD_SEC = 15 * 60

# Force-close offset — when shutting a TRV, set it to (current - this) so the
# TRV's own thermostat keeps the valve closed
FORCE_CLOSE_OFFSET = 1.0

# Minimum setpoint change worth sending (avoid hammering battery TRVs)
MIN_SETPOINT_DELTA = 0.5

# How often the controller loop runs
TICK_INTERVAL_SEC = 60

# Don't repeat the same setpoint command more often than this
COMMAND_COOLDOWN_SEC = 300

# Default external-temp push cadence when mode='push' and room doesn't override
DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC = 300

# Min delta before we re-push external temp to a TRV (°C). Saves battery airtime.
EXT_TEMP_PUSH_MIN_DELTA = 0.3

# ── Weather-based heat suppression ─────────────────────────────────
# Hysteresis prevents flap when outdoor temp hovers near a single threshold.
WX_SUPPRESS_OFF_C = 16.0       # engage when current outdoor ≥ this
WX_SUPPRESS_ON_C = 14.0        # release when current outdoor <  this
WX_FORECAST_LOOKAHEAD_H = 6    # hours of forecast to consider
WX_FORECAST_MIN_C = 12.0       # if forecast min within window < this → never suppress
# ── Adaptive overshoot compensation ────────────────────────────────
OVERSHOOT_LEARN_ALPHA = 0.3            # EWMA weight on each new observation
OVERSHOOT_PEAK_DROP_C = 0.1            # peak considered set once temp drops by this
OVERSHOOT_PEAK_TIMEOUT_SEC = 1200      # 20 min — accept whatever peak we have
OVERSHOOT_MAX_OFFSET_C = 1.5           # safety cap
# ── Window/door contact integration ────────────────────────────────
CONTACT_DEBOUNCE_OPEN_SEC = 30          # ignore brief openings
CONTACT_REQUIRE_TEMP_DROP_C = 0.5       # only act if room actually cooling
CONTACT_REQUIRE_DROP_WINDOW_SEC = 600   # within 10 min of opening
CONTACT_MAX_CLOSE_SEC = 3600            # safety release
CONTACT_CLOSE_DEBOUNCE_SEC = 5          # avoid flutter on door close
# ── Predictive pre-heat (uses thermal_profile) ─────────────────────
PROFILE_CACHE_TTL_SEC = 1800        # refresh per-room W/K + tau every 30 min
PREHEAT_SAFETY_MARGIN = 1.15        # start 15% earlier than the model says
PREHEAT_LOOKAHEAD_MAX_MIN = 240     # don't preheat more than 4h ahead
PREHEAT_TELEMETRY_HOURS = 72        # window of temperature history to fit

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── Helpers ────────────────────────────────────────────────────────
def _as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(v, default: Optional[bool] = None) -> Optional[bool]:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled", "lock"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled", "unlock"):
            return False
    return default


def _parse_hhmm(s: str) -> Optional[int]:
    try:
        hh, mm = str(s).split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _device_state(dev: Any) -> Dict[str, Any]:
    """Extract state dict from either a dict-style or object-style device."""
    if isinstance(dev, dict):
        return dev.get("state") or {}
    return getattr(dev, "state", None) or {}


def _device_friendly_name(dev: Any, fallback: str) -> str:
    if isinstance(dev, dict):
        return dev.get("friendly_name") or dev.get("name") or fallback
    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
    if not name:
        service = getattr(dev, "service", None)
        if service is not None:
            fn_map = getattr(service, "friendly_names", None) or {}
            name = fn_map.get(fallback)
    return name or fallback


def _pick_temperature(state: Dict[str, Any]) -> Optional[float]:
    """
    Pull a temperature reading from a device's state, preferring the most
    'room-like' key. Works for thermostats, TRVs and bare temperature sensors.
    """
    for key in ("local_temperature", "current_temperature", "temperature"):
        v = state.get(key)
        f = _as_float(v)
        if f is not None and f != 0:   # filter out the init-zero we sometimes see
            return f
    return None

def _primary_sensor_height_m(room: Dict[str, Any]) -> Optional[float]:
    """
    Resolve the mounting height of the room's primary temperature sensor,
    in metres. Used by the stratification-correction logic.

    Lookup precedence (to handle every schema state cleanly):

      1. ``room.temperature_sensors`` — the canonical plural list (step 2).
         Walk it for the entry marked ``primary: true``; if none flagged,
         use the first entry. This is the only path that ever returns a
         non-None value after a manual-mode round-trip through _clean_room.

      2. No fallback to anything else — manual config without the plural
         list (legacy state) simply has no height info, so correction is
         a no-op. The user can add height by editing the room in the
         multi-sensor UI (step 3b).

    Returns None when:
      - the room has no sensor list (legacy single-only state),
      - the primary sensor has no ``height_m``,
      - the value is implausible (outside 0–5 m).
    """
    sensors = room.get("temperature_sensors")
    if not isinstance(sensors, list) or not sensors:
        return None
    primary = next((s for s in sensors if isinstance(s, dict) and s.get("primary")), None)
    if primary is None:
        primary = next((s for s in sensors if isinstance(s, dict)), None)
    if not primary:
        return None
    h = primary.get("height_m")
    try:
        h = float(h) if h is not None else None
    except (TypeError, ValueError):
        return None
    if h is None or h < 0.0 or h > STRATIFICATION_MAX_PLAUSIBLE_HEIGHT_M:
        return None
    return h

# ── Per-attribute freshness ────────────────────────────────────────
# Per-tick cache of "when did this IEEE last report a temperature?" so a
# tick that evaluates 4 rooms doesn't fire 4 separate DuckDB queries when
# rooms share sensors, and so subsequent ticks within a short window can
# skip the query if we just checked.
_freshness_cache: Dict[str, Tuple[float, Optional[float]]] = {}
_FRESHNESS_CACHE_TTL_SEC = 30.0  # well under tick interval; just a cheap dedupe


def _last_temperature_ts(ieee: str) -> Optional[float]:
    """
    Return the unix timestamp of the most recent temperature report for this
    IEEE in DuckDB, or None if no row exists or the query layer isn't loaded.

    Looks at the three temperature attribute names devices commonly use,
    matching the keys _pick_temperature considers. Returns the *most recent*
    across all three so e.g. a thermostat reporting both `local_temperature`
    and `temperature` is treated as fresh as long as either is recent.
    """
    if _query_state_history is None:
        return None
    cached = _freshness_cache.get(ieee)
    now = time.time()
    if cached and (now - cached[0]) < _FRESHNESS_CACHE_TTL_SEC:
        return cached[1]

    most_recent: Optional[float] = None
    # Look back well beyond the threshold so a sensor reporting every 20 min
    # isn't falsely flagged just because nothing landed in the last 15.
    LOOKBACK_HOURS = 6
    for attr in ("local_temperature", "current_temperature", "temperature"):
        try:
            rows = _query_state_history(ieee, attr, LOOKBACK_HOURS) or []
        except Exception as e:
            logger.debug(f"freshness query failed for {ieee} attr={attr}: {e}")
            continue
        if not rows:
            continue
        # query_device_state_history orders ASC, so the last row is newest.
        last = rows[-1]
        ts_raw = last.get("ts")
        if ts_raw is None:
            continue
        # ts may be a datetime or float depending on backend
        try:
            ts_val = ts_raw.timestamp() if hasattr(ts_raw, "timestamp") else float(ts_raw)
        except (TypeError, ValueError, AttributeError):
            continue
        if most_recent is None or ts_val > most_recent:
            most_recent = ts_val

    _freshness_cache[ieee] = (now, most_recent)
    return most_recent


def _check_room_health(room: dict, devices: Dict[str, Any],
                       decision: "RoomDecision",
                       sensor_present_in_devices: bool,
                       sensor_raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decide whether the data feeding this room's classification is trustworthy.

    Severity:
        ok       — every configured device is in the snapshot and reporting fresh
        critical — any failure: device missing, no temp keys, or stale per DuckDB

    Per requirements all failures are critical (no warning tier). Returns
    {"level": str, "reasons": [str], "stale_devices": [{ieee, age_sec}]}
    so the frontend can both badge and explain.
    """
    reasons: List[str] = []
    stale: List[Dict[str, Any]] = []
    threshold_min = _as_float(room.get("freshness_threshold_minutes"))
    threshold_sec = float(threshold_min * 60) if threshold_min and threshold_min > 0 \
        else float(DEFAULT_FRESHNESS_THRESHOLD_SEC)
    now = time.time()

    sensor_ieee = room.get("temperature_sensor_ieee")
    ext_mode = room.get("external_temp_mode", "off")

    # ── External sensor (if configured) ─────────────────────────────
    if sensor_ieee and ext_mode != "off":
        if not sensor_present_in_devices:
            reasons.append(
                f"Sensor {sensor_ieee} not on the network "
                f"— check pairing"
            )
        elif not sensor_raw:
            reasons.append(
                f"Sensor {sensor_ieee} reports no temperature attributes"
            )
        else:
            last_ts = _last_temperature_ts(sensor_ieee)
            if last_ts is None:
                reasons.append(
                    f"Sensor {sensor_ieee} has never recorded a temperature"
                )
            else:
                age = now - last_ts
                if age > threshold_sec:
                    age_min = int(age / 60)
                    reasons.append(
                        f"Sensor {sensor_ieee} last reported "
                        f"{age_min} min ago (threshold {int(threshold_sec/60)} min)"
                    )
                    stale.append({"ieee": sensor_ieee, "age_sec": int(age),
                                  "kind": "sensor"})

    # The external sensor is the authoritative reading whenever
    # decision.temp_source == "external". In that case the TRV's own
    # internal local_temperature is not driving any control decision and
    # its DuckDB freshness is not a health signal for this room.
    using_external = (decision.temp_source == "external")

    # ── TRVs ────────────────────────────────────────────────────────
    for t in (room.get("trvs") or []):
        ieee = t.get("ieee") if isinstance(t, dict) else None
        if not ieee:
            continue
        dev = devices.get(ieee)
        if dev is None:
            reasons.append(f"TRV {ieee} not on the network")
            continue
        # State already extracted by caller into decision.trvs — find it
        trv_dec = next((x for x in decision.trvs if x.get("ieee") == ieee), None)
        if not trv_dec or not trv_dec.get("online"):
            reasons.append(f"TRV {ieee} offline")
            continue
        if trv_dec.get("current_temp") is None:
            # No reading at all — distinct from "stale". This means the
            # in-memory device state has no temperature key, which is a
            # genuine failure even when an external sensor is present
            # (the TRV is silent altogether, not just slow on local_temperature).
            reasons.append(f"TRV {ieee} reports no temperature")
            continue

        # Freshness check — skipped when the external sensor is the
        # authoritative source. See the block comment above.
        if not using_external:
            last_ts = _last_temperature_ts(ieee)
            if last_ts is None:
                # No DuckDB history yet — don't flag as critical on its own;
                # the in-memory state shows a value, and a fresh install hasn't
                # had time to accumulate rows. Silent pass.
                pass
            else:
                age = now - last_ts
                if age > threshold_sec:
                    age_min = int(age / 60)
                    reasons.append(
                        f"TRV {ieee} last reported temperature {age_min} min ago"
                    )
                    stale.append({"ieee": ieee, "age_sec": int(age), "kind": "trv"})

        if trv_dec.get("valve_alarm"):
            reasons.append(f"TRV {trv_dec.get('name') or ieee}: valve alarm (stuck/seized)")

    # ── No data source at all ───────────────────────────────────────
    if decision.temp_source == "none":
        reasons.append("No temperature data available — room cannot be controlled")

    return {
        "level": "critical" if reasons else "ok",
        "reasons": reasons,
        "stale_devices": stale,
        "threshold_minutes": int(threshold_sec / 60),
    }


# ── Room state ─────────────────────────────────────────────────────
class RoomDecision:
    """Per-tick analysis of a single room."""

    __slots__ = (
        "room_id", "name", "target_temp", "current_temp", "temp_source",
        "status", "calling_for_heat", "trvs", "sensor_ieee", "sensor_online",
        "health", "preheat", "overshoot", "contact",
        "current_temp_raw", "sensor_height_m", "stratification_offset_c",
    )

    def __init__(self, room_id: str, name: str):
        self.room_id = room_id
        self.name = name
        self.target_temp: Optional[float] = None
        self.current_temp: Optional[float] = None
        self.current_temp_raw: Optional[float] = None       # what the sensor actually said
        self.sensor_height_m: Optional[float] = None        # mount height used for correction
        self.stratification_offset_c: float = 0.0           # what we added to raw to get current_temp
        self.temp_source: str = "none"
        self.sensor_ieee: Optional[str] = None
        self.sensor_online: Optional[bool] = None
        self.status: str = "unknown"
        self.calling_for_heat: bool = False
        self.trvs: List[Dict] = []
        self.health: Dict[str, Any] = {"level": "ok", "reasons": [],
                                       "stale_devices": [], "threshold_minutes": 15}
        self.preheat: Optional[Dict[str, Any]] = None
        self.overshoot: Optional[Dict[str, Any]] = None
        self.contact: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict:
        return {
            "room_id": self.room_id,
            "name": self.name,
            "target_temp": self.target_temp,
            "current_temp": self.current_temp,
            "temp_source": self.temp_source,
            "sensor_ieee": self.sensor_ieee,
            "sensor_online": self.sensor_online,
            "status": self.status,
            "calling_for_heat": self.calling_for_heat,
            "trvs": self.trvs,
            "health": self.health,
            "preheat": self.preheat,
            "overshoot": self.overshoot,
            "contact": self.contact,
            "current_temp_raw": self.current_temp_raw,
            "sensor_height_m": self.sensor_height_m,
            "stratification_offset_c": self.stratification_offset_c,
        }


# ── Controller ─────────────────────────────────────────────────────
class HeatingController:
    """Active control of multi-zone heating with TRV coordination."""

    def __init__(self, config: dict, device_getter: Callable,
                 command_sender: Callable, comfort_defaults: Optional[dict] = None,
                 weather_service: Any = None,
                 anomaly_getter: Optional[Callable] = None,
                 telemetry_query: Optional[Callable] = None):
        """
        Args:
            config: heating config block (will read 'circuits' and 'enabled')
            device_getter: callable returning {ieee: device}
            command_sender: async callable (ieee, command, value) -> coroutine
            comfort_defaults: optional defaults for night_setback, min_temp, etc.
            weather_service: WeatherService for outdoor-aware suppression
        """
        config = config or {}
        controller_cfg = config.get("controller") or {}
        self.enabled = bool(config.get("enabled", False)) and \
                       bool(controller_cfg.get("enabled", False))
        self.dry_run = bool(controller_cfg.get("dry_run", False))

        self._get_devices = device_getter
        self._throttled_send = command_sender
        self._weather = weather_service
        self._anomaly_getter = anomaly_getter
        self._telemetry_query = telemetry_query
        self._insulation = str(
            (config.get("property") or {}).get("insulation", "partial")
        )
        # room_id -> {w_per_k, tau_seconds, radiator_watts, expires_at}
        self._room_profile_cache: Dict[str, Dict[str, Any]] = {}

        defaults = comfort_defaults or {}
        self._default_target = _as_float(defaults.get("target_temp"), 21.0)
        self._default_setback = _as_float(defaults.get("night_setback"), 17.0)
        self._default_min = _as_float(defaults.get("min_temp"), 16.0)

        # Weather-suppression config (per-circuit override read in _clean_circuits)
        wx_cfg = controller_cfg.get("weather_suppression") or {}

        self._wx_enabled = bool(wx_cfg.get("enabled", False))
        self._wx_off_c = _as_float(wx_cfg.get("off_threshold_c"), WX_SUPPRESS_OFF_C)
        self._wx_on_c = _as_float(wx_cfg.get("on_threshold_c"), WX_SUPPRESS_ON_C)
        self._wx_lookahead_h = int(_as_float(
            wx_cfg.get("forecast_lookahead_hours"), WX_FORECAST_LOOKAHEAD_H
        ))
        self._wx_forecast_min_c = _as_float(
            wx_cfg.get("forecast_min_c"), WX_FORECAST_MIN_C
        )
        oh_cfg = controller_cfg.get("operating_hours") or {}
        self._oh_enabled = bool(oh_cfg.get("enabled", False))
        wk = oh_cfg.get("weekday") or {}
        we = oh_cfg.get("weekend") or {}
        self._oh_weekday_start = str(wk.get("day_start", "07:00"))
        self._oh_weekday_end = str(wk.get("day_end", "22:00"))
        self._oh_weekend_start = str(we.get("day_start", "08:00"))
        self._oh_weekend_end = str(we.get("day_end", "23:00"))
        self._oh_setback_offset = _as_float(
            oh_cfg.get("night_setback_offset_c"), -3.0
        )
        action = str(oh_cfg.get("out_of_hours_action", "setback")).lower()
        self._oh_action = action if action in ("setback", "off", "min_only") else "setback"

        self.circuits = self._clean_circuits(config.get("circuits") or [])

        # Last-command tracking for cooldown / change detection
        self._last_command: Dict[str, Tuple[str, Any, float]] = {}
        # Last external-temp push tracking:  trv_ieee -> (last_pushed_c, ts)
        self._last_ext_push: Dict[str, Tuple[float, float]] = {}
        # Last decision snapshot (for dashboard/API)
        self._last_decision: Dict[str, Any] = {}
        self._last_decision_ts: float = 0
        # Applied-on-start flags so we don't spam configuration writes every tick
        self._trv_config_applied: set = set()
        # Sticky weather-suppression state per circuit_id
        self._weather_suppressed: Dict[str, bool] = {}
        # Per-room adaptive overshoot state.
        # Keyed by room_id:
        #   {learned_offset_c, phase, watch_started_ts, watch_target,
        #    peak_temp, samples, last_observed_overshoot}
        self._overshoot: Dict[str, Dict[str, Any]] = {}

        # Per-room window/door open state.
        # Keyed by room_id:
        #   {state, sensors_open: set, opened_ts, opened_temp,
        #    activated_ts, last_closed_ts, observed_drop_c}
        self._contact: Dict[str, Dict[str, Any]] = {}

        self._task: Optional[asyncio.Task] = None
        self._ext_push_task: Optional[asyncio.Task] = None

        self._radio_write_lock = asyncio.Lock()
        self._last_radio_write_ts = 0.0
        self._min_write_gap = 0.5
        self._config_lock = asyncio.Lock()

        if self.enabled:
            mode = "DRY-RUN" if self.dry_run else "LIVE"
            n_rooms = sum(len(c["rooms"]) for c in self.circuits)
            n_trvs = sum(len(r["trvs"]) for c in self.circuits for r in c["rooms"])
            n_sensors = sum(
                1 for c in self.circuits for r in c["rooms"]
                if r.get("temperature_sensor_ieee")
            )
            n_push = sum(
                1 for c in self.circuits for r in c["rooms"]
                if r.get("external_temp_mode") == "push"
            )
            logger.info(
                f"Heating Controller [{mode}]: "
                f"{len(self.circuits)} circuits, {n_rooms} rooms, {n_trvs} TRVs, "
                f"{n_sensors} room sensors, {n_push} rooms pushing ext-temp"
            )
        elif config.get("circuits"):
            logger.info("Heating Controller defined but not enabled (set heating.controller.enabled: true)")

    # ── Hot-reload ─────────────────────────────────────────────────
    async def apply_config(self, new_config: dict,
                           reason: str = "user-edit") -> Dict[str, Any]:
        """
        Re-derive enabled / dry_run / circuits from new_config in place,
        without restarting the controller. Operational state is preserved:
        - _last_command (idempotent gate stays warm)
        - _last_ext_push (TRV external-temp push throttle)
        - _trv_config_applied (don't re-push window/child-lock/valve config
          to TRVs that were already configured in this session)
        - _last_decision (panel keeps showing the last tick until a new one
          fires; replaced by the immediate post-reload tick below)

        Returns a dict describing what changed:
            {
                "applied": True,
                "diff": {
                    "rooms_added":   [{"circuit_id", "room_id", "name"}],
                    "rooms_removed": [{"circuit_id", "room_id", "name"}],
                    "rooms_changed": [{"circuit_id", "room_id", "name",
                                       "fields": ["target_temp", ...],
                                       "before": {...}, "after": {...}}],
                    "circuits_added":   [{"id", "name"}],
                    "circuits_removed": [{"id", "name"}],
                    "controller_changes": ["enabled", "dry_run"],
                },
                "restart_required_fields": [],   # reserved for future use
                "tick_triggered": bool,
            }

        The diff is the audit trail — both the user-facing toast and the
        log line are built from it, so they're guaranteed to match.
        """
        # Cache the floor plan alongside the cleaned circuits so the
        # thermal-profile path can reach it without another _load_config()
        self._floor_plan_cache = (controller_block or {}).get("_floor_plan_for_thermal")

        async with self._config_lock:
            new_config = new_config or {}

            # ── Diagnostic: what did we receive? ────────────────────
            # Log the raw incoming structure (one line per room) so we
            # can verify the route handler passed the right data.
            try:
                incoming_circuits = new_config.get("circuits") or []
                logger.debug(
                    f"[apply_config] entry: reason={reason} "
                    f"top_keys={list(new_config.keys())} "
                    f"enabled_top={new_config.get('enabled')} "
                    f"enabled_ctrl={(new_config.get('controller') or {}).get('enabled')} "
                    f"circuit_count={len(incoming_circuits)}"
                )
                for ci, c in enumerate(incoming_circuits):
                    if not isinstance(c, dict):
                        continue
                    for ri, r in enumerate(c.get("rooms") or []):
                        if not isinstance(r, dict):
                            continue
                        logger.debug(
                            f"[apply_config] incoming room "
                            f"circuit[{ci}].id={c.get('id')} "
                            f"room[{ri}].id={r.get('id')} "
                            f"name={r.get('name')!r} "
                            f"target_temp={r.get('target_temp')!r}"
                        )
            except Exception as diag_err:
                logger.warning(f"[apply_config] entry-diag failed: {diag_err}")

            controller_cfg = new_config.get("controller") or {}
            new_enabled = bool(new_config.get("enabled", False)) and \
                          bool(controller_cfg.get("enabled", False))
            new_dry_run = bool(controller_cfg.get("dry_run", False))
            new_wx_cfg = controller_cfg.get("weather_suppression") or {}
            new_wx_enabled = bool(new_wx_cfg.get("enabled", self._wx_enabled))
            new_wx_off_c = _as_float(new_wx_cfg.get("off_threshold_c"), self._wx_off_c)
            new_wx_on_c = _as_float(new_wx_cfg.get("on_threshold_c"), self._wx_on_c)
            new_wx_lookahead_h = int(_as_float(
                new_wx_cfg.get("forecast_lookahead_hours"), self._wx_lookahead_h
            ))
            new_wx_forecast_min_c = _as_float(
                new_wx_cfg.get("forecast_min_c"), self._wx_forecast_min_c
            )
            new_oh_cfg = controller_cfg.get("operating_hours") or {}
            new_oh_enabled = bool(new_oh_cfg.get("enabled", self._oh_enabled))
            new_wk = new_oh_cfg.get("weekday") or {}
            new_we = new_oh_cfg.get("weekend") or {}
            new_oh_weekday_start = str(new_wk.get("day_start", self._oh_weekday_start))
            new_oh_weekday_end = str(new_wk.get("day_end", self._oh_weekday_end))
            new_oh_weekend_start = str(new_we.get("day_start", self._oh_weekend_start))
            new_oh_weekend_end = str(new_we.get("day_end", self._oh_weekend_end))
            new_oh_setback_offset = _as_float(
                new_oh_cfg.get("night_setback_offset_c"), self._oh_setback_offset
            )
            new_action_raw = str(
                new_oh_cfg.get("out_of_hours_action", self._oh_action)
            ).lower()
            new_oh_action = new_action_raw if new_action_raw in (
                "setback", "off", "min_only"
            ) else self._oh_action
            new_circuits = self._clean_circuits(new_config.get("circuits") or [])

            # ── Diagnostic: what came out of _clean_circuits? ───────
            try:
                logger.debug(
                    f"[apply_config] post-parse: new_enabled={new_enabled} "
                    f"(was {self.enabled}) new_dry_run={new_dry_run} "
                    f"clean_circuit_count={len(new_circuits)}"
                )
                for ci, c in enumerate(new_circuits):
                    for ri, r in enumerate(c.get("rooms") or []):
                        logger.debug(
                            f"[apply_config] cleaned room "
                            f"circuit[{ci}].id={c.get('id')} "
                            f"room[{ri}].id={r.get('id')} "
                            f"target_temp={r.get('target_temp')!r}"
                        )
            except Exception as diag_err:
                logger.warning(f"[apply_config] post-parse-diag failed: {diag_err}")

            diff = self._diff_config(
                old_circuits=self.circuits,
                new_circuits=new_circuits,
                old_enabled=self.enabled,
                new_enabled=new_enabled,
                old_dry_run=self.dry_run,
                new_dry_run=new_dry_run,
            )

            # Atomic swap. self.circuits is read by tick code; we only get
            # here while the config lock is held, so no tick is mid-flight.
            self.circuits = new_circuits
            self.enabled = new_enabled
            self.dry_run = new_dry_run
            self._wx_enabled = new_wx_enabled
            self._wx_off_c = new_wx_off_c
            self._wx_on_c = new_wx_on_c
            self._wx_lookahead_h = new_wx_lookahead_h
            self._wx_forecast_min_c = new_wx_forecast_min_c
            self._oh_enabled = new_oh_enabled
            self._oh_weekday_start = new_oh_weekday_start
            self._oh_weekday_end = new_oh_weekday_end
            self._oh_weekend_start = new_oh_weekend_start
            self._oh_weekend_end = new_oh_weekend_end
            self._oh_setback_offset = new_oh_setback_offset
            self._oh_action = new_oh_action
            valid_circuit_ids = {c["id"] for c in new_circuits}
            valid_room_ids = {
                r["id"]
                for c in new_circuits
                for r in (c.get("rooms") or [])
            }
            self._overshoot = {
                k: v for k, v in self._overshoot.items()
                if k in valid_room_ids
            }
            self._contact = {
                k: v for k, v in self._contact.items()
                if k in valid_room_ids
            }
            self._weather_suppressed = {
                k: v for k, v in self._weather_suppressed.items()
                if k in valid_circuit_ids
            }

            # ── Diagnostic: confirm the swap landed ─────────────────
            try:
                logger.debug(
                    f"[apply_config] post-swap: self.enabled={self.enabled} "
                    f"self.circuits.id={id(self.circuits)} "
                    f"len={len(self.circuits)}"
                )
                for ci, c in enumerate(self.circuits):
                    for ri, r in enumerate(c.get("rooms") or []):
                        logger.debug(
                            f"[apply_config] live-state room "
                            f"circuit[{ci}].id={c.get('id')} "
                            f"room[{ri}].id={r.get('id')} "
                            f"target_temp={r.get('target_temp')!r}"
                        )
            except Exception as diag_err:
                logger.warning(f"[apply_config] post-swap-diag failed: {diag_err}")

            # Forget _last_command entries for receivers/TRVs that no
            # longer exist in the config — otherwise the idempotent gate
            # could suppress a future legitimate command if the same IEEE
            # is later re-added with a different desired state.
            valid_ieees = set()
            for c in new_circuits:
                if c.get("receiver_ieee"):
                    valid_ieees.add(c["receiver_ieee"])
                    valid_ieees.add(f"{c['receiver_ieee']}:setpoint")
                for r in c.get("rooms") or []:
                    for t in r.get("trvs") or []:
                        if isinstance(t, dict) and t.get("ieee"):
                            valid_ieees.add(t["ieee"])
            stale = [k for k in self._last_command if k not in valid_ieees]
            for k in stale:
                del self._last_command[k]

            # _trv_config_applied: keep entries for TRVs still in config,
            # drop entries for removed TRVs (so if they're re-added later
            # we re-apply persistent settings).
            current_trvs = {
                t["ieee"]
                for c in new_circuits
                for r in c.get("rooms") or []
                for t in (r.get("trvs") or [])
                if isinstance(t, dict) and t.get("ieee")
            }
            self._trv_config_applied &= current_trvs

        # Audit-trail log — shows exactly what changed and why.
        if diff.get("any_changes"):
            logger.info(
                f"Heating Controller config reloaded ({reason}): "
                f"{diff['summary']}"
            )
        else:
            logger.info(
                f"Heating Controller config reloaded ({reason}): no changes"
            )

        # Trigger an immediate tick so the UI reflects the new config
        # within seconds, not at the end of the next scheduled interval.
        # Done outside the config lock so the tick can take it.
        tick_triggered = False
        if self.enabled and diff.get("any_changes"):
            try:
                asyncio.create_task(self._tick())
                tick_triggered = True
            except Exception as e:
                logger.warning(f"post-reload tick scheduling failed: {e}")

        return {
            "applied": True,
            "diff": diff,
            "restart_required_fields": [],
            "tick_triggered": tick_triggered,
        }

    def _diff_config(self, old_circuits: List[Dict], new_circuits: List[Dict],
                     old_enabled: bool, new_enabled: bool,
                     old_dry_run: bool, new_dry_run: bool) -> Dict[str, Any]:
        """
        Compute a structured diff between old and new config. Used both for
        the audit-trail log line and the response payload that the
        frontend turns into a toast.
        """
        # Index circuits and rooms by id for fast lookup
        old_circ_by_id = {c["id"]: c for c in old_circuits}
        new_circ_by_id = {c["id"]: c for c in new_circuits}

        # Track per-room comparable fields. If you add a new room field
        # that the user can edit, list it here so the diff picks it up.
        ROOM_FIELDS = (
            "name", "target_temp", "night_setback", "min_temp",
            "temperature_sensor_ieee", "external_temp_mode",
            "external_temp_push_interval_sec",
            "freshness_threshold_minutes",
        )

        circuits_added = [
            {"id": c["id"], "name": c["name"]}
            for c in new_circuits if c["id"] not in old_circ_by_id
        ]
        circuits_removed = [
            {"id": c["id"], "name": c["name"]}
            for c in old_circuits if c["id"] not in new_circ_by_id
        ]

        rooms_added: List[Dict] = []
        rooms_removed: List[Dict] = []
        rooms_changed: List[Dict] = []

        # Walk new circuits — pick up adds and changes
        for nc in new_circuits:
            oc = old_circ_by_id.get(nc["id"])
            new_rooms_by_id = {r["id"]: r for r in nc.get("rooms") or []}
            old_rooms_by_id = {r["id"]: r for r in (oc.get("rooms") if oc else [])}

            for rid, nr in new_rooms_by_id.items():
                or_ = old_rooms_by_id.get(rid)
                if or_ is None:
                    rooms_added.append({
                        "circuit_id": nc["id"], "room_id": rid,
                        "name": nr.get("name", rid),
                    })
                    continue
                changed_fields = [
                    f for f in ROOM_FIELDS
                    if or_.get(f) != nr.get(f)
                ]
                # TRV list — compare as set of (ieee + sorted feature flags)
                or_trvs = sorted(
                    [self._trv_signature(t) for t in (or_.get("trvs") or [])]
                )
                nr_trvs = sorted(
                    [self._trv_signature(t) for t in (nr.get("trvs") or [])]
                )
                if or_trvs != nr_trvs:
                    changed_fields.append("trvs")
                # Schedule — compare full structure
                if (or_.get("schedule") or []) != (nr.get("schedule") or []):
                    changed_fields.append("schedule")
                if changed_fields:
                    rooms_changed.append({
                        "circuit_id": nc["id"],
                        "room_id": rid,
                        "name": nr.get("name", rid),
                        "fields": changed_fields,
                        "before": {f: or_.get(f) for f in changed_fields if f in ROOM_FIELDS},
                        "after": {f: nr.get(f) for f in changed_fields if f in ROOM_FIELDS},
                    })

            # Removed rooms in this circuit
            for rid, or_ in old_rooms_by_id.items():
                if rid not in new_rooms_by_id:
                    rooms_removed.append({
                        "circuit_id": nc["id"], "room_id": rid,
                        "name": or_.get("name", rid),
                    })

        # Rooms in fully-removed circuits
        for oc in old_circuits:
            if oc["id"] not in new_circ_by_id:
                for r in oc.get("rooms") or []:
                    rooms_removed.append({
                        "circuit_id": oc["id"], "room_id": r["id"],
                        "name": r.get("name", r["id"]),
                    })

        controller_changes = []
        if old_enabled != new_enabled:
            controller_changes.append(
                f"enabled: {old_enabled} → {new_enabled}"
            )
        if old_dry_run != new_dry_run:
            controller_changes.append(
                f"dry_run: {old_dry_run} → {new_dry_run}"
            )

        any_changes = bool(
            circuits_added or circuits_removed
            or rooms_added or rooms_removed or rooms_changed
            or controller_changes
        )

        # Human-readable one-line summary for the log
        parts: List[str] = []
        if controller_changes:
            parts.append("controller " + ", ".join(controller_changes))
        if circuits_added:
            parts.append(f"circuits added: {len(circuits_added)}")
        if circuits_removed:
            parts.append(f"circuits removed: {len(circuits_removed)}")
        if rooms_added:
            parts.append(f"rooms added: {len(rooms_added)}")
        if rooms_removed:
            parts.append(f"rooms removed: {len(rooms_removed)}")
        if rooms_changed:
            # Inline the most-common change (target_temp) for readability
            target_changes = [
                f"{r['name']} {r['before'].get('target_temp')} → "
                f"{r['after'].get('target_temp')}°C"
                for r in rooms_changed
                if "target_temp" in r["fields"]
            ]
            if target_changes:
                parts.append("targets: " + "; ".join(target_changes))
            other_count = sum(
                1 for r in rooms_changed if "target_temp" not in r["fields"]
            )
            if other_count:
                parts.append(f"{other_count} other room change(s)")

        return {
            "any_changes": any_changes,
            "summary": "; ".join(parts) if parts else "no changes",
            "circuits_added": circuits_added,
            "circuits_removed": circuits_removed,
            "rooms_added": rooms_added,
            "rooms_removed": rooms_removed,
            "rooms_changed": rooms_changed,
            "controller_changes": controller_changes,
        }

    @staticmethod
    def _trv_signature(t: Any) -> str:
        """Stable string for TRV equality comparison in the diff."""
        if not isinstance(t, dict):
            return str(t)
        ieee = t.get("ieee", "")
        flags = sorted(
            f"{k}={v}" for k, v in t.items()
            if k != "ieee" and v is not None
        )
        return f"{ieee}|{'|'.join(flags)}"

    # ── Config normalisation ───────────────────────────────────────
    def _clean_circuits(self, circuits: list) -> List[Dict]:
        if not isinstance(circuits, list):
            return []
        out = []
        for c in circuits:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            cid = str(c.get("id") or c["name"]).lower().replace(" ", "_")
            rooms = self._clean_rooms(c.get("rooms") or [])
            out.append({
                "id": cid,
                "name": str(c["name"]),
                "receiver_ieee": str(c.get("receiver_ieee") or "").strip() or None,
                "receiver_command": str(c.get("receiver_command", "switch")).lower(),
                "receiver_endpoint": c.get("receiver_endpoint"),
                "rooms": rooms,
                "weather_suppression": _as_bool(c.get("weather_suppression"), None),
                "operating_hours": _as_bool(c.get("operating_hours"), None),
            })
        return out

    def _config_floor_plan(self) -> Optional[Dict[str, Any]]:
        """
        Return the currently-applied floor plan (if any) from the cached
        controller config. Used by thermal_profile to enable the plan-aware
        heat-loss path on projected rooms.
        """
        return getattr(self, "_floor_plan_cache", None)

    def _clean_rooms(self, rooms: list) -> List[Dict]:
        if not isinstance(rooms, list):
            return []
        out = []
        for r in rooms:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            rid = str(r.get("id") or r["name"]).lower().replace(" ", "_")

            # Parse TRVs from either the new 'trvs' list (dicts) or legacy 'trv_ieees' list (strings).
            trvs = self._clean_trvs(r)

            if not trvs and not sensor_ieee:
                logger.warning(
                    f"Room '{rid}' has no TRVs and no temperature_sensor_ieee "
                    f"— it will never call for heat. Ignoring."
                )
                continue
            if not trvs:
                logger.info(
                    f"Room '{rid}' is sensor-only (no TRVs) — call-for-heat "
                    f"will be driven by sensor reading only."
                )

            schedule = r.get("schedule") or []
            if not isinstance(schedule, list):
                schedule = []
            clean_sched = []
            for slot in schedule:
                if not isinstance(slot, dict):
                    continue
                days = slot.get("days") or []
                if not isinstance(days, list):
                    days = []
                clean_sched.append({
                    "days": [d for d in days if d in DAY_KEYS],
                    "start": str(slot.get("start", "07:00")),
                    "end": str(slot.get("end", "22:00")),
                    "temp": _as_float(slot.get("temp"), 20.0),
                })

            sensor_ieee = r.get("temperature_sensor_ieee")
            sensor_ieee = str(sensor_ieee).strip() if sensor_ieee else None
            sensor_ieee = sensor_ieee or None

            mode = str(r.get("external_temp_mode", "advisory" if sensor_ieee else "off")).lower()
            if mode not in ("off", "advisory", "push"):
                mode = "advisory" if sensor_ieee else "off"
            # Push without a sensor is nonsensical — coerce to off.
            if not sensor_ieee and mode == "push":
                logger.warning(
                    f"Room {rid}: external_temp_mode='push' but no temperature_sensor_ieee — treating as 'off'"
                )
                mode = "off"

            push_interval = int(_as_float(
                r.get("external_temp_push_interval_sec"),
                DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC
            ) or DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)

            # Optional contact sensors that influence heating for this room
            contact_in = r.get("contact_sensors") or []
            contact_clean = []
            for cs in contact_in:
                if not isinstance(cs, dict):
                    continue
                ieee = str(cs.get("ieee") or "").strip()
                if not ieee:
                    continue
                contact_clean.append({
                    "ieee": ieee,
                    "name": str(cs.get("name") or ieee),
                    "debounce_open_seconds": int(_as_float(
                        cs.get("debounce_open_seconds"), CONTACT_DEBOUNCE_OPEN_SEC
                    )),
                    "require_temp_drop_c": _as_float(
                        cs.get("require_temp_drop_c"), CONTACT_REQUIRE_TEMP_DROP_C
                    ),
                    "max_close_minutes": int(_as_float(
                        cs.get("max_close_minutes"), CONTACT_MAX_CLOSE_SEC // 60
                    )),
                    "enabled": _as_bool(cs.get("enabled"), True),
                })

            room_out = {
                "id": rid,
                "name": str(r["name"]),
                "target_temp": _as_float(r.get("target_temp"), self._default_target),
                "night_setback": _as_float(r.get("night_setback"), self._default_setback),
                "min_temp": _as_float(r.get("min_temp"), self._default_min),
                "temperature_sensor_ieee": sensor_ieee,
                "external_temp_mode": mode,
                "external_temp_push_interval_sec": push_interval,
                "trvs": trvs,
                # Keep legacy key populated so older code paths still work.
                "trv_ieees": [t["ieee"] for t in trvs],
                "schedule": clean_sched,
                "contact_sensors": contact_clean,
            }

            if isinstance(r.get("dimensions"), dict):
                room_out["dimensions"] = r["dimensions"]
            if isinstance(r.get("radiator"), dict):
                room_out["radiator"] = r["radiator"]
            if isinstance(r.get("radiators"), list):
                room_out["radiators"] = r["radiators"]
            if isinstance(r.get("temperature_sensors"), list):
                room_out["temperature_sensors"] = r["temperature_sensors"]
            if isinstance(r.get("floor_plan_ref"), dict):
                room_out["floor_plan_ref"] = r["floor_plan_ref"]
            out.append(room_out)
        return out


    async def _throttled_send(self, ieee: str, command: str, value=None):
        # Bail if the radio isn't healthy
        res_mgr = getattr(self, "_resilience_manager", None)
        if res_mgr is not None:
            from modules.resilience import ConnectionState
            if res_mgr.state != ConnectionState.CONNECTED:
                logger.debug(
                    f"Skip send {command} to {ieee} — radio is {res_mgr.state}"
                )
                return {"success": False, "error": "Radio not connected"}

        async with self._radio_write_lock:
            gap = time.time() - self._last_radio_write_ts
            if gap < self._min_write_gap:
                await asyncio.sleep(self._min_write_gap - gap)
            try:
                resp = await self._send_command(ieee, command, value)
            finally:
                self._last_radio_write_ts = time.time()
            return resp

    def _clean_trvs(self, room: dict) -> List[Dict]:
        """
        Accept both shapes:
            trvs: [{ieee, window_detection?, child_lock?, valve_detection?}, ...]
            trv_ieees: ["aa:bb:...", ...]
        Later-listed IEEE in either collection wins on conflict (dict form preferred).
        """
        by_ieee: Dict[str, Dict[str, Any]] = {}

        # Legacy list first, so explicit dicts override.
        legacy = room.get("trv_ieees") or []
        if isinstance(legacy, list):
            for ieee in legacy:
                if not ieee:
                    continue
                ieee_s = str(ieee).strip()
                if not ieee_s:
                    continue
                by_ieee[ieee_s] = {
                    "ieee": ieee_s,
                    "window_detection": None,
                    "child_lock": None,
                    "valve_detection": None,
                }

        new = room.get("trvs") or []
        if isinstance(new, list):
            for t in new:
                if isinstance(t, str):
                    ieee_s = t.strip()
                    if ieee_s:
                        by_ieee.setdefault(ieee_s, {
                            "ieee": ieee_s,
                            "window_detection": None,
                            "child_lock": None,
                            "valve_detection": None,
                        })
                elif isinstance(t, dict):
                    ieee_s = str(t.get("ieee") or "").strip()
                    if not ieee_s:
                        continue
                    by_ieee[ieee_s] = {
                        "ieee": ieee_s,
                        "window_detection": _as_bool(t.get("window_detection"), None),
                        "child_lock": _as_bool(t.get("child_lock"), None),
                        "valve_detection": _as_bool(t.get("valve_detection"), None),
                    }

        return list(by_ieee.values())

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self):
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._control_loop())
        self._ext_push_task = asyncio.create_task(self._ext_push_loop())
        logger.info("Heating Controller started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ext_push_task:
            self._ext_push_task.cancel()
            self._ext_push_task = None
        logger.info("Heating Controller stopped")

    async def _control_loop(self):
        # Initial delay so other services finish startup
        await asyncio.sleep(15)
        # Apply persistent per-TRV config once devices are online
        #try:
        #    await self._apply_all_trv_config()
        #except Exception as e:
        #    logger.error(f"Initial TRV config apply failed: {e}", exc_info=True)

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Controller tick failed: {e}", exc_info=True)
            await asyncio.sleep(TICK_INTERVAL_SEC)

    async def _ext_push_loop(self):
        """Background task: push external temperature into Aqara TRVs."""
        await asyncio.sleep(30)  # settle
        while True:
            try:
                await self._push_external_temps_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"External temp push failed: {e}", exc_info=True)
            # Use shortest configured push interval as loop cadence, clamped
            intervals = [
                r.get("external_temp_push_interval_sec", DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)
                for c in self.circuits for r in c["rooms"]
                if r.get("external_temp_mode") == "push"
            ]
            sleep_for = min(intervals) if intervals else DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC
            sleep_for = max(60, min(sleep_for, 1800))
            await asyncio.sleep(sleep_for)

    # ── Public introspection ───────────────────────────────────────
    def get_state(self) -> Dict[str, Any]:
        """Last decision snapshot for the dashboard/API."""
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "last_tick_ts": self._last_decision_ts,
            "last_tick_age_seconds": (time.time() - self._last_decision_ts) if self._last_decision_ts else None,
            "circuits": self._last_decision.get("circuits", []),
        }

    async def force_tick(self) -> Dict[str, Any]:
        """Run one tick on demand (used by API for manual evaluate-now)."""
        await self._tick()
        return self.get_state()

    def find_trv(self, ieee: str) -> Optional[Tuple[Dict, Dict, Dict]]:
        """Locate a TRV in the config. Returns (circuit, room, trv) or None."""
        for c in self.circuits:
            for r in c["rooms"]:
                for t in r["trvs"]:
                    if t["ieee"] == ieee:
                        return c, r, t
        return None

    # ── Core control loop ──────────────────────────────────────────
    async def _tick(self):
        # Gate: don't fire radio operations when the stack is in recovery
        try:
            from modules.resilience import ConnectionState
            res_mgr = getattr(self, "_resilience_manager", None)
            if res_mgr is None:
                # Fallback: try to find it on the zigbee app
                app = getattr(self._get_devices, "__self__", None)
                if app and hasattr(app, "_resilience_manager"):
                    res_mgr = app._resilience_manager
            if res_mgr and res_mgr.state != ConnectionState.CONNECTED:
                logger.debug(
                    f"Skipping heating tick — radio state is {res_mgr.state}"
                )
                return
        except Exception:
            pass  # if we can't check, proceed — fail-open

        # Hold the config lock for the duration of the tick body. This
        # blocks apply_config() until we're done. Acquiring lock should be
        # near-instant in practice (apply_config is a CPU-bound dict swap).
        async with self._config_lock:
            await self._tick_body()

    async def _tick_body(self):
        # ── Diagnostic: confirm what the tick is reading ───────────
        # If apply_config swapped self.circuits but a stale snapshot is
        # being held somewhere, this will reveal it. We log id() of the
        # circuits list because two distinct list objects with the same
        # contents will have different ids — useful for catching
        # "I held a reference to the old list" bugs.
        try:
            logger.debug(
                f"[tick] reading self.circuits id={id(self.circuits)} "
                f"len={len(self.circuits)}"
            )
            for ci, c in enumerate(self.circuits):
                for ri, r in enumerate(c.get("rooms") or []):
                    logger.debug(
                        f"[tick] using room "
                        f"circuit[{ci}].id={c.get('id')} "
                        f"room[{ri}].id={r.get('id')} "
                        f"target_temp={r.get('target_temp')!r}"
                    )
        except Exception as diag_err:
            logger.warning(f"[tick] entry-diag failed: {diag_err}")

        devices = self._snapshot_devices()
        now = datetime.now()
        circuits_out = []

        for circuit in self.circuits:
            room_decisions = []
            any_calling = False

            for room in circuit["rooms"]:
                room["_oh_enabled_for_circuit"] = circuit.get("operating_hours")
                decision = self._evaluate_room(room, devices, now)
                if decision.calling_for_heat:
                    any_calling = True
                room_decisions.append(decision)

            # Circuit-level decision (pre-suppression)
            any_calling_pre_wx = any_calling
            should_call = any_calling

            # Weather suppression — runs after room eval, before receiver action.
            wx = self._evaluate_weather_suppression(circuit, room_decisions)
            if wx["active"] and should_call:
                logger.info(
                    f"Circuit '{circuit['name']}': weather-suppressed "
                    f"(would-have-called=True). {wx['reason']}"
                )
                should_call = False

            receiver_action = await self._apply_receiver(circuit, should_call)

            # TRV decisions — pass any_calling so we can force-close hot rooms
            trv_actions = []
            for decision in room_decisions:
                room = next((r for r in circuit["rooms"] if r["id"] == decision.room_id), None)
                if room is None:
                    continue
                actions = await self._apply_trvs(room, decision, circuit_calling=should_call)
                trv_actions.extend(actions)
                # Decorate decision dict with the TRV setpoint commands made
                for trv in decision.trvs:
                    a = next((a for a in actions if a["ieee"] == trv["ieee"]), None)
                    if a:
                        trv["intended_setpoint"] = a["target_setpoint"]
                        trv["action"] = a["action"]

            # Surface the receiver's live running_state / system_mode so the
            # controller panel can show whether the boiler is actually firing.
            recv_state = {}
            rx_ieee = circuit.get("receiver_ieee")
            if rx_ieee and rx_ieee in devices:
                rx_dev = devices[rx_ieee]
                rs_dict = (
                              rx_dev.get("state") if isinstance(rx_dev, dict)
                              else getattr(rx_dev, "state", None)
                          ) or {}
                rs = rs_dict.get("running_state")
                rs_on = False
                if isinstance(rs, (int, float)):
                    rs_on = bool(int(rs) & 0x0001)
                elif isinstance(rs, str) and "heat" in rs.lower():
                    rs_on = True
                recv_state = {
                    "running_state": rs,
                    "running": rs_on,
                    "system_mode": rs_dict.get("system_mode"),
                    "setpoint": rs_dict.get("occupied_heating_setpoint"),
                }

            circuits_out.append({
                "id": circuit["id"],
                "name": circuit["name"],
                "calling_for_heat": should_call,
                "any_room_calling": any_calling_pre_wx,
                "receiver_ieee": circuit["receiver_ieee"],
                "receiver_action": receiver_action,
                "receiver_state": recv_state,
                "weather": wx,
                "rooms": [d.to_dict() for d in room_decisions],
                "trv_actions": trv_actions,
            })

        self._last_decision = {"circuits": circuits_out}
        self._last_decision_ts = time.time()

        # Persist this tick for anomaly detection & post-hoc analysis.
        # Non-blocking in spirit: if the DB write fails we log and move on,
        # we never want a telemetry issue to stop control.
        if _write_heating_tick is not None:
            try:
                _write_heating_tick(
                    ts=self._last_decision_ts,
                    dry_run=self.dry_run,
                    circuits=circuits_out,
                )
            except Exception as e:
                logger.warning(f"tick persistence failed (non-fatal): {e}")

    # ── Device snapshot ────────────────────────────────────────────
    def _snapshot_devices(self) -> Dict[str, Any]:
        try:
            raw = self._get_devices() or {}
        except Exception as e:
            logger.error(f"device_getter raised: {e}")
            return {}
        return {str(ieee): dev for ieee, dev in raw.items()}

    # ── Room evaluation ────────────────────────────────────────────
    def _evaluate_room(self, room: dict, devices: Dict[str, Any],
                       now: datetime) -> RoomDecision:
        decision = RoomDecision(room_id=room["id"], name=room["name"])
        base_target = self._effective_target(room, now)
        decision.target_temp = base_target

        # Gather TRV state
        trv_temps = []
        trvs = []
        for t in room["trvs"]:
            ieee = t["ieee"]
            dev = devices.get(ieee)
            if dev is None:
                trvs.append({
                    "ieee": ieee,
                    "name": ieee,
                    "current_temp": None,
                    "current_setpoint": None,
                    "online": False,
                    "valve_alarm": False,
                    "window_open": False,
                })
                continue
            state = _device_state(dev)
            temp = _pick_temperature(state)
            setpoint = state.get("occupied_heating_setpoint") or state.get("target_temp")
            if temp is not None:
                trv_temps.append(temp)
            trvs.append({
                "ieee": ieee,
                "name": _device_friendly_name(dev, ieee),
                "current_temp": temp,
                "current_setpoint": _as_float(setpoint),
                "online": True,
                "valve_alarm": bool(state.get("valve_alarm")),
                "window_open": bool(state.get("window_open")),
            })

        decision.trvs = trvs

        # Pick room temperature: external sensor wins if present & reading.
        sensor_ieee = room.get("temperature_sensor_ieee")
        ext_mode = room.get("external_temp_mode", "off")
        decision.sensor_ieee = sensor_ieee

        ext_temp: Optional[float] = None
        sensor_raw: Dict[str, Any] = {}  # diagnostic: raw temp keys from sensor state
        sensor_present_in_devices = False
        if sensor_ieee and ext_mode != "off":
            sensor_dev = devices.get(sensor_ieee)
            if sensor_dev is not None:
                sensor_present_in_devices = True
                sensor_state = _device_state(sensor_dev)
                sensor_raw = {
                    k: sensor_state.get(k)
                    for k in ("local_temperature", "current_temperature", "temperature")
                    if k in sensor_state
                }
                ext_temp = _pick_temperature(sensor_state)
                decision.sensor_online = ext_temp is not None
            else:
                decision.sensor_online = False

        # ── Apply stratification correction to the chosen reading ─────
        # Only the external sensor path has a configurable mounting height
        # — TRVs sit near the floor by their nature, but their readings
        # already factor in convective bias from their proximity to the
        # radiator, so a separate height correction would be misleading.
        # If the chosen source is "external" and the primary sensor has a
        # height set, we shift the reported reading toward the 1.5 m
        # comfort-zone reference.
        sensor_height_m = _primary_sensor_height_m(room)
        decision.sensor_height_m = sensor_height_m

        if ext_temp is not None:
            corrected = correct_sensor_reading(ext_temp, sensor_height_m)
            decision.current_temp_raw = round(ext_temp, 1)
            decision.current_temp = corrected if corrected is not None else round(ext_temp, 1)
            decision.temp_source = "external"
            decision.stratification_offset_c = (
                round(decision.current_temp - decision.current_temp_raw, 2)
                if corrected is not None else 0.0
            )
        elif trv_temps:
            mean = sum(trv_temps) / len(trv_temps)
            decision.current_temp = round(mean, 1)
            decision.current_temp_raw = decision.current_temp
            decision.temp_source = "trv_mean"
            decision.stratification_offset_c = 0.0
        else:
            decision.current_temp = None
            decision.current_temp_raw = None
            decision.temp_source = "none"
            decision.stratification_offset_c = 0.0

        # Predictive pre-heat — bump target if an upcoming schedule slot
        # needs lead time, based on the cached thermal profile.
        try:
            preheat_target, preheat_info = self._maybe_preheat_target(
                room, decision.current_temp, now
            )
            if preheat_info is not None:
                decision.preheat = preheat_info
            if preheat_target is not None:
                decision.target_temp = preheat_target
        except Exception as e:
            logger.debug(f"preheat eval failed for {room['id']}: {e}")

        # ── Adaptive overshoot compensation ────────────────────
        # Look up prior tick's calling state for this room.
        prior_calling = False
        try:
            for c in (self._last_decision.get("circuits") or []):
                if c.get("id") != circuit["id"]:
                    continue
                for r in (c.get("rooms") or []):
                    if r.get("room_id") == room["id"]:
                        prior_calling = bool(r.get("calling_for_heat"))
                        break
                break
        except Exception:
            pass

        # Compute provisional calling state using fixed COLD_BAND so the
        # overshoot tracker sees the unmodified cold→ontarget edge.
        provisional_calling = (
                decision.current_temp is not None
                and decision.target_temp is not None
                and decision.current_temp < decision.target_temp - COLD_BAND
        )

        learned_offset, overshoot_info = self._track_overshoot(
            room_id=room["id"],
            target_temp=decision.target_temp,
            current_temp=decision.current_temp,
            was_calling=prior_calling,
            is_now_calling_pre_offset=provisional_calling,
            now_ts=time.time(),
        )
        decision.overshoot = overshoot_info

        # Effective cutoff widens by learned offset so we stop calling
        # earlier on rooms that historically overshoot.
        effective_cold_band = max(COLD_BAND, learned_offset)

        # Classify with adaptive cutoff
        if decision.current_temp is None or decision.target_temp is None:
            decision.status = "unknown"
            decision.calling_for_heat = False
        elif decision.current_temp < decision.target_temp - effective_cold_band:
            decision.status = "cold"
            decision.calling_for_heat = True

            # ── Solar suppression check ───────────────────────────────
            # If solar gain alone can cover most of the temperature deficit,
            # suppress the heat call for this tick. The boiler stays off and
            # we re-evaluate on the next tick. Conservative: only suppresses
            # when solar can cover SOLAR_SUPPRESS_FRACTION of the deficit so
            # a cloud passing over doesn't strand a cold room for long.
            if self._weather is not None:
                try:
                    _solar_profile = self._get_room_profile(room)
                    _w_per_k = (_solar_profile or {}).get("w_per_k")
                    if _w_per_k:
                        from modules.solar_gain import should_suppress_heat_call
                        lat = getattr(self._weather, "latitude", None)
                        lon = getattr(self._weather, "longitude", None)
                        if lat is not None and lon is not None:
                            suppress, solar_w = should_suppress_heat_call(
                                room_config=room,
                                lat=lat,
                                lon=lon,
                                current_temp_c=decision.current_temp,
                                target_temp_c=decision.target_temp,
                                w_per_k=_w_per_k,
                                shortwave_wm2=self._weather.get_solar_irradiance(),
                                cloud_fraction=self._weather.get_cloud_fraction() or 0.0,
                            )
                            if suppress and solar_w > 0:
                                logger.info(
                                    f"Room '{decision.name}': solar suppressing heat call "
                                    f"(solar={solar_w:.0f}W, deficit="
                                    f"{decision.target_temp - decision.current_temp:.1f}°C)"
                                )
                                decision.status = "solar_suppressed"
                                decision.calling_for_heat = False
                except Exception as ss_err:
                    logger.debug(f"solar suppression check failed for {room['id']}: {ss_err}")

        elif decision.current_temp > decision.target_temp + HOT_BAND:
            decision.status = "hot"
            decision.calling_for_heat = False
        else:
            decision.status = "ontarget"

        # ── Door/window contact override ──────────────────────
        contact = self._evaluate_contact_state(
            room, devices, decision.current_temp, time.time()
        )
        decision.contact = contact
        if contact["is_active"]:
            decision.status = "contact_open"
            decision.calling_for_heat = False

        # Health check — runs after classification so it can use temp_source.
        # Failures are logged as WARNING so they're visible in journalctl
        # without needing the panel open.
        try:
            decision.health = _check_room_health(
                room, devices, decision,
                sensor_present_in_devices=sensor_present_in_devices,
                sensor_raw=sensor_raw,
            )
            if decision.health["level"] != "ok":
                logger.warning(
                    f"Room '{decision.name}' health={decision.health['level']}: "
                    + "; ".join(decision.health["reasons"])
                )
        except Exception as e:
            logger.error(f"Health check failed for room {decision.name}: {e}")
            decision.health = {"level": "ok", "reasons": [],
                               "stale_devices": [], "threshold_minutes": 15}

        # ── Diagnostic log line ───────────────────────────────────────
        # Surfaces every input that fed the classification, so a surprising
        # call-for-heat can be traced to the exact value that caused it.
        try:
            trv_dbg = [
                f"{t['ieee'][-8:]}={t['current_temp']}"
                for t in decision.trvs
            ]
            if sensor_ieee:
                if not sensor_present_in_devices:
                    sensor_dbg = f"sensor={sensor_ieee[-8:]}:NOT_IN_SNAPSHOT"
                elif not sensor_raw:
                    sensor_dbg = f"sensor={sensor_ieee[-8:]}:no_temp_keys"
                else:
                    sensor_dbg = (
                            f"sensor={sensor_ieee[-8:]}:"
                            + ",".join(f"{k}={v}" for k, v in sensor_raw.items())
                            + f" picked={ext_temp}"
                    )
            else:
                sensor_dbg = "sensor=none"
            logger.info(
                f"Room '{decision.name}' eval: "
                f"target={decision.target_temp} "
                f"current={decision.current_temp} "
                f"source={decision.temp_source} "
                f"status={decision.status} "
                f"calling={decision.calling_for_heat} "
                f"health={decision.health['level']} | "
                f"{sensor_dbg} | trvs=[{', '.join(trv_dbg) or 'none'}]"
            )
        except Exception as e:  # never let diagnostics break a tick
            logger.debug(f"Diagnostic log failed for room {decision.name}: {e}")

        return decision

    WEEKEND_DAYS = ("sat", "sun")

    def _is_within_operating_hours(self, now: datetime) -> Tuple[bool, str]:
        """
        Returns (is_day, period_label). period_label is one of
        "weekday-day", "weekday-night", "weekend-day", "weekend-night".
        """
        day = DAY_KEYS[now.weekday()]
        is_weekend = day in self.WEEKEND_DAYS
        if is_weekend:
            start_s, end_s = self._oh_weekend_start, self._oh_weekend_end
        else:
            start_s, end_s = self._oh_weekday_start, self._oh_weekday_end

        start_m = _parse_hhmm(start_s)
        end_m = _parse_hhmm(end_s)
        now_minutes = now.hour * 60 + now.minute

        if start_m is None or end_m is None:
            return True, ("weekend-day" if is_weekend else "weekday-day")

        in_day = ((start_m <= now_minutes < end_m) if start_m <= end_m
                  else (now_minutes >= start_m or now_minutes < end_m))
        prefix = "weekend" if is_weekend else "weekday"
        return in_day, f"{prefix}-{'day' if in_day else 'night'}"

    def _effective_target(self, room: dict, now: datetime) -> float:
        """
        Resolution order (most-specific first):
          1. Active per-room schedule slot
          2. Within operating hours → room.target_temp
          3. Outside operating hours → setback / min_only / off (handled
             by caller via -inf sentinel for 'off')
        """
        day = DAY_KEYS[now.weekday()]
        now_minutes = now.hour * 60 + now.minute

        # 1. Schedule slots (most specific)
        for slot in room.get("schedule", []):
            if day not in (slot.get("days") or []):
                continue
            start_m = _parse_hhmm(slot.get("start", "00:00"))
            end_m = _parse_hhmm(slot.get("end", "23:59"))
            if start_m is None or end_m is None:
                continue
            in_slot = ((start_m <= now_minutes < end_m) if start_m <= end_m
                       else (now_minutes >= start_m or now_minutes < end_m))
            if in_slot:
                return float(slot.get("temp", room["target_temp"]))

        target = float(room.get("target_temp", self._default_target))
        min_t = float(room.get("min_temp", self._default_min))

        # 2. Operating-hours framework — opt-in per circuit/global
        oh_circuit = room.get("_oh_enabled_for_circuit", None)
        oh_active = self._oh_enabled if oh_circuit is None else bool(oh_circuit)

        if oh_active:
            is_day, _ = self._is_within_operating_hours(now)
            if is_day:
                return target
            # Out-of-hours
            if self._oh_action == "off":
                # Sentinel — caller will floor at min_temp and never call for heat
                return min_t
            if self._oh_action == "min_only":
                return min_t
            # "setback"
            setback = target + self._oh_setback_offset
            return max(setback, min_t)

        # 3. Legacy fallback (operating_hours disabled): old 22-06 setback
        if now_minutes >= 22 * 60 or now_minutes < 6 * 60:
            return float(room.get("night_setback", self._default_setback))

        return target

    # ── Weather-based suppression ──────────────────────────────────
    def _forecast_window_min(self, lookahead_h: int) -> Optional[float]:
        """Min forecast temperature in the next `lookahead_h` hours from now."""
        if not self._weather:
            return None
        try:
            fc = self._weather.get_forecast() or {}
        except Exception:
            return None
        times = fc.get("times") or []
        temps = fc.get("temperature_2m") or []
        if not times or not temps:
            return None

        now_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
        start_idx = 0
        for i, t_str in enumerate(times):
            try:
                if datetime.fromisoformat(str(t_str)) >= now_dt:
                    start_idx = i
                    break
            except (ValueError, TypeError):
                continue

        window = [
            t for t in temps[start_idx:start_idx + lookahead_h]
            if isinstance(t, (int, float))
        ]
        return min(window) if window else None

    # ── Window/door open detection ─────────────────────────────────
    def _evaluate_contact_state(
            self, room: dict, devices: Dict[str, Any],
            current_temp: Optional[float], now_ts: float
    ) -> Dict[str, Any]:
        """
        Update the window state machine for a room and return a snapshot.

        States:
          closed       — no configured sensor reports open
          open_pending — sensor open, debounce or temp-drop not yet met
          open_active  — force-close TRVs, stop calling for heat

        The temp-drop guard means a brief vent (e.g. steam off the hob)
        won't kill the radiator: if the room doesn't actually cool, we
        stay in pending forever (or until the door closes).
        """
        rid = room["id"]
        contact_cfg = room.get("contact_sensors") or []
        st = self._contact.setdefault(rid, {
            "state": "closed",
            "sensors_open": set(),
            "opened_ts": None,
            "opened_temp": None,
            "activated_ts": None,
            "last_closed_ts": None,
            "observed_drop_c": None,
            "reason": "",
        })

        if not contact_cfg:
            st["state"] = "closed"
            st["sensors_open"] = set()
            return self._contact_snapshot(st)

        # Resolve which configured sensors are currently reporting open
        open_now = set()
        active_sensors_meta: List[Dict[str, Any]] = []
        for cs in contact_cfg:
            if not cs.get("enabled", True):
                continue
            dev = devices.get(cs["ieee"])
            is_open = False
            if dev is not None:
                state = _device_state(dev)
                # Security handler exposes is_open=True when magnet separated.
                # Tolerate either key in case other handlers populate "contact".
                if "is_open" in state:
                    is_open = bool(state.get("is_open"))
                elif "contact" in state:
                    is_open = not bool(state.get("contact"))
            active_sensors_meta.append({
                "ieee": cs["ieee"], "name": cs["name"], "is_open": is_open,
            })
            if is_open:
                open_now.add(cs["ieee"])

        st["sensors_open"] = open_now
        any_open = bool(open_now)

        # Cache resolved config for whichever sensor opened first
        # (use largest debounce / drop / max-close among open sensors)
        debounce_s = CONTACT_DEBOUNCE_OPEN_SEC
        drop_required = CONTACT_REQUIRE_TEMP_DROP_C
        max_close_s = CONTACT_MAX_CLOSE_SEC
        if any_open:
            relevant = [c for c in contact_cfg if c["ieee"] in open_now]
            if relevant:
                debounce_s = max(c["debounce_open_seconds"] for c in relevant)
                drop_required = max(c["require_temp_drop_c"] for c in relevant)
                max_close_s = max(c["max_close_minutes"] * 60 for c in relevant)

        prev_state = st["state"]

        # ── Transitions ────────────────────────────────────────
        if prev_state == "closed":
            if any_open:
                st["state"] = "open_pending"
                st["opened_ts"] = now_ts
                st["opened_temp"] = current_temp
                st["activated_ts"] = None
                st["observed_drop_c"] = None
                st["reason"] = f"sensor opened, debouncing {debounce_s}s"

        elif prev_state == "open_pending":
            if not any_open:
                st["state"] = "closed"
                st["last_closed_ts"] = now_ts
                st["opened_ts"] = None
                st["opened_temp"] = None
                st["reason"] = "sensor closed during debounce"
            else:
                elapsed = now_ts - (st["opened_ts"] or now_ts)
                drop = None
                if (st["opened_temp"] is not None and current_temp is not None):
                    drop = st["opened_temp"] - current_temp
                    st["observed_drop_c"] = round(drop, 2)

                if elapsed < debounce_s:
                    st["reason"] = (
                        f"open {int(elapsed)}s/{debounce_s}s — debouncing"
                    )
                else:
                    # Past debounce — temp-drop guard decides escalation
                    if drop is None:
                        st["reason"] = "no temperature reading — staying pending"
                    elif drop >= drop_required:
                        st["state"] = "open_active"
                        st["activated_ts"] = now_ts
                        st["reason"] = (
                            f"temp drop {drop:.1f}°C ≥ {drop_required:.1f}°C "
                            f"— force-closing valves"
                        )
                    elif elapsed > CONTACT_REQUIRE_DROP_WINDOW_SEC:
                        st["reason"] = (
                            f"open {int(elapsed/60)}m, drop {drop:.1f}°C "
                            f"< {drop_required:.1f}°C — likely steam vent, "
                            f"holding heat"
                        )
                    else:
                        st["reason"] = (
                            f"open {int(elapsed)}s, drop {drop:.1f}°C "
                            f"< {drop_required:.1f}°C — watching"
                        )

        elif prev_state == "open_active":
            if not any_open:
                st["state"] = "closed"
                st["last_closed_ts"] = now_ts
                st["opened_ts"] = None
                st["opened_temp"] = None
                st["activated_ts"] = None
                st["reason"] = "sensor closed — resuming normal control"
            else:
                active_for = now_ts - (st["activated_ts"] or now_ts)
                if active_for > max_close_s:
                    # Safety release — sensor stuck open?
                    st["state"] = "closed"
                    st["last_closed_ts"] = now_ts
                    st["reason"] = (
                        f"max_close {int(max_close_s/60)}min reached — "
                        f"safety release; sensor still open"
                    )
                else:
                    st["reason"] = (
                        f"active {int(active_for/60)}m — valves force-closed"
                    )

        snap = self._contact_snapshot(st)
        snap["sensors"] = active_sensors_meta
        snap["debounce_seconds"] = debounce_s
        snap["drop_required_c"] = drop_required
        snap["max_close_minutes"] = int(max_close_s / 60)
        return snap

    @staticmethod
    def _contact_snapshot(st: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "state": st["state"],
            "reason": st.get("reason", ""),
            "opened_ts": st.get("opened_ts"),
            "activated_ts": st.get("activated_ts"),
            "observed_drop_c": st.get("observed_drop_c"),
            "is_active": st["state"] == "open_active",
        }

    # ── Adaptive overshoot compensation ────────────────────────────
    def _track_overshoot(
            self, room_id: str, target_temp: Optional[float],
            current_temp: Optional[float], was_calling: bool,
            is_now_calling_pre_offset: bool, now_ts: float
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Run one step of per-room overshoot learning.

        Returns (learned_offset_c, info_dict). The offset is the EWMA of
        observed overshoots after stopping a call-for-heat cycle. Info is
        surfaced in the decision dict for the UI.
        """
        st = self._overshoot.setdefault(room_id, {
            "learned_offset_c": 0.0,
            "phase": "idle",            # idle | watching
            "watch_started_ts": None,
            "watch_target": None,
            "peak_temp": None,
            "samples": 0,
            "last_observed_overshoot": None,
        })

        if current_temp is None or target_temp is None:
            return st["learned_offset_c"], {
                "learned_offset_c": st["learned_offset_c"],
                "phase": st["phase"],
                "samples": st["samples"],
                "last_observed_overshoot": st["last_observed_overshoot"],
            }

        # 1. Trigger watch when we just stopped calling
        if st["phase"] == "idle" and was_calling and not is_now_calling_pre_offset:
            st["phase"] = "watching"
            st["watch_started_ts"] = now_ts
            st["watch_target"] = target_temp
            st["peak_temp"] = current_temp

        # 2. Watching: track peak, record on drop or timeout, abandon if calling resumes
        elif st["phase"] == "watching":
            if is_now_calling_pre_offset:
                # Calling resumed before we saw a peak — abandon, no sample
                st["phase"] = "idle"
                st["peak_temp"] = None
                st["watch_target"] = None
                st["watch_started_ts"] = None
            else:
                if current_temp > (st["peak_temp"] or current_temp):
                    st["peak_temp"] = current_temp

                peak = st["peak_temp"] or current_temp
                started = st["watch_started_ts"] or now_ts
                timed_out = (now_ts - started) > OVERSHOOT_PEAK_TIMEOUT_SEC
                dropped = current_temp <= (peak - OVERSHOOT_PEAK_DROP_C)

                if dropped or timed_out:
                    observed = max(0.0, peak - (st["watch_target"] or target_temp))
                    observed = min(observed, OVERSHOOT_MAX_OFFSET_C * 2)  # outlier guard
                    alpha = OVERSHOOT_LEARN_ALPHA
                    if st["samples"] == 0:
                        new_offset = observed
                    else:
                        new_offset = (1 - alpha) * st["learned_offset_c"] + alpha * observed
                    st["learned_offset_c"] = max(0.0, min(new_offset, OVERSHOOT_MAX_OFFSET_C))
                    st["last_observed_overshoot"] = round(observed, 2)
                    st["samples"] += 1
                    st["phase"] = "idle"
                    st["peak_temp"] = None
                    st["watch_target"] = None
                    st["watch_started_ts"] = None

        return st["learned_offset_c"], {
            "learned_offset_c": round(st["learned_offset_c"], 2),
            "phase": st["phase"],
            "samples": st["samples"],
            "last_observed_overshoot": st["last_observed_overshoot"],
        }

    def _evaluate_weather_suppression(
            self, circuit: dict, decisions: List["RoomDecision"]
    ) -> Dict[str, Any]:
        """
        Decide whether to suppress this circuit's call-for-heat based on outdoor
        conditions. Sticky hysteresis lives in self._weather_suppressed.

        Safety overrides (one-tick, don't clear sticky state):
          - any room < its min_temp        → never suppress
          - any room with health != 'ok'   → never suppress
          - no outdoor reading             → never suppress (fail-safe)
        """
        cid = circuit["id"]
        per_circuit = circuit.get("weather_suppression")
        enabled = self._wx_enabled if per_circuit is None else bool(per_circuit)

        out = {
            "enabled": enabled,
            "active": False,
            "sticky": self._weather_suppressed.get(cid, False),
            "outdoor_current": None,
            "forecast_min_window": None,
            "lookahead_hours": self._wx_lookahead_h,
            "off_threshold_c": self._wx_off_c,
            "on_threshold_c": self._wx_on_c,
            "forecast_min_c": self._wx_forecast_min_c,
            "reason": "",
        }

        if not enabled or self._weather is None:
            self._weather_suppressed[cid] = False
            out["sticky"] = False
            out["reason"] = "disabled" if not enabled else "no weather service"
            return out

        try:
            outdoor = self._weather.get_outdoor_temperature()
        except Exception as e:
            logger.debug(f"weather: get_outdoor_temperature failed: {e}")
            outdoor = None

        forecast_min = self._forecast_window_min(self._wx_lookahead_h)
        out["outdoor_current"] = outdoor
        out["forecast_min_window"] = forecast_min

        if outdoor is None:
            self._weather_suppressed[cid] = False
            out["sticky"] = False
            out["reason"] = "no outdoor reading — fail-safe"
            return out

        sticky = self._weather_suppressed.get(cid, False)
        if sticky:
            if outdoor < self._wx_on_c:
                sticky = False
                out["reason"] = (
                    f"outdoor {outdoor:.1f}°C < on_threshold "
                    f"{self._wx_on_c:.1f}°C — release"
                )
            elif forecast_min is not None and forecast_min < self._wx_forecast_min_c:
                sticky = False
                out["reason"] = (
                    f"forecast min {forecast_min:.1f}°C in next "
                    f"{self._wx_lookahead_h}h < {self._wx_forecast_min_c:.1f}°C — release"
                )
            else:
                fc_str = f"{forecast_min:.1f}°C" if forecast_min is not None else "n/a"
                out["reason"] = f"outdoor {outdoor:.1f}°C, forecast min {fc_str} — held"
        else:
            forecast_clear = (forecast_min is None) or (forecast_min >= self._wx_forecast_min_c)
            if outdoor >= self._wx_off_c and forecast_clear:
                sticky = True
                fc_str = f"{forecast_min:.1f}°C" if forecast_min is not None else "n/a"
                out["reason"] = (
                    f"outdoor {outdoor:.1f}°C ≥ off_threshold "
                    f"{self._wx_off_c:.1f}°C, forecast min {fc_str} — engage"
                )
            else:
                fc_str = f"{forecast_min:.1f}°C" if forecast_min is not None else "n/a"
                out["reason"] = f"outdoor {outdoor:.1f}°C, forecast min {fc_str} — clear"

        self._weather_suppressed[cid] = sticky
        out["sticky"] = sticky

        if not sticky:
            return out

        # Pull anomaly snapshot once for this circuit
        active_anomaly_room_ids: set = set()
        if self._anomaly_getter is not None:
            try:
                snap = self._anomaly_getter() or {}
                for a in (snap.get("active") or []):
                    if str(a.get("circuit_id")) == cid:
                        active_anomaly_room_ids.add(str(a.get("room_id")))
            except Exception as e:
                logger.debug(f"anomaly snapshot failed: {e}")

        # Sticky says suppress — check one-tick safety overrides
        for d in decisions:
            room = next((r for r in circuit["rooms"] if r["id"] == d.room_id), None)
            if room is None:
                continue
            min_t = _as_float(room.get("min_temp"), self._default_min)
            if (d.current_temp is not None and min_t is not None
                    and d.current_temp < min_t):
                out["active"] = False
                out["reason"] += (
                    f"; OVERRIDE: room '{d.name}' at {d.current_temp:.1f}°C "
                    f"below min_temp {min_t:.1f}°C"
                )
                return out
            if d.health and d.health.get("level") not in (None, "ok"):
                out["active"] = False
                out["reason"] += (
                    f"; OVERRIDE: room '{d.name}' health="
                    f"{d.health.get('level')} — not safe to suppress"
                )
                return out
            if d.room_id in active_anomaly_room_ids:
                out["active"] = False
                out["reason"] += (
                    f"; OVERRIDE: room '{d.name}' has active anomaly — "
                    f"not safe to suppress"
                )
                return out

        out["active"] = True
        return out


    # ── Predictive pre-heat ────────────────────────────────────────
    def _next_schedule_slot(
            self, room: dict, now: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Find the next schedule slot starting AFTER `now` within the next 24 h.
        Returns {start_dt, temp, minutes_until} or None.
        """
        schedule = room.get("schedule") or []
        if not schedule:
            return None

        candidates = []
        for offset_days in range(0, 2):  # today + tomorrow
            day_dt = now + (datetime.fromtimestamp(0) - datetime.fromtimestamp(0))
            day_dt = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                      + (offset_days * (datetime(2000, 1, 2) - datetime(2000, 1, 1))))
            day_key = DAY_KEYS[day_dt.weekday()]
            for slot in schedule:
                if day_key not in (slot.get("days") or []):
                    continue
                start_m = _parse_hhmm(slot.get("start"))
                if start_m is None:
                    continue
                slot_start = day_dt.replace(
                    hour=start_m // 60, minute=start_m % 60
                )
                if slot_start <= now:
                    continue
                temp = _as_float(slot.get("temp"))
                if temp is None:
                    continue
                candidates.append((slot_start, temp))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        slot_start, temp = candidates[0]
        minutes_until = (slot_start - now).total_seconds() / 60.0
        if minutes_until > PREHEAT_LOOKAHEAD_MAX_MIN:
            return None
        return {
            "start_dt": slot_start,
            "temp": temp,
            "minutes_until": minutes_until,
        }

    def _get_room_profile(self, room: dict) -> Optional[Dict[str, Any]]:
        """
        Lazily compute and cache w_per_k + tau for a room. Returns None if
        we don't have enough data (no dimensions, no telemetry, etc.).
        """
        rid = room["id"]
        cached = self._room_profile_cache.get(rid)
        now_ts = time.time()
        if cached and cached.get("expires_at", 0) > now_ts:
            return cached

        dimensions = room.get("dimensions")
        if not dimensions:
            return None
        if self._weather is None or self._telemetry_query is None:
            return None

        # Build temperature history series for the profile fit
        sensor_ieee = room.get("temperature_sensor_ieee")
        if not sensor_ieee:
            trvs = room.get("trvs") or []
            if trvs and isinstance(trvs[0], dict):
                sensor_ieee = trvs[0].get("ieee")
        if not sensor_ieee:
            return None

        try:
            from modules.thermal_profile import compute_profile
        except Exception as e:
            logger.debug(f"thermal_profile import failed: {e}")
            return None

        series: List[Dict[str, Any]] = []
        try:
            for attr in ("temperature", "local_temperature",
                         "current_temperature"):
                rows = self._telemetry_query(
                    sensor_ieee, attr, PREHEAT_TELEMETRY_HOURS
                ) or []
                if rows:
                    series = rows
                    break
        except Exception as e:
            logger.debug(f"telemetry_query failed for {sensor_ieee}: {e}")

        # Real outdoor temperature history from telemetry — beats the
        # previous constant-current-temp proxy for fits across swingy days.
        outdoor_getter = None
        try:
            from modules.telemetry_db import build_outdoor_temp_getter
            outdoor_getter = build_outdoor_temp_getter(PREHEAT_TELEMETRY_HOURS)
        except Exception as e:
            logger.debug(f"build_outdoor_temp_getter failed: {e}")

        # Fallback: if no history yet (fresh install), use current as constant
        if outdoor_getter is None:
            outdoor_now = None
            try:
                outdoor_now = self._weather.get_outdoor_temperature()
            except Exception:
                pass
            if outdoor_now is None:
                return None
            outdoor_getter = lambda _ts: outdoor_now

        try:
            profile = compute_profile(
                room_id=room_id,
                dimensions=dimensions,
                insulation=insulation,
                temperature_series=temp_series,
                outdoor_temp_getter=outdoor_getter,
                heating_state_getter=heating_state_getter,
                floor_plan=heating.get("floor_plan"),
                floor_plan_ref=found_room.get("floor_plan_ref"),
            )
        except Exception as e:
            logger.debug(f"compute_profile failed for {rid}: {e}")
            return None

        rad_cfg = room.get("radiator") or {}
        radiator_watts = _as_float(rad_cfg.get("watts_at_dt50"), None)

        entry = {
            "w_per_k": prof.blended_w_per_k,
            "tau_seconds": prof.tau_seconds,
            "radiator_watts": radiator_watts,
            "confidence": (
                "high" if (prof.measured_confidence or 0) >= 0.7
                else "medium" if (prof.measured_confidence or 0) >= 0.3
                else "low"
            ),
            "expires_at": now_ts + PROFILE_CACHE_TTL_SEC,
        }
        self._room_profile_cache[rid] = entry
        return entry

    def _maybe_preheat_target(
            self, room: dict, current_temp: Optional[float], now: datetime
    ) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        """
        Decide if we should pre-heat for an upcoming schedule slot.
        Returns (override_target, preheat_info) where override_target is None
        when no preheat is needed.
        """
        if current_temp is None:
            return None, None
        upcoming = self._next_schedule_slot(room, now)
        if not upcoming:
            return None, None

        upcoming_target = upcoming["temp"]
        # Already warm enough — nothing to do
        if current_temp >= upcoming_target:
            return None, None

        profile = self._get_room_profile(room)
        if not profile or not profile.get("w_per_k"):
            return None, {"reason": "no profile"}

        outdoor = None
        if self._weather is not None:
            try:
                outdoor = self._weather.get_outdoor_temperature()
            except Exception:
                pass
        if outdoor is None:
            return None, {"reason": "no outdoor reading"}

        try:
            from modules.thermal_profile import compute_preheat
            from modules.solar_gain import solar_gain_window

            # Solar gain over the expected preheat window — reduces lead time
            # on sunny mornings. Requires weather service; degrades to no-solar
            # if unavailable or room has no window geometry.
            solar_gain_w: Optional[float] = None
            solar_has_orientation = False
            solar_shortwave_measured = False
            if self._weather is not None:
                try:
                    shortwave = self._weather.get_solar_irradiance()
                    cloud_frac = self._weather.get_cloud_fraction() or 0.0
                    lat = getattr(self._weather, "latitude", None)
                    lon = getattr(self._weather, "longitude", None)
                    if lat is not None and lon is not None:
                        sgw = solar_gain_window(
                            room_config=room,
                            lat=lat,
                            lon=lon,
                            duration_minutes=int(upcoming["minutes_until"]),
                            shortwave_wm2=shortwave,
                            cloud_fraction=cloud_frac,
                        )
                        solar_gain_w = sgw.average_watts
                        solar_has_orientation = sgw.has_orientation_data
                        solar_shortwave_measured = shortwave is not None
                except Exception as sg_err:
                    logger.debug(f"solar_gain_window failed for {room['id']}: {sg_err}")

            est = compute_preheat(
                room_id=room["id"],
                from_temp_c=current_temp,
                to_temp_c=upcoming_target,
                outdoor_temp_c=outdoor,
                w_per_k=profile["w_per_k"],
                tau_seconds=profile.get("tau_seconds"),
                radiator_watts_effective=profile.get("radiator_watts"),
                confidence_in=profile.get("confidence", "low"),
                max_minutes=PREHEAT_LOOKAHEAD_MAX_MIN,
                solar_gain_w=solar_gain_w,
                solar_has_orientation=solar_has_orientation,
                solar_shortwave_measured=solar_shortwave_measured,
            )
        except Exception as e:
            logger.debug(f"compute_preheat failed for {room['id']}: {e}")
            return None, {"reason": "compute failed"}

        info = {
            "upcoming_target": upcoming_target,
            "upcoming_in_minutes": round(upcoming["minutes_until"], 1),
            "minutes_needed": est.minutes_needed,
            "reachable": est.reachable,
            "confidence": est.confidence,
            "preheating": False,
            "solar_gain_w": est.solar_gain_w,
            "minutes_saved_by_solar": est.minutes_saved_by_solar,
            "solar_confidence": est.solar_confidence,
        }

        if not est.reachable or est.minutes_needed is None:
            return None, info

        # Apply safety margin — start a bit earlier than the model thinks
        margin_minutes = est.minutes_needed * PREHEAT_SAFETY_MARGIN
        if margin_minutes >= upcoming["minutes_until"]:
            info["preheating"] = True
            return float(upcoming_target), info

        return None, info

    # ── Receiver control ───────────────────────────────────────────
    async def _apply_receiver(self, circuit: dict, should_call: bool) -> Dict[str, Any]:
        """
        Control the receiver based on circuit config.

        receiver_command modes (from config):
          - "thermostat" : sends system_mode heat/off via 0x0201 handler
          - "switch"     : sends on/off via OnOff cluster (relay-type receivers)

        Idempotent — only sends if command differs from last sent.

        Hive receiver special case
        --------------------------
        For SLR1c/SLR1b ("Hive receivers"), the HVAC handler's
        set_target_temperature(temp) issues an atomic 4-attribute write:
            system_mode = heat
            temp_setpoint_hold = 1
            temp_setpoint_hold_duration = 0xFFFF
            occupied_heating_setpoint = temp
        """
        ieee = circuit.get("receiver_ieee")
        if not ieee:
            return {"sent": False, "reason": "no receiver configured"}

        mode = str(circuit.get("receiver_command", "thermostat")).lower()
        # Detect Hive-style receivers from the device model. The HVAC
        # handler uses the same "SLR" / "RECEIVER" check internally; we
        # mirror it here so the controller and the handler stay aligned
        # on which write protocol is in play.
        is_hive_receiver = False
        try:
            devs = self._snapshot_devices()
            dev = devs.get(ieee)
            if dev is not None:
                # device may be a wrapper or a dict, depending on caller
                model_str = (
                        getattr(dev, "model", None)
                        or (dev.get("model") if isinstance(dev, dict) else None)
                        or ""
                )
                model_upper = str(model_str).upper()
                is_hive_receiver = "SLR" in model_upper or "RECEIVER" in model_upper
        except Exception as e:
            logger.debug(f"hive-receiver detection failed for {ieee}: {e}")

        if mode == "thermostat":
            target_command = "system_mode"
            target_value = "heat" if should_call else "off"
            display = f"system_mode → {target_value}"
            # When calling for heat, also push a high setpoint to guarantee
            # the receiver's internal comparator fires the boiler. When
            # standing down, push a low one so the receiver doesn't fight us.
            # Config override: circuit.receiver_call_setpoint / _idle_setpoint
            call_sp = float(circuit.get("receiver_call_setpoint", 30.0))
            idle_sp = float(circuit.get("receiver_idle_setpoint", 7.0))
            target_setpoint = call_sp if should_call else idle_sp
        else:
            target_command = "on" if should_call else "off"
            target_value = None
            display = target_command

        last = self._last_command.get(ieee)
        if last and last[0] == target_command and last[1] == target_value:
            return {"sent": False, "reason": "unchanged", "command": display}

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would send receiver '{circuit['name']}' ({ieee}) → {display}")
            self._last_command[ieee] = (target_command, target_value, time.time())
            return {"sent": True, "command": display, "dry_run": True}

        # For a Hive receiver calling for heat
        skip_mode_send = (
                mode == "thermostat"
                and should_call
                and is_hive_receiver
        )

        try:
            # 1) Push setpoint first (only in thermostat mode)
            if mode == "thermostat":
                last_sp = self._last_command.get(f"{ieee}:setpoint")
                if not last_sp or last_sp[0] != target_setpoint:
                    try:
                        await self._throttled_send(
                            ieee, "temperature", target_setpoint,
                            endpoint_id=circuit.get("receiver_endpoint"),
                        )
                    except TypeError:
                        await self._throttled_send(ieee, "temperature", target_setpoint)
                    self._last_command[f"{ieee}:setpoint"] = (target_setpoint, time.time())
                    logger.info(
                        f"Receiver '{circuit['name']}' setpoint → {target_setpoint}°C"
                        + (" (atomic mode+hold included)" if skip_mode_send else "")
                    )
            # 2) Then push mode / on-off — unless the atomic setpoint write
            # already handled it (Hive call-for-heat case).
            if skip_mode_send:
                # Mark the mode as if we'd sent it, so the idempotent gate
                # at the top of the next tick reflects reality.
                self._last_command[ieee] = (target_command, target_value, time.time())
                logger.info(
                    f"Receiver '{circuit['name']}' ({ieee}) → "
                    f"{display} (sent atomically with setpoint)"
                )
                return {
                    "sent": True,
                    "command": display,
                    "setpoint": target_setpoint,
                    "atomic": True,
                }
            await self._throttled_send(ieee, target_command, target_value,
                                       endpoint_id=circuit.get("receiver_endpoint"))
            self._last_command[ieee] = (target_command, target_value, time.time())
            logger.info(f"Receiver '{circuit['name']}' ({ieee}) → {display}")
            return {
                "sent": True,
                "command": display,
                "setpoint": target_setpoint if mode == "thermostat" else None,
            }
        except TypeError:
            try:
                await self._throttled_send(ieee, target_command, target_value)
                self._last_command[ieee] = (target_command, target_value, time.time())
                return {"sent": True, "command": display}
            except Exception as e:
                logger.error(f"Receiver command failed ({display}): {e}")
                return {"sent": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Receiver command failed ({display}): {e}")
            return {"sent": False, "error": str(e)}

    # ── TRV setpoint control ───────────────────────────────────────
    async def _apply_trvs(self, room: dict, decision: RoomDecision,
                          circuit_calling: bool) -> List[Dict]:
        """
        Decide and apply TRV setpoints per room status:
          - cold:     setpoint = target  (open via own thermostat)
          - ontarget: setpoint = target  (idle)
          - hot:      setpoint = current - FORCE_CLOSE_OFFSET (force-close)
                      ONLY IF circuit_calling — otherwise harmless to leave open
        """
        actions = []
        target = decision.target_temp
        if target is None:
            return actions

        # When forcing closed we use the *room* temperature (external if present);
        # that's more defensible than the TRV's own hot-pipe reading.
        room_temp = decision.current_temp

        for trv in decision.trvs:
            ieee = trv["ieee"]
            current_temp = trv.get("current_temp")
            current_sp = trv.get("current_setpoint")

            # Decide intended setpoint.
            #
            # Room "hot" → force-close the valve by writing a setpoint comfortably
            # below the current room temperature. We do this whether or not the
            # circuit is currently calling — pre-emptive close so the very next
            # time the circuit fires, this valve is already shut.
            #
            # The intended setpoint is floored at MIN_TRV_SETPOINT (5°C for Aqara
            # E1; configurable per-TRV in config.yaml).
            TRV_MIN_SETPOINT = float(trv.get("min_setpoint", 5.0))
            if decision.status in ("hot", "contact_open"):
                reference = room_temp if room_temp is not None else current_temp
                if reference is None:
                    intended = round(target, 1)
                    action = "track_target"
                else:
                    # Use whichever is lower: target-margin or room-margin.
                    # Both well below current_temp so the valve definitely closes.
                    offset = FORCE_CLOSE_OFFSET
                    by_room = reference - offset
                    by_target = target - offset
                    intended = round(max(TRV_MIN_SETPOINT, min(by_room, by_target)), 1)
                    action = "force_close"
            else:
                intended = round(max(TRV_MIN_SETPOINT, target), 1)
                action = "track_target"

            # Skip if not online
            if not trv.get("online"):
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "offline",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Skip if already close enough
            if current_sp is not None and abs(current_sp - intended) < MIN_SETPOINT_DELTA:
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "already_set",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Cooldown check
            last = self._last_command.get(ieee)
            now = time.time()
            if last and last[0] == "temperature" and last[1] == intended \
                    and (now - last[2]) < COMMAND_COOLDOWN_SEC:
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "cooldown",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Send command
            if self.dry_run:
                logger.info(
                    f"[DRY-RUN] Would set TRV {trv['name']} ({ieee}) → "
                    f"{intended}°C ({action}, room {decision.status}, "
                    f"src={decision.temp_source})"
                )
                self._last_command[ieee] = ("temperature", intended, now)
                actions.append({
                    "ieee": ieee, "action": action, "sent": True, "dry_run": True,
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            try:
                await self._throttled_send(ieee, "temperature", intended)
                self._last_command[ieee] = ("temperature", intended, now)
                logger.info(
                    f"TRV {trv['name']} ({ieee}) → {intended}°C "
                    f"({action}, room {decision.status}, src={decision.temp_source})"
                )
                actions.append({
                    "ieee": ieee, "action": action, "sent": True,
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
            except Exception as e:
                logger.error(f"TRV command failed for {ieee}: {e}")
                actions.append({
                    "ieee": ieee, "action": action, "sent": False, "error": str(e),
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })

        return actions

    # ── Per-TRV persistent config application ──────────────────────
    async def _apply_all_trv_config(self):
        """Apply window_detection / child_lock / valve_detection for every configured TRV."""
        for c in self.circuits:
            for r in c["rooms"]:
                for t in r["trvs"]:
                    if t["ieee"] in self._trv_config_applied:
                        continue
                    await self.apply_trv_config(t["ieee"])

    async def apply_trv_config(self, ieee: str) -> Dict[str, Any]:
        """
        Apply persistent Aqara-cluster settings for a single TRV.
        Reads current device state first; skips writes where value already matches.
        Safe to call repeatedly.
        """
        loc = self.find_trv(ieee)
        if not loc:
            return {"success": False, "error": "TRV not in controller config"}
        _, room, trv = loc

        results: Dict[str, Any] = {"ieee": ieee, "sent": {}, "skipped": {}, "failed": {}}

        # Try to read current device state first so we can skip no-op writes.
        # Falls back to state cache if direct read isn't possible (sleepy device).
        dev = (self._get_devices() or {}).get(ieee)
        current_state = getattr(dev, "state", {}) if dev else {}

        # Map: config key → (command name, current state key, expected-type coerce)
        config_attrs = (
            ("window_detection", "window_detection", "window_detection"),
            ("child_lock",       "child_lock",       "child_lock"),
            ("valve_detection",  "valve_detection",  "valve_detection"),
        )

        for cfg_key, command, state_key in config_attrs:
            desired = trv.get(cfg_key)
            if desired is None:
                results["skipped"][cfg_key] = "not configured"
                continue

            current = current_state.get(state_key)
            # State values may be bool, int, or None
            if current is not None and bool(current) == bool(desired):
                results["skipped"][cfg_key] = f"already {bool(desired)}"
                continue

            if self.dry_run:
                logger.info(f"[DRY-RUN] Would set TRV {ieee} {cfg_key} = {desired}")
                results["sent"][cfg_key] = {"value": bool(desired), "dry_run": True}
                continue

            try:
                ok = await self._throttled_send(ieee, command, 1 if desired else 0)
                if ok is False:
                    results["failed"][cfg_key] = "device rejected write"
                else:
                    results["sent"][cfg_key] = {"value": bool(desired)}
            except Exception as e:
                logger.error(f"TRV {ieee} {cfg_key} write failed: {e}")
                results["failed"][cfg_key] = str(e)

        # Sensor type — only write if room is in push mode AND device isn't already external
        if room.get("external_temp_mode") == "push":
            current_sensor = current_state.get("sensor_type")
            # sensor_type: 1 = external, 0 = internal (stored as int or str)
            already_external = (
                    current_sensor in (1, "1", "external", True)
            )
            if already_external:
                results["skipped"]["sensor_type"] = "already external"
            elif self.dry_run:
                logger.info(f"[DRY-RUN] Would set TRV {ieee} sensor_type = external")
                results["sent"]["sensor_type"] = {"value": "external", "dry_run": True}
            else:
                try:
                    ok = await self._throttled_send(ieee, "sensor_type", 1)
                    if ok is False:
                        results["failed"]["sensor_type"] = "device rejected write"
                    else:
                        results["sent"]["sensor_type"] = {"value": "external"}
                except Exception as e:
                    logger.error(f"TRV {ieee} sensor_type write failed: {e}")
                    results["failed"]["sensor_type"] = str(e)

        # Only mark as applied if nothing failed — otherwise retry next time
        if not results["failed"]:
            self._trv_config_applied.add(ieee)
        results["success"] = not results["failed"]
        return results

    async def trigger_calibration(self, ieee: str) -> Dict[str, Any]:
        """
        One-shot: kick off motor calibration on an Aqara TRV.
        Takes ~2 minutes on the device side — status lives in state['motor_calibration'].
        """
        if not self.find_trv(ieee):
            return {"success": False, "error": "TRV not in controller config"}
        if self.dry_run:
            logger.info(f"[DRY-RUN] Would start calibration on TRV {ieee}")
            return {"success": True, "dry_run": True, "ieee": ieee}
        try:
            await self._throttled_send(ieee, "motor_calibration", 1)
            logger.info(f"TRV {ieee}: motor calibration started")
            return {"success": True, "ieee": ieee}
        except Exception as e:
            logger.error(f"TRV {ieee} calibration failed: {e}")
            return {"success": False, "error": str(e)}

    def update_trv_settings(self, ieee: str, updates: Dict[str, Any]) -> bool:
        """
        In-memory update of a single TRV's config. Persistence is the caller's job
        (routes save to config.yaml). Returns True if TRV found and updated.
        """
        loc = self.find_trv(ieee)
        if not loc:
            return False
        _, _, trv = loc
        for k in ("window_detection", "child_lock", "valve_detection"):
            if k in updates:
                trv[k] = _as_bool(updates[k], trv.get(k))
        # Force re-apply on next sweep
        self._trv_config_applied.discard(ieee)
        return True

    # ── External temperature push ──────────────────────────────────
    async def _push_external_temps_once(self):
        """
        For every room in push mode, read the sensor's current temperature and
        forward it to each Aqara TRV in the room. Skips pushes that are
        redundant (same reading within EXT_TEMP_PUSH_MIN_DELTA of last push).
        """
        devices = self._snapshot_devices()
        now_ts = time.time()

        for c in self.circuits:
            for room in c["rooms"]:
                if room.get("external_temp_mode") != "push":
                    continue
                sensor_ieee = room.get("temperature_sensor_ieee")
                if not sensor_ieee:
                    continue
                sensor_dev = devices.get(sensor_ieee)
                if sensor_dev is None:
                    logger.debug(f"Room {room['id']}: sensor {sensor_ieee} not in device registry")
                    continue
                sensor_temp = _pick_temperature(_device_state(sensor_dev))
                if sensor_temp is None:
                    logger.debug(f"Room {room['id']}: sensor {sensor_ieee} has no temperature")
                    continue

                interval = room.get("external_temp_push_interval_sec",
                                    DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)

                for trv in room["trvs"]:
                    ieee = trv["ieee"]
                    last = self._last_ext_push.get(ieee)
                    if last is not None:
                        last_temp, last_ts = last
                        fresh_enough = (now_ts - last_ts) < interval
                        tiny_change = abs(sensor_temp - last_temp) < EXT_TEMP_PUSH_MIN_DELTA
                        if fresh_enough and tiny_change:
                            continue

                    if self.dry_run:
                        logger.info(
                            f"[DRY-RUN] Would push external temp {sensor_temp:.2f}°C "
                            f"→ TRV {ieee} (room {room['id']})"
                        )
                        self._last_ext_push[ieee] = (sensor_temp, now_ts)
                        continue

                    try:
                        resp = await self._throttled_send(ieee, "external_temp", sensor_temp)
                        succeeded = isinstance(resp, dict) and resp.get("success", False)
                        err = resp.get("error", "") if isinstance(resp, dict) else ""

                        if "NCP" in err or "ACK_TIMEOUT" in err:
                            logger.error(
                                "NCP failure detected — aborting heating tick to "
                                "allow radio recovery"
                            )
                            return  # exit _push_external_temps_once entirely

                        if succeeded:
                            self._last_ext_push[ieee] = (sensor_temp, now_ts)
                            logger.info(
                                f"TRV {ieee}: pushed external temp {sensor_temp:.2f}°C "
                                f"(room {room['id']})"
                            )
                        else:
                            err = resp.get("error", "unknown") if isinstance(resp, dict) else "no response"
                            logger.warning(
                                f"TRV {ieee}: external temp push rejected ({err})"
                            )
                            # On NCP failure, abort the rest of this cycle
                            # — radio needs time to recover, further writes will stack failures
                            if "NCP" in err or "ACK_TIMEOUT" in err:
                                logger.warning(
                                    "Aborting remaining external-temp pushes for this cycle "
                                    "— radio needs recovery time"
                                )
                                return
                    except Exception as e:
                        logger.warning(f"TRV {ieee}: external temp push failed: {e}")
                        return

                    # Small yield between TRVs so radio doesn't choke on back-to-back writes
                    await asyncio.sleep(0.5)