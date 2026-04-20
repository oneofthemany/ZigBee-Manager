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

def compute_static(dimensions: Dict[str, Any], insulation: str = "partial") -> Tuple[Optional[float], StaticBreakdown, List[str]]:
    """
    Per-wall heat loss. Each wall can be external / party / internal, so we
    compute gross area from X×H or Y×H depending on orientation, subtract
    windows and doors attributed to that wall, then apply the appropriate
    U-value.
    """
    warnings: List[str] = []
    bd = StaticBreakdown()
    if not isinstance(dimensions, dict):
        return None, bd, ["no dimensions supplied"]

    u_table = U_VALUES.get(insulation) or U_VALUES["partial"]

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


# ──────────────────────────────────────────────────────────────────────
# LEARNED (FROM TELEMETRY)
# ──────────────────────────────────────────────────────────────────────

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
        outdoor_temp_getter,        # callable(ts_seconds) -> float | None
        floor_area_m2: float,
        ceiling_height_m: float = 2.4,
) -> Tuple[Optional[float], float, int, Optional[float], Optional[float]]:
    """
    Measure W/K from cool-down history.

    Returns (w_per_k, confidence_0_to_1, sample_count, r2_best, tau_median_s)
    """
    if not temperature_series or len(temperature_series) < 10:
        return None, 0.0, 0, None, None
    if not floor_area_m2 or floor_area_m2 <= 0:
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
) -> ThermalProfile:
    """Top-level orchestrator. Static + learned + blend."""
    prof = ThermalProfile(room_id=room_id)

    if not dimensions:
        prof.warnings.append("no dimensions configured — skipping thermal profile")
        return prof

    floor_area = float(dimensions.get("floor_area_m2") or 0.0)
    ceiling_h = float(dimensions.get("ceiling_height_m") or 2.4)

    # Static
    static_total, bd, static_warnings = compute_static(dimensions, insulation)
    prof.static_w_per_k = static_total
    prof.static_breakdown = bd
    prof.warnings.extend(static_warnings)

    # Measured
    if temperature_series and outdoor_temp_getter and floor_area > 0:
        m_ua, conf, n, r2, tau = compute_measured(
            temperature_series, outdoor_temp_getter, floor_area, ceiling_h
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
        steady = outdoor_temp_c + (radiator_watts_effective / w_per_k)
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