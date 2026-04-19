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
    Compute W/K heat loss from room dimensions + dwelling insulation level.

    Returns (total_w_per_k, breakdown, warnings). total is None if there's
    not enough data to compute.
    """
    warnings: List[str] = []
    bd = StaticBreakdown()

    if not isinstance(dimensions, dict):
        return None, bd, ["no dimensions supplied"]

    u_table = U_VALUES.get(insulation) or U_VALUES["partial"]

    # Walls
    walls = dimensions.get("walls") or {}
    ext_area = float(walls.get("external_m2") or 0.0)
    party_area = float(walls.get("party_m2") or 0.0)
    # Internal walls to other heated rooms lose no heat — skip.

    # We need to subtract window + door area from external walls so we don't
    # double-count the "hole" area. Doors only if they're external.
    windows = dimensions.get("windows") or []
    doors = dimensions.get("doors") or []
    ext_door_area = sum(float(d.get("area_m2") or 0.0)
                        for d in doors if d.get("type") == "external")
    total_window_area = sum(float(w.get("area_m2") or 0.0) for w in windows)

    ext_wall_net = max(0.0, ext_area - total_window_area - ext_door_area)
    if ext_area > 0 and ext_wall_net == 0:
        warnings.append(
            "window + door area exceeds external wall area — check inputs"
        )

    bd.walls_external = ext_wall_net * u_table["wall_ext"]
    bd.walls_party = party_area * u_table["wall_party"]

    # Windows (per-item glazing)
    for w in windows:
        area = float(w.get("area_m2") or 0.0)
        glazing = str(w.get("glazing", "double")).lower()
        u = u_table["window"].get(glazing, u_table["window"]["double"])
        bd.windows += area * u

    # Doors
    for d in doors:
        area = float(d.get("area_m2") or 0.0)
        if d.get("type") == "external":
            bd.doors += area * u_table["door_ext"]
        # Internal doors: zero loss

    # Floor
    floor_area = float(dimensions.get("floor_area_m2") or 0.0)
    floor_type = str(dimensions.get("floor_type", "unknown")).lower()
    bd.floor = floor_area * u_table["floor"].get(floor_type, u_table["floor"]["unknown"])

    # Ceiling — assume same footprint as floor
    ceiling_type = str(dimensions.get("ceiling_type", "unknown")).lower()
    bd.ceiling = floor_area * u_table["ceiling"].get(ceiling_type, u_table["ceiling"]["unknown"])

    # Ventilation / infiltration
    ceiling_h = float(dimensions.get("ceiling_height_m", 2.4))
    volume_m3 = floor_area * ceiling_h
    ach = ACH_BY_INSULATION.get(insulation, 1.0)
    # Sensible heat loss from air exchange = ρ·Cp·V·(ACH/3600) W/K
    bd.ventilation = AIR_DENSITY_KG_M3 * AIR_CP_J_KG_K * volume_m3 * (ach / 3600.0)

    total = bd.total
    if total <= 0:
        warnings.append("static calculation produced zero — check room dimensions")
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