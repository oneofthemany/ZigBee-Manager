"""
Thermal profile calculations for rooms.

Two layers:

1. **Static** — heat loss rate (W/K) computed from a room's dimensions plus
   the dwelling-level insulation level. U-values are SAP Appendix S defaults.

2. **Learned** — measured heat loss rate from telemetry. We find cool-down
   intervals (heat off, room temperature monotonically falling toward an
   assumed-constant outdoor temperature) and fit Newton's law of cooling:

        T(t) = T_out + (T_0 - T_out) * exp(-t / tau)

   where tau = (m·c) / UA, tau is the thermal time constant in seconds, and
   UA is the heat loss coefficient (W/K). From tau we can derive UA if we
   know the room's effective thermal mass — which we don't exactly, but we
   can approximate it from floor area × ceiling height × air specific heat
   plus a furnishings/fabric factor.

3. **Blended** — if we have enough measured data with a decent fit, weight
   70% measured / 30% static. Otherwise return static only with a
   low-confidence flag.

Everything here is pure functions. No I/O beyond what callers pass in.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.thermal_profile")

# ── Physical constants ────────────────────────────────────────────────
AIR_DENSITY_KG_M3 = 1.2          # at 20 °C
AIR_CP_J_KG_K = 1005             # specific heat of air
# Empirical: rooms have roughly 3x the thermal mass of their air alone once
# furnishings, plasterboard, screed etc. are accounted for. Standard in
# thermal-model literature (see CIBSE TM41).
ROOM_THERMAL_MASS_FACTOR = 3.0

# ── Sensor stratification correction ─────────────────────────────────
# Warm air stratifies upward in heated rooms. A sensor mounted high reads
# warmer than the comfort zone; a sensor near the floor reads cooler. The
# correction below is the standard rule of thumb from CIBSE Guide A:
#   ~0.5 °C per metre above the reference height (a heated room's vertical
#   temperature gradient under typical convective heating).
#
# Reference height = 1.5 m — standing breathing zone, the default mounting
# height for residential thermostats and what target temperatures implicitly
# refer to. A sensor exactly at 1.5 m receives no correction.
#
# The correction is additive on the *delta from reference*:
#   correction_c = -GRADIENT * (sensor_height_m - REFERENCE_HEIGHT_M)
# Subtract from raw reading to get comfort-zone temperature.
#
# We don't gate this on "is heating active" because:
#   (a) average gradient over a heating season is dominated by heated time
#   (b) when heating is off, the gradient self-decays and the correction is
#       small in absolute terms (well under sensor noise)
#   (c) gating would require coupling temperature reads to controller state
STRATIFICATION_REFERENCE_HEIGHT_M = 1.5
STRATIFICATION_GRADIENT_C_PER_M   = 0.5
# Above 5 m or below 0 m we treat the value as configured wrong and skip.
STRATIFICATION_MAX_PLAUSIBLE_HEIGHT_M = 5.0


def stratification_offset_c(
        sensor_height_m: Optional[float],
        reference_height_m: float = STRATIFICATION_REFERENCE_HEIGHT_M,
        gradient_c_per_m: float = STRATIFICATION_GRADIENT_C_PER_M,
) -> float:
    """
    Return the additive offset to apply to a raw reading to get the
    comfort-zone temperature.

    > corrected = raw + stratification_offset_c(height)

    Examples (with default reference 1.5 m, gradient 0.5 °C/m):
        height 1.5 → 0.0   (no correction)
        height 2.2 → -0.35 (sensor reads 0.35 °C high)
        height 0.5 → +0.50 (sensor reads 0.5 °C low)

    Returns 0 when height is unknown/None or implausible — fail-safe to
    "no correction" rather than a guess.
    """
    if sensor_height_m is None:
        return 0.0
    try:
        h = float(sensor_height_m)
    except (TypeError, ValueError):
        return 0.0
    if h < 0.0 or h > STRATIFICATION_MAX_PLAUSIBLE_HEIGHT_M:
        return 0.0
    return -gradient_c_per_m * (h - reference_height_m)


def correct_sensor_reading(
        raw_c: Optional[float],
        sensor_height_m: Optional[float],
        reference_height_m: float = STRATIFICATION_REFERENCE_HEIGHT_M,
        gradient_c_per_m: float = STRATIFICATION_GRADIENT_C_PER_M,
) -> Optional[float]:
    """
    Convenience: apply ``stratification_offset_c`` to a reading. Returns the
    raw value unchanged when it's ``None``, the height is unknown, or the
    height is implausible. Rounded to 0.1 °C to avoid spurious precision.
    """
    if raw_c is None:
        return None
    offset = stratification_offset_c(sensor_height_m, reference_height_m, gradient_c_per_m)
    if offset == 0.0:
        return raw_c
    return round(raw_c + offset, 1)

# ── U-value tables (W / m² / K) ───────────────────────────────────────
# Keyed by insulation level. Values from SAP Appendix S + CIBSE Guide A.
# The "party_wall_u" is 0 for heated-neighbour party walls (the normal
# assumption for terraces/flats); an isolated unheated void would be ~0.5.
U_VALUES = {
    "none": {
        "wall_ext":    2.10,
        "wall_party":  0.00,
        "wall_int":    0.00,   # internal to heated spaces — zero loss
        "window":      {"single": 4.80, "double": 2.80, "triple": 1.80},
        "door_ext":    3.00,
        "door_int":    0.00,
        "floor":       {
            "solid": 0.70, "suspended": 0.70,
            "carpet_over_concrete": 0.55, "tile_over_concrete": 0.70,
            "wooden": 0.55, "carpet_over_wooden": 0.45,
            "unknown": 0.70,
        },
        "ceiling":     {"insulated": 0.35, "uninsulated": 2.30, "flat_roof": 1.50, "unknown": 1.50},
    },
    "partial": {
        "wall_ext":    1.20,
        "wall_party":  0.00,
        "wall_int":    0.00,
        "window":      {"single": 4.80, "double": 2.30, "triple": 1.50},
        "door_ext":    2.40,
        "door_int":    0.00,
        "floor": {
            "solid": 0.50, "suspended": 0.50,
            "carpet_over_concrete": 0.40, "tile_over_concrete": 0.50,
            "wooden": 0.40, "carpet_over_wooden": 0.30,
            "unknown": 0.50,
        },
        "ceiling":     {"insulated": 0.30, "uninsulated": 1.80, "flat_roof": 1.20, "unknown": 1.00},
    },
    "full": {
        "wall_ext":    0.30,
        "wall_party":  0.00,
        "wall_int":    0.00,
        "window":      {"single": 4.80, "double": 1.60, "triple": 0.90},
        "door_ext":    1.50,
        "door_int":    0.00,
        "floor": {
            "solid": 0.25, "suspended": 0.25,
            "carpet_over_concrete": 0.20, "tile_over_concrete": 0.25,
            "wooden": 0.20, "carpet_over_wooden": 0.18,
            "unknown": 0.25,
        },
        "ceiling":     {"insulated": 0.18, "uninsulated": 1.50, "flat_roof": 0.80, "unknown": 0.35},
    },
    "cavity_wall": {
        # Cavity-filled walls, otherwise default-era glazing & loft
        "wall_ext":    0.60,
        "wall_party":  0.00,
        "wall_int":    0.00,
        "window":      {"single": 4.80, "double": 2.30, "triple": 1.50},
        "door_ext":    2.40,
        "door_int":    0.00,
        "floor": {
            "solid": 0.45, "suspended": 0.45,
            "carpet_over_concrete": 0.35, "tile_over_concrete": 0.45,
            "wooden": 0.35, "carpet_over_wooden": 0.28,
            "unknown": 0.45,
        },
        "ceiling":     {"insulated": 0.25, "uninsulated": 1.80, "flat_roof": 1.00, "unknown": 0.50},
    },
}

# Air changes per hour — infiltration/ventilation loss
ACH_BY_INSULATION = {
    "none": 1.5, "partial": 1.0, "full": 0.6, "cavity_wall": 0.8,
}


@dataclass
class StaticBreakdown:
    """Per-element heat loss contribution (W/K)."""
    walls_external: float = 0.0
    walls_party: float = 0.0
    windows: float = 0.0
    doors: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    ventilation: float = 0.0

    @property
    def total(self) -> float:
        return (self.walls_external + self.walls_party + self.windows
                + self.doors + self.floor + self.ceiling + self.ventilation)


@dataclass
class ThermalProfile:
    """Full per-room thermal profile."""
    room_id: str
    static_w_per_k: Optional[float] = None
    static_breakdown: Optional[StaticBreakdown] = None
    measured_w_per_k: Optional[float] = None
    measured_confidence: float = 0.0      # 0–1
    measured_sample_count: int = 0
    measured_r2: Optional[float] = None
    tau_seconds: Optional[float] = None
    blended_w_per_k: Optional[float] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "static_w_per_k": self.static_w_per_k,
            "static_breakdown": self.static_breakdown.__dict__ if self.static_breakdown else None,
            "measured_w_per_k": self.measured_w_per_k,
            "measured_confidence": round(self.measured_confidence, 2),
            "measured_sample_count": self.measured_sample_count,
            "measured_r2": self.measured_r2,
            "tau_seconds": self.tau_seconds,
            "blended_w_per_k": self.blended_w_per_k,
            "warnings": self.warnings,
        }


# ──────────────────────────────────────────────────────────────────────
# STATIC CALCULATION
# ──────────────────────────────────────────────────────────────────────

def compute_static(
        dimensions: Dict[str, Any],
        insulation: str = "partial",
        floor_plan: Optional[Dict[str, Any]] = None,
        floor_plan_ref: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], StaticBreakdown, List[str]]:
    """
    Per-wall heat loss. Two code paths share one result shape:

    * **Plan-aware path** (preferred when ``floor_plan`` + ``floor_plan_ref``
      are supplied): uses true polygon geometry. Each polygon edge becomes
      one wall; each opening uses its host wall's actual type, not a folded
      bin. Gives correct results for L-shaped rooms, rooms with more than 4
      walls, and walls of unequal length.

    * **Legacy bbox path** (when no plan is supplied, or the room has no
      ``floor_plan_ref``): the existing X × depth × H approximation, folded
      into 4 walls (front/back/left/right). Kept verbatim for manual-mode
      rooms — same behaviour as before this patch.

    The plan-aware path falls back to the bbox path automatically if the
    plan lookup fails (missing level/room, degenerate polygon, etc.) and
    appends a warning so the user can see why precision dropped.
    """
    warnings: List[str] = []
    bd = StaticBreakdown()
    if not isinstance(dimensions, dict):
        return None, bd, ["no dimensions supplied"]

    u_table = U_VALUES.get(insulation) or U_VALUES["partial"]

    # ── Try the plan-aware path first when possible ────────────────────
    plan_geom = None
    if isinstance(floor_plan, dict) and isinstance(floor_plan_ref, dict):
        lvl_id = floor_plan_ref.get("level_id")
        rm_id = floor_plan_ref.get("room_id")
        if lvl_id and rm_id:
            try:
                from modules.floor_plan import per_wall_breakdown_from_plan
                plan_geom = per_wall_breakdown_from_plan(floor_plan, lvl_id, rm_id)
                if plan_geom is None:
                    warnings.append(
                        f"floor_plan_ref present but room {rm_id!r} not found "
                        f"in level {lvl_id!r}; falling back to legacy dimensions"
                    )
            except Exception as e:
                warnings.append(f"plan geometry lookup failed: {e}; using legacy dimensions")
                plan_geom = None

    if plan_geom is not None:
        return _compute_static_from_plan(plan_geom, u_table, insulation, warnings, bd)

    # ── Legacy bbox path (unchanged behaviour) ─────────────────────────
    x_m = float(dimensions.get("width_m") or 0.0)
    y_m = float(dimensions.get("depth_m") or 0.0)
    h_m = float(dimensions.get("ceiling_height_m") or 2.4)
    floor_area = float(dimensions.get("floor_area_m2") or (x_m * y_m))

    walls = dimensions.get("walls") or {}
    # Gross area per wall
    gross = {
        "front": x_m * h_m if x_m else 0.0,
        "back":  x_m * h_m if x_m else 0.0,
        "left":  y_m * h_m if y_m else 0.0,
        "right": y_m * h_m if y_m else 0.0,
    }

    # Subtract per-wall windows/doors to get net area per wall
    windows = dimensions.get("windows") or []
    doors = dimensions.get("doors") or []
    per_wall_opening_area = {w: 0.0 for w in gross.keys()}
    for item in windows + doors:
        wall = item.get("wall")
        if wall in per_wall_opening_area:
            per_wall_opening_area[wall] += float(item.get("area_m2") or 0.0)

    # Fold the per-wall U-values into the walls_external / walls_party buckets.
    for w_name, w_def in walls.items():
        if w_name not in gross:
            continue
        gross_a = gross[w_name]
        opening_a = per_wall_opening_area.get(w_name, 0.0)
        net_a = max(0.0, gross_a - opening_a)
        if gross_a > 0 and net_a == 0:
            warnings.append(f"openings exceed wall area on {w_name} wall")
        w_type = w_def.get("type", "external") if isinstance(w_def, dict) else "external"
        if w_type == "external":
            bd.walls_external += net_a * u_table["wall_ext"]
        elif w_type == "party":
            bd.walls_party += net_a * u_table["wall_party"]
        # internal / unknown → no loss (or treated as internal)

    # Windows
    for w in windows:
        area = float(w.get("area_m2") or 0.0)
        # Only count loss on windows attributed to external walls
        wall = w.get("wall")
        wall_kind = (walls.get(wall) or {}).get("type", "external") if wall else "external"
        if wall_kind != "external":
            continue
        glazing = str(w.get("glazing", "double")).lower()
        u = u_table["window"].get(glazing, u_table["window"]["double"])
        bd.windows += area * u

    # Doors
    for dr in doors:
        area = float(dr.get("area_m2") or 0.0)
        if dr.get("type") == "external":
            bd.doors += area * u_table["door_ext"]

    # Floor / ceiling
    floor_type = str(dimensions.get("floor_type", "unknown")).lower()
    bd.floor = floor_area * u_table["floor"].get(floor_type, u_table["floor"]["unknown"])
    ceiling_type = str(dimensions.get("ceiling_type", "unknown")).lower()
    bd.ceiling = floor_area * u_table["ceiling"].get(ceiling_type, u_table["ceiling"]["unknown"])

    # Ventilation
    volume_m3 = floor_area * h_m
    ach = ACH_BY_INSULATION.get(insulation, 1.0)
    bd.ventilation = AIR_DENSITY_KG_M3 * AIR_CP_J_KG_K * volume_m3 * (ach / 3600.0)

    total = bd.total
    if total <= 0:
        warnings.append("static calculation produced zero — check dimensions")
        return None, bd, warnings
    return round(total, 1), bd, warnings

def _compute_static_from_plan(
        plan_geom: Dict[str, Any],
        u_table: Dict[str, Any],
        insulation: str,
        warnings: List[str],
        bd: StaticBreakdown,
) -> Tuple[Optional[float], StaticBreakdown, List[str]]:
    """
    Plan-aware static heat loss. Same StaticBreakdown buckets as the legacy
    path so downstream code (sizing UI, diagnostics, reports) sees an
    identical result shape — only the *numbers* improve.

    Key behavioural differences vs the bbox path:
      * Each polygon wall contributes its true length, not a folded bin
      * Wall type comes from auto-inference (1 room = external, 2 = party,
        with explicit overrides honoured) — see ``infer_wall_type`` in
        floor_plan.py
      * Openings deduct from their host wall's *actual* area, even when
        the room has more than 4 walls or unequal wall lengths
      * Each opening's external/internal classification uses its host
        wall's type, not the folded bin
    """
    floor_area = float(plan_geom.get("floor_area_m2") or 0.0)
    h_m = float(plan_geom.get("ceiling_height_m") or 2.4)

    # ── Walls ───────────────────────────────────────────────────────────
    for w in plan_geom.get("walls", []):
        length = float(w.get("length_m") or 0.0)
        height = float(w.get("height_m") or h_m)
        gross_a = length * height
        if gross_a <= 0:
            continue
        opening_a = float(w.get("openings_area_m2") or 0.0)
        if opening_a > gross_a:
            warnings.append(
                f"openings ({opening_a:.2f} m²) exceed wall area "
                f"({gross_a:.2f} m²) on a {w.get('compass','?')}-facing wall"
            )
            opening_a = gross_a
        net_a = max(0.0, gross_a - opening_a)
        wtype = w.get("type", "external")
        if wtype == "external":
            bd.walls_external += net_a * u_table["wall_ext"]
        elif wtype == "party":
            bd.walls_party += net_a * u_table["wall_party"]
        # internal / unknown → loss-free (matches legacy treatment)

    # ── Windows ─────────────────────────────────────────────────────────
    # Only windows whose host wall is external contribute. The plan-aware
    # path classified this per-opening; the bbox path would have folded the
    # wall first and then asked "is the *bin* external?", which can be
    # wrong for L-shaped rooms where two edges fall in the same bin.
    for w in plan_geom.get("windows", []):
        if not w.get("on_external"):
            continue
        area = float(w.get("area_m2") or 0.0)
        glazing = str(w.get("glazing", "double")).lower()
        u = u_table["window"].get(glazing, u_table["window"]["double"])
        bd.windows += area * u

    # ── Doors ───────────────────────────────────────────────────────────
    for dr in plan_geom.get("doors", []):
        if not dr.get("on_external"):
            continue
        if dr.get("type") != "external":
            continue
        area = float(dr.get("area_m2") or 0.0)
        bd.doors += area * u_table["door_ext"]

    # ── Floor / ceiling (same as legacy path) ───────────────────────────
    floor_type = str(plan_geom.get("floor_type") or "unknown").lower()
    bd.floor = floor_area * u_table["floor"].get(
        floor_type, u_table["floor"]["unknown"]
    )
    ceiling_type = str(plan_geom.get("ceiling_type") or "unknown").lower()
    bd.ceiling = floor_area * u_table["ceiling"].get(
        ceiling_type, u_table["ceiling"]["unknown"]
    )

    # ── Ventilation (same formula as legacy) ────────────────────────────
    volume_m3 = floor_area * h_m
    ach = ACH_BY_INSULATION.get(insulation, 1.0)
    bd.ventilation = AIR_DENSITY_KG_M3 * AIR_CP_J_KG_K * volume_m3 * (ach / 3600.0)

    total = bd.total
    if total <= 0:
        warnings.append("plan-aware static calculation produced zero — check polygon geometry")
        return None, bd, warnings
    return round(total, 1), bd, warnings

# ──────────────────────────────────────────────────────────────────────
# LEARNED (FROM TELEMETRY)
# ──────────────────────────────────────────────────────────────────────


# ── Cool-down window thresholds ───────────────────────────────────────
# Two profiles:
#   LEARN_* — used when fitting baseline τ from the long telemetry window.
#     Loose, so we accept more candidate windows and let the R² filter in
#     _fit_newton_cooling cull the noisy ones. Rooms that are held near
#     setpoint most of the time still produce enough usable drifts.
#   ALERT_* — used by the anomaly detector (detect_fast_cooling) where we
#     compare a single recent window to the baseline. Kept stricter to
#     avoid false "window open" alarms from small natural drifts.
LEARN_MIN_DURATION_SEC = 20 * 60
LEARN_MIN_DROP_C       = 0.3
LEARN_MAX_DURATION_SEC = 6 * 3600

@dataclass
class CoolDownWindow:
    """One identified cool-down interval."""
    start_ts: float
    end_ts: float
    start_temp: float
    end_temp: float
    outdoor_temp: float
    tau_seconds: float       # fitted time constant
    r2: float                # fit quality
    sample_count: int


def _find_cooldown_windows(
        temperature_series: List[Dict[str, Any]],
        outdoor_getter,
        min_duration_sec: int = 30 * 60,
        max_duration_sec: int = 6 * 3600,
        min_drop_c: float = 0.5,
        heating_state_getter=None,          # callable(ts_sec) -> bool or None
        heating_active_tolerance: float = 0.1,   # allow up to 10% tainted samples
) -> List[Tuple[float, float, List[Tuple[float, float]]]]:
    """
    Identify continuous intervals where temperature is monotonically falling
    (within noise tolerance). Returns list of (start_ts, end_ts, samples).

    samples: list of (elapsed_seconds_from_start, temp_c)

    temperature_series entries look like: {"ts": datetime, "numeric_val": 20.5}
    """
    import datetime as _dt

    if not temperature_series or len(temperature_series) < 4:
        return []

    pts = []
    for p in temperature_series:
        ts = p.get("ts")
        v = p.get("numeric_val")
        if v is None:
            try:
                v = float(p.get("value"))
            except (TypeError, ValueError):
                continue
        if ts is None:
            continue
        if isinstance(ts, _dt.datetime):
            tss = ts.timestamp()
        else:
            continue  # unexpected shape
        pts.append((tss, float(v)))

    if len(pts) < 4:
        return []

    pts.sort(key=lambda x: x[0])

    windows: List[Tuple[float, float, List[Tuple[float, float]]]] = []
    cur_start_i = 0

    def _commit(i0: int, i1: int):
        if i1 - i0 < 3:
            return
        t0 = pts[i0][0]
        t1 = pts[i1][0]
        if t1 - t0 < min_duration_sec or t1 - t0 > max_duration_sec:
            return
        temp_drop = pts[i0][1] - pts[i1][1]
        if temp_drop < min_drop_c:
            return
        # Heating-state gate: reject windows where too many samples
        # overlap a period when heating was active. A tolerance > 0
        # covers transient TRV cycling at the window boundaries.
        if heating_state_getter is not None:
            tainted = 0
            total = i1 - i0 + 1
            for p in pts[i0:i1 + 1]:
                try:
                    if heating_state_getter(p[0]):
                        tainted += 1
                except Exception:
                    # If the gate raises, fail closed — reject the window.
                    return
            if total == 0 or (tainted / total) > heating_active_tolerance:
                return
        samples = [(p[0] - t0, p[1]) for p in pts[i0:i1 + 1]]
        windows.append((t0, t1, samples))

    NOISE = 0.1  # °C — allow tiny upticks without breaking a cool-down run
    for i in range(1, len(pts)):
        prev_temp = pts[i - 1][1]
        cur_temp = pts[i][1]
        if cur_temp > prev_temp + NOISE:
            # Temperature rose significantly → boundary
            _commit(cur_start_i, i - 1)
            cur_start_i = i
    _commit(cur_start_i, len(pts) - 1)

    return windows


def _fit_newton_cooling(samples: List[Tuple[float, float]], outdoor_temp: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Fit T(t) = T_out + (T_0 - T_out) * exp(-t / tau)
    by linear regression on ln((T - T_out) / (T_0 - T_out)) = -t / tau.

    Returns (tau_seconds, r_squared). None on failure.
    """
    if len(samples) < 3:
        return None, None
    t0_abs, T0 = samples[0]
    if T0 <= outdoor_temp + 0.5:
        return None, None   # nothing to cool toward

    xs, ys = [], []
    for t, T in samples:
        delta = T - outdoor_temp
        if delta <= 0.1:
            continue  # reached equilibrium
        ratio = delta / (T0 - outdoor_temp)
        if ratio <= 0 or ratio > 1.0:
            continue
        xs.append(float(t))
        ys.append(math.log(ratio))

    if len(xs) < 3:
        return None, None

    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None, None
    slope = num / den   # = -1/tau
    if slope >= 0:
        return None, None   # not actually cooling

    tau = -1.0 / slope

    # R²
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    if tau < 60 or tau > 48 * 3600:
        return None, None  # unrealistic
    return tau, r2


