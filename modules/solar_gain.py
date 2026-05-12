"""
solar_gain.py
=============
Estimates instantaneous and time-averaged solar heat gain into individual
rooms, using the room's window geometry and real-time sun position.

This is the first layer of solar-aware preheat and cooldown logic. It
answers two questions the heating controller needs:

  1. How many watts of free heat is the sun pumping into this room right now?
  2. How many watts will it average over the next N minutes (preheat window)?

Physics model
-------------
We use the ASHRAE simplified solar heat gain approach:

    Q_window = A × SHGC × I_incident                          [W]

where:
  A            = window area [m²]
  SHGC         = Solar Heat Gain Coefficient (glazing-type dependent)
  I_incident   = irradiance falling perpendicularly on the glass [W/m²]

I_incident is derived from either:
  a) A measured shortwave_radiation value from Open-Meteo (preferred — it's
     the real, cloud-attenuated value). We then apply a cosine projection to
     account for the angle between the sun and the window face.
  b) A clear-sky beam model (1000 × sin(elevation)) attenuated by a cloud
     fraction term, used when shortwave_radiation isn't available.

The cosine projection factor for a vertical window on a wall with outward
normal N_deg (bearing, clockwise from true north) and sun azimuth S_deg is:

    cos_inc = cos(elevation) × cos(S_deg − N_deg)

This is zero when the sun is behind or parallel to the wall, and 1.0 when
the sun is shining perpendicularly at the window at zero elevation (which
never happens in practice, but the geometry is correct).

Diffuse component
-----------------
On overcast days the beam is negligible but diffuse sky radiation is
significant. We model diffuse gain as:

    Q_diffuse = A × SHGC × I_diffuse_fraction × shortwave_radiation

where I_diffuse_fraction is a cloud-cover-weighted term. On a clear day most
radiation is direct beam; on a fully overcast day ~100% is diffuse (but
total is lower). A simple model: diffuse_fraction = 0.15 + 0.85 × cloud_fraction.

Integration with the rest of the system
----------------------------------------
  • `solar_gain_now(room_config, lat, lon, dt_utc, shortwave_wm2, cloud_fraction)`
    → float [W]  — instantaneous gain for one room.

  • `solar_gain_window(room_config, lat, lon, start_utc, duration_minutes,
                        shortwave_wm2, cloud_fraction)`
    → SolarGainWindow  — average watts + breakdown, used by preheat.

  • `solar_gain_forecast(room_config, lat, lon, start_utc, hourly_shortwave,
                          hourly_cloud_cover)`
    → List[SolarGainSample]  — per-hour gain profile, for scheduling decisions.

Config fields used from room_config
------------------------------------
The room config already has `dimensions.windows[]` and `dimensions.walls{}`.
This module adds one new optional field per wall:

    dimensions:
      walls:
        front: { type: external, facing_deg: 180 }   # south-facing outward normal
        left:  { type: external, facing_deg: 270 }   # west-facing
      windows:
        - { wall: front, area_m2: 2.1, glazing: double }

`facing_deg` is the compass bearing of the wall's outward normal
(0=N, 90=E, 180=S, 270=W). If a wall has no `facing_deg`, windows on
that wall contribute zero solar gain — the function degrades gracefully
rather than crashing.

All functions are pure (no I/O). Thread-safe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sun_position import sun_position

# ── Solar Heat Gain Coefficients by glazing type ──────────────────────
# ASHRAE Fundamentals 2021, Table 15 (centre-of-glass SHGC).
# These are conservative mid-range values for standard clear glass without
# low-e coatings; actual values vary by product but these are correct for
# SAP/building-physics estimation at this level of precision.
SHGC = {
    "single":  0.70,
    "double":  0.60,
    "triple":  0.40,
}
SHGC_DEFAULT = 0.60  # assume double if not specified

# ── Clear-sky beam irradiance reference ───────────────────────────────
# Peak clear-sky global horizontal irradiance at sea level. ASHRAE uses
# 1000 W/m² as a standard reference value; the actual extraterrestrial
# value is ~1361 W/m² but ~30% is absorbed/scattered even on clear days.
CLEAR_SKY_BEAM_WM2 = 1000.0

# ── Cloud attenuation model ───────────────────────────────────────────
# On a fully overcast sky (cloud_fraction=1.0) the beam is roughly 20% of
# clear-sky. The cubic term steepens the curve so that light cloud (0.2)
# has minimal effect while heavy cover (0.8+) drops beam substantially.
# From Kasten & Czeplak (1980) empirical fit.
def _beam_attenuation(cloud_fraction: float) -> float:
    """Cloud attenuation factor for direct beam irradiance. Range 0–1."""
    cf = max(0.0, min(1.0, cloud_fraction))
    return max(0.05, 1.0 - 0.75 * (cf ** 3.4))


def _diffuse_fraction(cloud_fraction: float) -> float:
    """
    Fraction of total irradiance arriving as diffuse (sky) radiation.
    On clear days most is direct beam; on overcast days diffuse dominates
    but total drops sharply.
    """
    cf = max(0.0, min(1.0, cloud_fraction))
    return 0.15 + 0.85 * cf


# ── Geometry ──────────────────────────────────────────────────────────

def _cos_incidence(sun_azimuth_deg: float, sun_elevation_deg: float,
                   wall_facing_deg: float) -> float:
    """
    Cosine of the angle of incidence between the sun's rays and a vertical
    window on a wall with outward normal `wall_facing_deg`.

    Returns 0.0 if the sun is behind the wall (incidence > 90°), so negative
    values are clamped — there's no negative solar gain from a window.

    The formula for a vertical surface:
        cos(inc) = cos(elevation) × cos(azimuth − facing)
    """
    if sun_elevation_deg <= 0.0:
        return 0.0  # Sun below horizon
    az_diff_rad = math.radians(sun_azimuth_deg - wall_facing_deg)
    el_rad = math.radians(sun_elevation_deg)
    cos_inc = math.cos(el_rad) * math.cos(az_diff_rad)
    return max(0.0, cos_inc)


# ── Per-window gain ───────────────────────────────────────────────────

def _window_gain_watts(
        window: Dict[str, Any],
        wall_facing_deg: Optional[float],
        sun_azimuth_deg: float,
        sun_elevation_deg: float,
        shortwave_wm2: Optional[float],
        cloud_fraction: float,
) -> float:
    """
    Solar heat gain through a single window [W].

    If wall_facing_deg is None (wall has no facing_deg configured) we can
    still compute a degraded estimate using the diffuse-only component,
    since diffuse radiation doesn't depend on window orientation. The beam
    component is zero in that case.
    """
    area_m2 = float(window.get("area_m2") or 0.0)
    if area_m2 <= 0.0:
        return 0.0

    glazing = str(window.get("glazing") or "double").lower()
    shgc = SHGC.get(glazing, SHGC_DEFAULT)

    cf = max(0.0, min(1.0, cloud_fraction))

    if shortwave_wm2 is not None and shortwave_wm2 >= 0.0:
        # ── Measured irradiance path (preferred) ──────────────────────
        # Open-Meteo shortwave_radiation is global horizontal irradiance (GHI).
        # Split into direct beam and diffuse using cloud fraction.
        diff_frac = _diffuse_fraction(cf)
        beam_ghi = shortwave_wm2 * (1.0 - diff_frac)
        diffuse = shortwave_wm2 * diff_frac

        # Project beam onto the window surface (beam_ghi is horizontal; convert
        # to the irradiance on the tilted/vertical window face).
        if wall_facing_deg is not None and sun_elevation_deg > 0.0:
            cos_inc = _cos_incidence(sun_azimuth_deg, sun_elevation_deg, wall_facing_deg)
            # GHI → beam on tilted surface via transposition (simplified Liu-Jordan):
            # I_beam_surface = I_beam_horizontal × cos(inc) / sin(elevation)
            sin_el = math.sin(math.radians(sun_elevation_deg))
            if sin_el > 0.01:
                beam_surface = beam_ghi * cos_inc / sin_el
            else:
                beam_surface = 0.0
        else:
            # No facing data — only diffuse contribution. Still useful on
            # overcast days and doesn't crash the whole calculation.
            beam_surface = 0.0

        # Diffuse: isotropic sky model — a vertical surface sees half the sky
        # hemisphere, so multiply by 0.5.
        diffuse_on_surface = diffuse * 0.5

        total_irradiance = beam_surface + diffuse_on_surface

    else:
        # ── Clear-sky beam model fallback ─────────────────────────────
        # Used when shortwave_radiation is not available from the weather service.
        beam = CLEAR_SKY_BEAM_WM2 * _beam_attenuation(cf)

        if wall_facing_deg is not None:
            cos_inc = _cos_incidence(sun_azimuth_deg, sun_elevation_deg, wall_facing_deg)
            beam_on_surface = beam * cos_inc
        else:
            beam_on_surface = 0.0

        diffuse = CLEAR_SKY_BEAM_WM2 * 0.15 * cf  # rough isotropic diffuse
        total_irradiance = beam_on_surface + diffuse * 0.5

    return area_m2 * shgc * total_irradiance


# ── Room-level gain ───────────────────────────────────────────────────

def _room_gain_at_position(
        room_config: Dict[str, Any],
        sun_azimuth_deg: float,
        sun_elevation_deg: float,
        shortwave_wm2: Optional[float],
        cloud_fraction: float,
) -> float:
    """
    Sum solar heat gain [W] across all windows in the room at the given
    sun position and irradiance conditions.
    """
    dimensions = room_config.get("dimensions") or {}
    walls = dimensions.get("walls") or {}
    windows = dimensions.get("windows") or []

    if not windows:
        return 0.0

    total_watts = 0.0
    for window in windows:
        wall_key = window.get("wall")
        wall_cfg = walls.get(wall_key) if wall_key else None
        # Only windows on external walls contribute solar gain
        if wall_cfg and wall_cfg.get("type") not in ("external",):
            continue
        facing_deg: Optional[float] = None
        if wall_cfg:
            fd = wall_cfg.get("facing_deg")
            if fd is not None:
                facing_deg = float(fd)

        total_watts += _window_gain_watts(
            window=window,
            wall_facing_deg=facing_deg,
            sun_azimuth_deg=sun_azimuth_deg,
            sun_elevation_deg=sun_elevation_deg,
            shortwave_wm2=shortwave_wm2,
            cloud_fraction=cloud_fraction,
        )

    return total_watts


# ── Public API ────────────────────────────────────────────────────────

def solar_gain_now(
        room_config: Dict[str, Any],
        lat: float,
        lon: float,
        dt_utc: Optional[datetime] = None,
        shortwave_wm2: Optional[float] = None,
        cloud_fraction: float = 0.0,
) -> float:
    """
    Instantaneous solar heat gain into a room [W].

    Args:
        room_config:    Room config dict with `dimensions.walls` and
                        `dimensions.windows`. See module docstring for
                        the `facing_deg` field.
        lat, lon:       Location (WGS-84 degrees).
        dt_utc:         Moment to evaluate. Defaults to now (UTC).
        shortwave_wm2:  Global horizontal irradiance from Open-Meteo's
                        `shortwave_radiation` field [W/m²]. If None, the
                        clear-sky beam model is used instead.
        cloud_fraction: Cloud cover as a fraction 0.0–1.0. Used both to
                        attenuate beam (fallback path) and to split
                        GHI into beam vs diffuse (measured path).
                        Pass `cloud_cover / 100` from Open-Meteo.

    Returns:
        Estimated solar heat gain in watts. 0.0 if the sun is below the
        horizon or no window data is available.
    """
    when = dt_utc or datetime.now(timezone.utc)
    pos = sun_position(lat, lon, when)
    if not pos["is_daylight"]:
        return 0.0

    return _room_gain_at_position(
        room_config=room_config,
        sun_azimuth_deg=pos["azimuth_deg"],
        sun_elevation_deg=pos["elevation_deg"],
        shortwave_wm2=shortwave_wm2,
        cloud_fraction=cloud_fraction,
    )


@dataclass
class SolarGainWindow:
    """
    Result of `solar_gain_window()` — averaged solar contribution over
    a future time window, suitable for use in preheat calculations.
    """
    average_watts: float
    """Mean solar heat gain [W] over the requested duration."""

    peak_watts: float
    """Maximum single-sample solar heat gain [W] within the window."""

    has_orientation_data: bool
    """True if at least one window had a `facing_deg` configured wall,
    meaning the beam component could be computed. If False, results
    reflect diffuse-only and are conservative (lower than reality on
    clear days)."""

    sample_count: int
    """Number of time samples used to compute the average."""

    warnings: List[str] = field(default_factory=list)
    """Human-readable notes about degraded accuracy."""

    def effective_temperature_offset_c(self, w_per_k: float) -> float:
        """
        Express average solar gain as an equivalent outdoor temperature
        offset [°C]. Useful for adjusted preheat calculations:

            T_effective_outdoor = T_outdoor − solar_temp_offset

        A room receiving 200 W of solar gain and losing 80 W/K has an
        effective outdoor temperature 2.5 °C warmer than measured outside.
        Equivalently, the radiator needs 200 W less to hold the target.
        """
        if w_per_k <= 0.0:
            return 0.0
        return self.average_watts / w_per_k

    def to_dict(self) -> Dict[str, Any]:
        return {
            "average_watts": round(self.average_watts, 1),
            "peak_watts": round(self.peak_watts, 1),
            "has_orientation_data": self.has_orientation_data,
            "sample_count": self.sample_count,
            "warnings": self.warnings,
        }


def solar_gain_window(
        room_config: Dict[str, Any],
        lat: float,
        lon: float,
        start_utc: Optional[datetime] = None,
        duration_minutes: int = 90,
        step_minutes: int = 15,
        shortwave_wm2: Optional[float] = None,
        cloud_fraction: float = 0.0,
) -> SolarGainWindow:
    """
    Average solar heat gain [W] over a future time window.

    This is the primary integration point for `compute_preheat` in
    `thermal_profile.py`. Pass the result's `average_watts` as the
    `solar_gain_w` argument to reduce the effective radiator demand.

    Args:
        room_config:        Room config dict.
        lat, lon:           Location.
        start_utc:          Start of the window. Defaults to now (UTC).
        duration_minutes:   How far ahead to average. Should match the
                            expected preheat duration (pass the current
                            preheat estimate and iterate if needed; in
                            practice a single pass is sufficient).
        step_minutes:       Sampling interval within the window. 15 min
                            is adequate — solar position changes slowly.
        shortwave_wm2:      Current GHI from weather service [W/m²].
                            Used as a constant over the window (it's the
                            most recent observed value). For longer windows
                            (>2 h) the forecast path is more accurate.
        cloud_fraction:     Cloud cover fraction 0.0–1.0.

    Returns:
        SolarGainWindow with average_watts, peak_watts, and diagnostics.
    """
    start = start_utc or datetime.now(timezone.utc)
    step = max(5, min(60, step_minutes))
    samples: List[float] = []
    has_orientation = False
    warnings: List[str] = []

    # Check whether any window has orientation data
    dimensions = room_config.get("dimensions") or {}
    walls_cfg = dimensions.get("walls") or {}
    windows_cfg = dimensions.get("windows") or []
    for w in windows_cfg:
        wall_key = w.get("wall")
        wall_def = walls_cfg.get(wall_key) if wall_key else None
        if wall_def and wall_def.get("facing_deg") is not None:
            has_orientation = True
            break

    if not has_orientation:
        warnings.append(
            "No wall facing_deg configured — beam component omitted; "
            "solar gain estimates are diffuse-only and will be conservative."
        )

    t = start
    end = start + timedelta(minutes=duration_minutes)
    while t <= end:
        pos = sun_position(lat, lon, t)
        if pos["is_daylight"]:
            gain = _room_gain_at_position(
                room_config=room_config,
                sun_azimuth_deg=pos["azimuth_deg"],
                sun_elevation_deg=pos["elevation_deg"],
                shortwave_wm2=shortwave_wm2,
                cloud_fraction=cloud_fraction,
            )
            samples.append(gain)
        else:
            samples.append(0.0)
        t += timedelta(minutes=step)

    if not samples:
        return SolarGainWindow(
            average_watts=0.0,
            peak_watts=0.0,
            has_orientation_data=has_orientation,
            sample_count=0,
            warnings=["No samples computed (window entirely at night?)"],
        )

    return SolarGainWindow(
        average_watts=sum(samples) / len(samples),
        peak_watts=max(samples),
        has_orientation_data=has_orientation,
        sample_count=len(samples),
        warnings=warnings,
    )


@dataclass
class SolarGainSample:
    """Single hourly sample in a forecast profile."""
    dt_utc: datetime
    watts: float
    sun_azimuth_deg: float
    sun_elevation_deg: float
    is_daylight: bool


def solar_gain_forecast(
        room_config: Dict[str, Any],
        lat: float,
        lon: float,
        start_utc: Optional[datetime] = None,
        hourly_shortwave_wm2: Optional[Sequence[float]] = None,
        hourly_cloud_fraction: Optional[Sequence[float]] = None,
        hours: int = 12,
) -> List[SolarGainSample]:
    """
    Per-hour solar gain profile over the next N hours.

    Used by the controller's schedule lookahead to decide whether to advance
    or delay a heat event based on incoming solar gain.

    Args:
        room_config:            Room config dict.
        lat, lon:               Location.
        start_utc:              Start time. Defaults to now (UTC).
        hourly_shortwave_wm2:   Sequence of hourly Open-Meteo
                                `shortwave_radiation` forecasts [W/m²].
                                Length should be >= `hours`.
        hourly_cloud_fraction:  Sequence of hourly cloud cover fractions
                                (0.0–1.0). Length should be >= `hours`.
        hours:                  How many hours to compute.

    Returns:
        List of SolarGainSample, one per hour starting from `start_utc`.
    """
    start = start_utc or datetime.now(timezone.utc)
    # Align to the current hour for cleaner indexing against forecast arrays
    start_hour = start.replace(minute=0, second=0, microsecond=0)

    results: List[SolarGainSample] = []
    for h in range(hours):
        t = start_hour + timedelta(hours=h)
        pos = sun_position(lat, lon, t)

        sw: Optional[float] = None
        cf = 0.0
        if hourly_shortwave_wm2 and h < len(hourly_shortwave_wm2):
            sw = float(hourly_shortwave_wm2[h])
        if hourly_cloud_fraction and h < len(hourly_cloud_fraction):
            cf = float(hourly_cloud_fraction[h])

        if pos["is_daylight"]:
            gain = _room_gain_at_position(
                room_config=room_config,
                sun_azimuth_deg=pos["azimuth_deg"],
                sun_elevation_deg=pos["elevation_deg"],
                shortwave_wm2=sw,
                cloud_fraction=cf,
            )
        else:
            gain = 0.0

        results.append(SolarGainSample(
            dt_utc=t,
            watts=gain,
            sun_azimuth_deg=pos["azimuth_deg"],
            sun_elevation_deg=pos["elevation_deg"],
            is_daylight=pos["is_daylight"],
        ))

    return results


# ── Convenience: suppression check ───────────────────────────────────

def should_suppress_heat_call(
        room_config: Dict[str, Any],
        lat: float,
        lon: float,
        current_temp_c: float,
        target_temp_c: float,
        w_per_k: float,
        dt_utc: Optional[datetime] = None,
        shortwave_wm2: Optional[float] = None,
        cloud_fraction: float = 0.0,
        suppress_fraction: float = 0.8,
) -> tuple[bool, float]:
    """
    Should the controller suppress a heat call because solar gain is
    sufficient to close (most of) the temperature deficit on its own?

    This is a conservative check — suppression only fires when solar alone
    can cover `suppress_fraction` (default 80%) of the remaining deficit.

    Args:
        room_config:        Room config dict.
        lat, lon:           Location.
        current_temp_c:     Current room temperature [°C].
        target_temp_c:      Target temperature [°C].
        w_per_k:            Room heat loss coefficient [W/K].
        dt_utc:             Evaluation instant (default: now UTC).
        shortwave_wm2:      Current GHI [W/m²].
        cloud_fraction:     Cloud cover fraction 0.0–1.0.
        suppress_fraction:  Fraction of deficit solar must cover to suppress.
                            Lower = more aggressive suppression.
                            Higher = more conservative (default 0.8).

    Returns:
        (should_suppress: bool, solar_watts: float)
        The float is the current solar gain estimate for logging.
    """
    if current_temp_c >= target_temp_c:
        # Room already at or above target — suppress regardless
        return True, 0.0

    deficit_k = target_temp_c - current_temp_c
    # Watts needed to hold the deficit (steady-state, no dynamics)
    watts_needed = deficit_k * w_per_k

    solar_w = solar_gain_now(
        room_config=room_config,
        lat=lat,
        lon=lon,
        dt_utc=dt_utc,
        shortwave_wm2=shortwave_wm2,
        cloud_fraction=cloud_fraction,
    )

    if watts_needed <= 0.0:
        return True, solar_w

    suppress = (solar_w / watts_needed) >= suppress_fraction
    return suppress, solar_w


# ─────────────────────────────── self-test ───────────────────────────────

if __name__ == "__main__":
    from datetime import date

    # Reference room: south-facing living room, London, midsummer noon
    # A 2.1 m² double-glazed south window should receive substantial gain.
    ROOM = {
        "id": "living",
        "dimensions": {
            "walls": {
                "front": {"type": "external", "facing_deg": 180},   # south
                "back":  {"type": "internal"},
                "left":  {"type": "party"},
                "right": {"type": "external", "facing_deg": 270},   # west
            },
            "windows": [
                {"wall": "front", "area_m2": 2.1, "glazing": "double"},
                {"wall": "right", "area_m2": 0.8, "glazing": "double"},
            ],
        },
    }
    LAT, LON = 51.5074, -0.1278
    # 2026-06-21 12:00 UTC — sun due south, elevation ~61.5°
    noon = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)

    # ── Test 1: Clear sky at solar noon ───────────────────────────────
    gain_noon = solar_gain_now(ROOM, LAT, LON, dt_utc=noon, cloud_fraction=0.0)
    print(f"Clear noon gain:  {gain_noon:.1f} W")
    # South window: cos_inc ≈ cos(61.5°) ≈ 0.474; beam_surface ~ 1000×0.474;
    # gain ≈ 2.1 × 0.6 × (roughly) 400–500 W/m² → ~500–630 W
    assert 300.0 < gain_noon < 900.0, f"Unexpected noon gain: {gain_noon}"

    # ── Test 2: Fully overcast ────────────────────────────────────────
    gain_overcast = solar_gain_now(ROOM, LAT, LON, dt_utc=noon, cloud_fraction=1.0)
    print(f"Overcast noon:    {gain_overcast:.1f} W")
    assert gain_overcast < gain_noon, "Overcast should give less gain than clear"
    assert gain_overcast > 0.0, "Diffuse component should still give some gain"

    # ── Test 3: Night returns zero ────────────────────────────────────
    midnight = datetime(2026, 6, 21, 1, 0, 0, tzinfo=timezone.utc)
    gain_night = solar_gain_now(ROOM, LAT, LON, dt_utc=midnight)
    print(f"Midnight gain:    {gain_night:.1f} W")
    assert gain_night == 0.0, "No solar gain at night"

    # ── Test 4: With measured shortwave_radiation ─────────────────────
    # Open-Meteo might give us 650 W/m² on a partly cloudy midsummer noon
    gain_measured = solar_gain_now(
        ROOM, LAT, LON, dt_utc=noon,
        shortwave_wm2=650.0, cloud_fraction=0.3,
    )
    print(f"Measured 650 W/m² gain: {gain_measured:.1f} W")
    assert gain_measured > 0.0

    # ── Test 5: Window covering (preheat window) ──────────────────────
    sgw = solar_gain_window(
        ROOM, LAT, LON,
        start_utc=datetime(2026, 6, 21, 6, 30, 0, tzinfo=timezone.utc),
        duration_minutes=90,
        shortwave_wm2=400.0,
        cloud_fraction=0.2,
    )
    print(f"Preheat window:   avg={sgw.average_watts:.1f} W  "
          f"peak={sgw.peak_watts:.1f} W  samples={sgw.sample_count}")
    assert sgw.average_watts >= 0.0
    assert sgw.peak_watts >= sgw.average_watts

    # ── Test 6: Forecast profile ──────────────────────────────────────
    forecast = solar_gain_forecast(
        ROOM, LAT, LON,
        start_utc=datetime(2026, 6, 21, 6, 0, 0, tzinfo=timezone.utc),
        hourly_shortwave_wm2=[0, 50, 200, 500, 750, 850, 800, 650, 400, 150, 30, 0],
        hourly_cloud_fraction=[0.1] * 12,
        hours=12,
    )
    print("Forecast profile:")
    for s in forecast:
        bar = "█" * int(s.watts / 50)
        print(f"  {s.dt_utc.strftime('%H:%M')}  {s.watts:6.1f} W  {bar}")
    assert len(forecast) == 12

    # ── Test 7: Suppression check ─────────────────────────────────────
    # Room at 18°C, target 21°C, heat loss 80 W/K, strong noon sun
    suppress, sw = should_suppress_heat_call(
        ROOM, LAT, LON,
        current_temp_c=20.5, target_temp_c=21.0,
        w_per_k=80.0,
        dt_utc=noon, shortwave_wm2=800.0, cloud_fraction=0.1,
    )
    print(f"\nSuppression check: suppress={suppress}  solar={sw:.1f} W")

    # Room 3°C below target — solar shouldn't be able to cover 80% of 240 W
    suppress2, sw2 = should_suppress_heat_call(
        ROOM, LAT, LON,
        current_temp_c=18.0, target_temp_c=21.0,
        w_per_k=80.0,
        dt_utc=noon, shortwave_wm2=400.0, cloud_fraction=0.5,
    )
    print(f"Large deficit:     suppress={suppress2}  solar={sw2:.1f} W")

    # ── Test 8: No orientation data — graceful degradation ────────────
    ROOM_NO_FACING = {
        "id": "bedroom",
        "dimensions": {
            "walls": {
                "front": {"type": "external"},   # no facing_deg
            },
            "windows": [
                {"wall": "front", "area_m2": 1.2, "glazing": "double"},
            ],
        },
    }
    gain_no_orient = solar_gain_now(
        ROOM_NO_FACING, LAT, LON, dt_utc=noon,
        shortwave_wm2=700.0, cloud_fraction=0.2,
    )
    print(f"\nNo-orientation gain: {gain_no_orient:.1f} W (diffuse only)")
    # Should be > 0 (diffuse) but less than oriented south window
    assert gain_no_orient < gain_noon

    print("\n✓ all solar_gain self-tests passed")