def compute_measured(
        temperature_series: List[Dict[str, Any]],
        outdoor_temp_getter,
        floor_area_m2: float,
        ceiling_height_m: float = 2.4,
        min_duration_sec: int = LEARN_MIN_DURATION_SEC,
        min_drop_c: float = LEARN_MIN_DROP_C,
        max_duration_sec: int = LEARN_MAX_DURATION_SEC,
        heating_state_getter=None,
) -> Tuple[Optional[float], float, int, Optional[float], Optional[float]]:
    """
    Measure W/K from cool-down history.

    Returns (w_per_k, confidence_0_to_1, sample_count, r2_best, tau_median_s)
    """
    if not temperature_series or len(temperature_series) < 10:
        return None, 0.0, 0, None, None
    if not floor_area_m2 or floor_area_m2 <= 0:
        return None, 0.0, 0, None, None

    windows = _find_cooldown_windows(
        temperature_series,
        outdoor_temp_getter,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        min_drop_c=min_drop_c,
        heating_state_getter=heating_state_getter,
    )
    if not windows:
        return None, 0.0, 0, None, None

    windows = _find_cooldown_windows(temperature_series, outdoor_temp_getter)
    if not windows:
        return None, 0.0, 0, None, None

    taus, r2s = [], []
    for (t0, t1, samples) in windows:
        mid_ts = (t0 + t1) / 2
        outdoor = outdoor_temp_getter(mid_ts)
        if outdoor is None:
            # Fall back: assume 10 °C (conservative UK average); low confidence
            outdoor = 10.0
        tau, r2 = _fit_newton_cooling(samples, outdoor)
        if tau is None:
            continue
        if r2 is not None and r2 < 0.5:
            continue
        taus.append(tau)
        r2s.append(r2 or 0.0)

    if not taus:
        return None, 0.0, 0, None, None

    # Use median tau (robust to outliers)
    taus_sorted = sorted(taus)
    tau_median = taus_sorted[len(taus_sorted) // 2]

    # Derive UA from tau and thermal mass
    volume_m3 = floor_area_m2 * ceiling_height_m
    m_air = AIR_DENSITY_KG_M3 * volume_m3
    mc = m_air * AIR_CP_J_KG_K * ROOM_THERMAL_MASS_FACTOR    # J/K
    ua = mc / tau_median                                      # W/K

    # Confidence: scale with sample count (saturates at 10) and mean R²
    mean_r2 = sum(r2s) / len(r2s) if r2s else 0.0
    sample_factor = min(1.0, len(taus) / 10.0)
    confidence = round(sample_factor * max(0.0, mean_r2), 2)

    return round(ua, 1), confidence, len(taus), max(r2s), tau_median


# ──────────────────────────────────────────────────────────────────────
# BLEND
# ──────────────────────────────────────────────────────────────────────

def compute_profile(
        room_id: str,
        dimensions: Optional[Dict[str, Any]],
        insulation: str,
        temperature_series: Optional[List[Dict[str, Any]]] = None,
        outdoor_temp_getter=None,
        blend_weight_measured: float = 0.7,
        heating_state_getter=None,
        floor_plan: Optional[Dict[str, Any]] = None,
        floor_plan_ref: Optional[Dict[str, Any]] = None,
) -> ThermalProfile:
    """
    Top-level orchestrator. Static + learned + blend.

    When ``floor_plan`` + ``floor_plan_ref`` are supplied, ``compute_static``
    uses the room's real polygon geometry instead of the bbox approximation.
    The measured (telemetry-based) leg of the calculation is unchanged: it
    only needs floor area and ceiling height, both of which come out of
    ``dimensions`` either way (plan-projected or manual).
    """
    prof = ThermalProfile(room_id=room_id)

    if not dimensions:
        prof.warnings.append("no dimensions configured — skipping thermal profile")
        return prof

    floor_area = float(dimensions.get("floor_area_m2") or 0.0)
    ceiling_h = float(dimensions.get("ceiling_height_m") or 2.4)

    # Static — plan-aware when references are supplied; otherwise bbox
    static_total, bd, static_warnings = compute_static(
        dimensions, insulation,
        floor_plan=floor_plan,
        floor_plan_ref=floor_plan_ref,
    )
    prof.static_w_per_k = static_total
    prof.static_breakdown = bd
    prof.warnings.extend(static_warnings)

    # Measured
    if temperature_series and outdoor_temp_getter and floor_area > 0:
        m_ua, conf, n, r2, tau = compute_measured(
            temperature_series, outdoor_temp_getter, floor_area, ceiling_h,
            heating_state_getter=heating_state_getter,
        )
        prof.measured_w_per_k = m_ua
        prof.measured_confidence = conf
        prof.measured_sample_count = n
        prof.measured_r2 = r2
        prof.tau_seconds = tau

    # Blend
    if prof.static_w_per_k is None and prof.measured_w_per_k is None:
        prof.blended_w_per_k = None
    elif prof.measured_w_per_k is None or prof.measured_confidence < 0.3:
        prof.blended_w_per_k = prof.static_w_per_k
        if prof.measured_w_per_k is not None:
            prof.warnings.append(
                f"measured fit too low-confidence ({prof.measured_confidence}); using static only"
            )
    elif prof.static_w_per_k is None:
        prof.blended_w_per_k = prof.measured_w_per_k
    else:
        w = min(1.0, blend_weight_measured * prof.measured_confidence * (1.0 / 0.7))
        prof.blended_w_per_k = round(
            w * prof.measured_w_per_k + (1 - w) * prof.static_w_per_k, 1
        )

    return prof


# ──────────────────────────────────────────────────────────────────────
# PRE-HEAT TIME PREDICTION
# ──────────────────────────────────────────────────────────────────────

from dataclasses import dataclass as _dc


@_dc
class PreheatEstimate:
    room_id: str
    from_temp_c: float
    to_temp_c: float
    outdoor_temp_c: float
    tau_seconds: Optional[float]
    radiator_watts_effective: Optional[float]
    w_per_k: Optional[float]
    steady_state_temp_c: Optional[float]   # where the room would plateau
    minutes_needed: Optional[float]        # None if unreachable / no data
    reachable: bool
    confidence: str                         # "high" | "medium" | "low" | "none"
    warnings: List[str] = field(default_factory=list)

    # ── Solar gain fields (populated when solar data is available) ────
    solar_gain_w: Optional[float] = None
    """Average solar heat gain [W] over the preheat window. None = not computed."""

    minutes_saved_by_solar: Optional[float] = None
    """How many minutes shorter preheat is due to solar gain. None = not computed."""

    solar_confidence: str = "none"
    """Confidence in the solar component: 'high' (measured GHI + orientation),
    'medium' (measured GHI, no orientation — diffuse only), 'low' (clear-sky
    model, no measured data), 'none' (no solar data available)."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "from_temp_c": self.from_temp_c,
            "to_temp_c": self.to_temp_c,
            "outdoor_temp_c": self.outdoor_temp_c,
            "tau_seconds": self.tau_seconds,
            "radiator_watts_effective": self.radiator_watts_effective,
            "w_per_k": self.w_per_k,
            "steady_state_temp_c": self.steady_state_temp_c,
            "minutes_needed": self.minutes_needed,
            "reachable": self.reachable,
            "confidence": self.confidence,
            "warnings": self.warnings,
            "solar_gain_w": self.solar_gain_w,
            "minutes_saved_by_solar": self.minutes_saved_by_solar,
            "solar_confidence": self.solar_confidence,
        }


def compute_preheat(
        room_id: str,
        from_temp_c: float,
        to_temp_c: float,
        outdoor_temp_c: float,
        w_per_k: Optional[float],
        tau_seconds: Optional[float],
        radiator_watts_effective: Optional[float],
        confidence_in: str = "medium",
        max_minutes: int = 240,
        solar_gain_w: Optional[float] = None,
        solar_has_orientation: bool = False,
        solar_shortwave_measured: bool = False,
) -> PreheatEstimate:
    """
    Predict minutes needed to heat from_temp → to_temp given steady-state physics.

    Inputs:
      w_per_k: blended W/K from Phase 3
      tau_seconds: thermal time constant from Phase 3 (measured, if available)
      radiator_watts_effective: output at the *current flow temp* for the
          installed radiator. If None, we fall back to using the target
          (with-margin) output from Phase 4 sizing, which is the "ideal"
          installation. Returns lower confidence in that case.
      solar_gain_w: average solar heat gain [W] over the expected preheat
          window, from ``solar_gain.solar_gain_window().average_watts``.
          Treated as additional heating power — reduces minutes_needed.
          Pass None (default) to skip solar adjustment (backward compat).
      solar_has_orientation: True if at least one window had a facing_deg —
          affects solar_confidence reported on the estimate.
      solar_shortwave_measured: True if shortwave_radiation came from the
          weather service rather than the clear-sky model — affects
          solar_confidence.
    """
    est = PreheatEstimate(
        room_id=room_id,
        from_temp_c=from_temp_c,
        to_temp_c=to_temp_c,
        outdoor_temp_c=outdoor_temp_c,
        tau_seconds=tau_seconds,
        radiator_watts_effective=radiator_watts_effective,
        w_per_k=w_per_k,
        steady_state_temp_c=None,
        minutes_needed=None,
        reachable=False,
        confidence="none",
    )

    if from_temp_c >= to_temp_c:
        est.minutes_needed = 0.0
        est.reachable = True
        est.confidence = "high"
        return est

    if w_per_k is None or w_per_k <= 0:
        est.warnings.append("no heat loss rate available — cannot predict pre-heat")
        return est

    if tau_seconds is None or tau_seconds <= 0:
        # No measured tau — synthesise one from static model alone:
        #   tau = (m·c) / UA, where m·c is the thermal mass the Phase 3
        #   static block would have assumed if it could. Rough estimate.
        # This is less accurate but keeps a value available from day one.
        # m·c ~= 3 × (ρ·V·Cp) per room (same factor as compute_measured)
        # Without floor area here we can't estimate V directly; fall back
        # to a typical indoor tau of 3h as a coarse default.
        tau_seconds = 3 * 3600
        est.tau_seconds = tau_seconds
        est.warnings.append("using default tau of 3h (no measured data)")
        confidence_in = "low"

    # ── Solar gain adjustment ─────────────────────────────────────────
    # Solar gain reduces the net load on the radiator, lowering the time
    # to reach target. We model it by boosting the effective radiator output
    # by the average solar watts over the preheat window.
    #
    # Physics: T_steady = T_outdoor + (Q_rad + Q_solar) / W_per_K
    # The radiator and sun together push the room to a higher steady state,
    # so it reaches the target faster. The τ doesn't change (it's a property
    # of the room fabric, not the heat source).
    #
    # We also compute minutes_saved = minutes_without_solar − minutes_with_solar
    # so the UI can say "pre-heat: 45 min (solar saving ~10 min)".
    effective_radiator_w = radiator_watts_effective  # may be None
    if solar_gain_w is not None and solar_gain_w > 0.0:
        est.solar_gain_w = round(solar_gain_w, 1)
        # Determine solar confidence
        if solar_shortwave_measured and solar_has_orientation:
            est.solar_confidence = "high"
        elif solar_shortwave_measured:
            est.solar_confidence = "medium"   # diffuse only — no beam direction
        else:
            est.solar_confidence = "low"      # clear-sky fallback model
        if solar_gain_w > 0.5 * (radiator_watts_effective or 0):
            est.warnings.append(
                f"solar gain ({solar_gain_w:.0f} W) is >50% of radiator output — "
                f"preheat estimate depends heavily on forecast accuracy."
            )

        if effective_radiator_w is not None and effective_radiator_w > 0:
            effective_radiator_w = effective_radiator_w + solar_gain_w
        # If radiator_watts_effective was None we can't sensibly add solar
        # (the steady-state calc below handles None separately).

    if radiator_watts_effective is None or radiator_watts_effective <= 0:
        est.warnings.append(
            "no radiator capacity configured — pre-heat assumes the radiator "
            "is exactly sized to need. Real-world heat-up may be slower."
        )
        # Assume steady-state = target (i.e. radiator perfectly sized).
        # This gives an optimistic answer. Flag it.
        steady = to_temp_c + 2.0    # +2°C headroom to avoid singular math
        est.steady_state_temp_c = round(steady, 1)
        confidence_in = "low"
    else:
        steady = outdoor_temp_c + (effective_radiator_w / w_per_k)
        est.steady_state_temp_c = round(steady, 1)

    if steady <= to_temp_c + 0.1:
        est.reachable = False
        est.warnings.append(
            f"radiator + flow temp can only push room to {steady:.1f}°C — "
            f"cannot reach target {to_temp_c}°C at outdoor {outdoor_temp_c}°C"
        )
        est.confidence = "high"   # the negative answer is confident
        return est

    numerator = steady - to_temp_c
    denominator = steady - from_temp_c
    if denominator <= 0:
        est.minutes_needed = 0.0
        est.reachable = True
        est.confidence = confidence_in
        return est

    import math as _math
    t_seconds = -tau_seconds * _math.log(numerator / denominator)
    if t_seconds < 0:
        t_seconds = 0.0

    minutes = t_seconds / 60.0
    if minutes > max_minutes:
        est.warnings.append(
            f"predicted {minutes:.0f} min exceeds max of {max_minutes} min — "
            f"clamped. Check radiator sizing or flow temp."
        )
        minutes = max_minutes

    # ── Compute minutes_saved_by_solar ────────────────────────────────
    if solar_gain_w is not None and solar_gain_w > 0.0 and radiator_watts_effective:
        # Re-run without solar to get the baseline, then diff.
        steady_no_solar = outdoor_temp_c + (radiator_watts_effective / w_per_k)
        if steady_no_solar > to_temp_c + 0.1:
            num_ns = steady_no_solar - to_temp_c
            den_ns = steady_no_solar - from_temp_c
            if den_ns > 0:
                t_no_solar = -tau_seconds * _math.log(num_ns / den_ns)
                mins_no_solar = min(max_minutes, t_no_solar / 60.0)
                saved = mins_no_solar - minutes
                est.minutes_saved_by_solar = round(max(0.0, saved), 1)

    est.minutes_needed = round(minutes, 0)
    est.reachable = True
    est.confidence = confidence_in
    return est

# ──────────────────────────────────────────────────────────────────────
# ANOMALY DETECTION
# ──────────────────────────────────────────────────────────────────────

@_dc
class Anomaly:
    room_id: str
    kind: str                          # "fast_cool" | "slow_heat"
    severity: str                      # "info" | "warning" | "critical"
    detected_at: float                 # epoch seconds
    observed_tau_seconds: Optional[float] = None
    baseline_tau_seconds: Optional[float] = None
    tau_ratio: Optional[float] = None  # observed / baseline; <1 = faster cooling
    message: str = ""
    window_start_ts: Optional[float] = None
    window_end_ts: Optional[float] = None
    temp_drop_c: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "kind": self.kind,
            "severity": self.severity,
            "detected_at": self.detected_at,
            "observed_tau_seconds": self.observed_tau_seconds,
            "baseline_tau_seconds": self.baseline_tau_seconds,
            "tau_ratio": round(self.tau_ratio, 2) if self.tau_ratio else None,
            "message": self.message,
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "temp_drop_c": self.temp_drop_c,
        }


def detect_fast_cooling(
        room_id: str,
        recent_temperature_series: List[Dict[str, Any]],
        outdoor_temp_c: float,
        baseline_tau_seconds: float,
        min_duration_minutes: int = 20,
        fast_ratio_threshold: float = 0.5,   # observed tau < 50% of baseline → alert
        critical_ratio_threshold: float = 0.3,
) -> Optional[Anomaly]:
    """
    Look at the most recent cool-down interval. If its fitted tau is
    significantly shorter than the baseline, it's likely a window is open.
    """
    if not recent_temperature_series or not baseline_tau_seconds:
        return None

    windows = _find_cooldown_windows(
        recent_temperature_series, None,
        min_duration_sec=min_duration_minutes * 60,
        max_duration_sec=3 * 3600,
        min_drop_c=0.3,
    )
    if not windows:
        return None

    # Most recent window
    t0, t1, samples = windows[-1]
    tau, r2 = _fit_newton_cooling(samples, outdoor_temp_c)
    if tau is None or r2 is None or r2 < 0.5:
        return None

    ratio = tau / baseline_tau_seconds
    if ratio >= fast_ratio_threshold:
        return None  # within normal range

    import time as _t
    severity = "critical" if ratio < critical_ratio_threshold else "warning"
    temp_drop = round(samples[0][1] - samples[-1][1], 1)
    mins = round((t1 - t0) / 60)

    return Anomaly(
        room_id=room_id,
        kind="fast_cool",
        severity=severity,
        detected_at=_t.time(),
        observed_tau_seconds=round(tau, 0),
        baseline_tau_seconds=round(baseline_tau_seconds, 0),
        tau_ratio=ratio,
        window_start_ts=t0,
        window_end_ts=t1,
        temp_drop_c=temp_drop,
        message=(
            f"Room cooling {int((1 - ratio) * 100)}% faster than normal: "
            f"dropped {temp_drop}°C in {mins} min. Check for an open window, "
            f"door left open, or sudden weather change."
        ),
    )


def detect_slow_heating(
        room_id: str,
        recent_temperature_series: List[Dict[str, Any]],
        expected_tau_seconds: float,
        min_duration_minutes: int = 20,
        slow_ratio_threshold: float = 2.0,   # observed tau > 2× baseline → alert
        critical_ratio_threshold: float = 3.0,
) -> Optional[Anomaly]:
    """
    Check the most recent heat-up interval. Room should be warming toward an
    unknown steady state; if it's warming much slower than expected, flag.

    The tau here is the heat-up constant, which should be similar-ish to the
    cool-down constant for the same room (same thermal mass, same UA). Large
    divergence = something's wrong.
    """
    if not recent_temperature_series or not expected_tau_seconds:
        return None
    if len(recent_temperature_series) < 4:
        return None

    # Find heat-up intervals: monotonically rising temperature
    import datetime as _dt
    pts = []
    for p in recent_temperature_series:
        ts = p.get("ts")
        v = p.get("numeric_val")
        if v is None:
            try:
                v = float(p.get("value"))
            except (TypeError, ValueError):
                continue
        if not isinstance(ts, _dt.datetime):
            continue
        pts.append((ts.timestamp(), float(v)))
    if len(pts) < 4:
        return None
    pts.sort(key=lambda x: x[0])

    NOISE = 0.1
    cur_start = 0
    heatup_windows = []

    def _commit(i0, i1):
        if i1 - i0 < 3:
            return
        t0 = pts[i0][0]; t1 = pts[i1][0]
        if t1 - t0 < min_duration_minutes * 60:
            return
        if pts[i1][1] - pts[i0][1] < 0.3:  # need actual rise
            return
        heatup_windows.append((t0, t1, [(p[0] - t0, p[1]) for p in pts[i0:i1 + 1]]))

    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1] - NOISE:
            _commit(cur_start, i - 1)
            cur_start = i
    _commit(cur_start, len(pts) - 1)

    if not heatup_windows:
        return None

    t0, t1, samples = heatup_windows[-1]
    # For heat-up: T(t) = T_steady - (T_steady - T0) * exp(-t/tau)
    # We don't know T_steady. Estimate it as the max reached + a small margin.
    reached = max(s[1] for s in samples)
    steady_est = reached + 0.5
    # Same log-linear fit
    import math as _m
    xs, ys = [], []
    T0 = samples[0][1]
    for (t, T) in samples:
        delta = steady_est - T
        denom = steady_est - T0
        if delta <= 0 or denom <= 0:
            continue
        ratio = delta / denom
        if ratio <= 0 or ratio > 1.0:
            continue
        xs.append(t)
        ys.append(_m.log(ratio))
    if len(xs) < 3:
        return None
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    den = sum((x-mx)**2 for x in xs)
    if den <= 0:
        return None
    slope = num/den
    if slope >= 0:
        return None
    tau = -1.0/slope
    if tau < 60 or tau > 48*3600:
        return None

    ratio = tau / expected_tau_seconds
    if ratio <= slow_ratio_threshold:
        return None

    import time as _t
    severity = "critical" if ratio > critical_ratio_threshold else "warning"
    temp_rise = round(samples[-1][1] - samples[0][1], 1)
    mins = round((t1 - t0) / 60)

    return Anomaly(
        room_id=room_id,
        kind="slow_heat",
        severity=severity,
        detected_at=_t.time(),
        observed_tau_seconds=round(tau, 0),
        baseline_tau_seconds=round(expected_tau_seconds, 0),
        tau_ratio=ratio,
        window_start_ts=t0,
        window_end_ts=t1,
        temp_drop_c=-temp_rise,  # negative = rise
        message=(
            f"Room warming {int(ratio)}× slower than expected: "
            f"rose {temp_rise}°C in {mins} min. Check radiator is not blocked, "
            f"valve is opening, and no cold draught is present."
        ),
    )