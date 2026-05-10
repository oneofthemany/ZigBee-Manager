"""
modules/sun_position.py
=======================
Solar position (azimuth, elevation, sunrise, sunset) computed locally with
no network dependency. Pure functions — no I/O.

Implementation follows the NOAA Solar Calculator algorithm
(https://gml.noaa.gov/grad/solcalc/calcdetails.html), which is the
spreadsheet most photovoltaic / building-physics tools cite. Accuracy is
~0.01° on azimuth and ~0.01° on elevation between 1900–2100, which is
overkill for heating-control purposes but is also small enough that the
maths is one page and we don't need a third-party library.

All inputs and outputs are in:
  - degrees (latitude, longitude, azimuth, elevation, declination)
  - UTC for time
  - latitude positive north, longitude positive east
  - azimuth measured clockwise from true north (0=N, 90=E, 180=S, 270=W)

Public API
----------
    sun_position(lat, lon, dt_utc=None)            -> {az, el, ...}
    sunrise_sunset(lat, lon, date_utc=None)        -> {sunrise, sunset, noon}
    sun_path_for_day(lat, lon, date_utc=None,
                     step_minutes=15)              -> list of {ts, az, el}
    sun_path_year_envelope(lat, lon)               -> {summer, equinox, winter}
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

# ─────────────────────────── time helpers ───────────────────────────

DateTimeLike = Union[None, str, int, float, datetime, date]


def _to_utc_datetime(value: DateTimeLike) -> datetime:
    """Coerce caller input to a timezone-aware UTC datetime."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):  # date but not datetime
        return datetime(value.year, value.month, value.day, 12, 0, 0, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Epoch seconds (heuristic: < 10^12 → seconds, else ms)
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        # Allow trailing 'Z'
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Try date-only
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TypeError(f"unsupported time input: {type(value).__name__}")


def _to_utc_date(value: DateTimeLike) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return _to_utc_datetime(value).date()


def _julian_day(dt_utc: datetime) -> float:
    """Astronomical Julian Day Number (fractional)."""
    y, m = dt_utc.year, dt_utc.month
    d = dt_utc.day + (
            dt_utc.hour + (dt_utc.minute + dt_utc.second / 60.0) / 60.0
    ) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = (
            math.floor(365.25 * (y + 4716))
            + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5
    )
    return jd


def _julian_century(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


# ─────────────────────────── core SPA maths ───────────────────────────

def _atmospheric_refraction_deg(elev_uncorrected_deg: float) -> float:
    """
    NOAA-style atmospheric refraction correction. Adds to the geometric
    elevation. Result is in degrees.
    """
    el = elev_uncorrected_deg
    if el > 85.0:
        ref_arcsec = 0.0
    elif el > 5.0:
        t = math.tan(math.radians(el))
        ref_arcsec = 58.1 / t - 0.07 / (t ** 3) + 0.000086 / (t ** 5)
    elif el > -0.575:
        ref_arcsec = (
                1735.0
                + el * (-518.2 + el * (103.4 + el * (-12.79 + el * 0.711)))
        )
    else:
        ref_arcsec = -20.772 / math.tan(math.radians(el))
    return ref_arcsec / 3600.0


def _solar_geometry(jc: float) -> Dict[str, float]:
    """Compute the orbit-derived intermediate quantities used by everything else."""
    geom_mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    geom_mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eccent = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    m_rad = math.radians(geom_mean_anom)
    sun_eq_ctr = (
            math.sin(m_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
            + math.sin(2.0 * m_rad) * (0.019993 - 0.000101 * jc)
            + math.sin(3.0 * m_rad) * 0.000289
    )
    sun_true_long = geom_mean_long + sun_eq_ctr
    sun_app_long = (
            sun_true_long - 0.00569
            - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))
    )
    mean_obliq_ecl = (
            23.0 + (26.0 + ((21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813)))) / 60.0) / 60.0
    )
    obliq_corr = mean_obliq_ecl + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc))

    sin_app = math.sin(math.radians(sun_app_long))
    cos_app = math.cos(math.radians(sun_app_long))
    decl_deg = math.degrees(math.asin(math.sin(math.radians(obliq_corr)) * sin_app))

    # Equation of time (minutes)
    var_y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    g_rad = math.radians(geom_mean_long)
    eot_min = 4.0 * math.degrees(
        var_y * math.sin(2.0 * g_rad)
        - 2.0 * eccent * math.sin(m_rad)
        + 4.0 * eccent * var_y * math.sin(m_rad) * math.cos(2.0 * g_rad)
        - 0.5 * var_y * var_y * math.sin(4.0 * g_rad)
        - 1.25 * eccent * eccent * math.sin(2.0 * m_rad)
    )

    return {
        "decl_deg": decl_deg,
        "eot_min": eot_min,
        "obliq_corr": obliq_corr,
    }


def sun_position(
        lat: float,
        lon: float,
        dt_utc: DateTimeLike = None,
) -> Dict[str, Any]:
    """
    Sun azimuth and elevation at a single instant.

    Returns:
        {
            "ts":               UTC ISO-8601 timestamp,
            "lat":              float,
            "lon":              float,
            "azimuth_deg":      0–360, clockwise from true north,
            "elevation_deg":    -90 to +90, refraction-corrected,
            "elevation_geom":   geometric (refraction-free) elevation,
            "declination_deg":  solar declination,
            "is_daylight":      bool — corrected elevation > 0,
        }
    """
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"latitude {lat} out of range")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"longitude {lon} out of range")

    when = _to_utc_datetime(dt_utc)
    jd = _julian_day(when)
    jc = _julian_century(jd)
    geom = _solar_geometry(jc)
    decl = geom["decl_deg"]
    eot = geom["eot_min"]

    # True solar time in minutes since LST midnight
    minutes_of_day = when.hour * 60.0 + when.minute + when.second / 60.0
    true_solar_min = (minutes_of_day + eot + 4.0 * lon) % 1440.0

    # Hour angle (deg): negative before solar noon, positive after
    hour_angle = (true_solar_min / 4.0) - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0

    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    ha_rad = math.radians(hour_angle)

    cos_zenith = (
            math.sin(lat_rad) * math.sin(decl_rad)
            + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith_deg = math.degrees(math.acos(cos_zenith))
    elev_geom_deg = 90.0 - zenith_deg
    elev_corr_deg = elev_geom_deg + _atmospheric_refraction_deg(elev_geom_deg)

    # Azimuth: clockwise from north
    sin_zen = math.sin(math.radians(zenith_deg))
    if sin_zen < 1e-9:
        # Sun directly overhead or below — azimuth undefined; pick north
        azimuth_deg = 0.0
    else:
        cos_az = (
                (math.sin(lat_rad) * math.cos(math.radians(zenith_deg)) - math.sin(decl_rad))
                / (math.cos(lat_rad) * sin_zen)
        )
        cos_az = max(-1.0, min(1.0, cos_az))
        az_partial = math.degrees(math.acos(cos_az))
        if hour_angle > 0.0:
            azimuth_deg = (az_partial + 180.0) % 360.0
        else:
            azimuth_deg = (540.0 - az_partial) % 360.0

    return {
        "ts": when.isoformat().replace("+00:00", "Z"),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "azimuth_deg": round(azimuth_deg, 3),
        "elevation_deg": round(elev_corr_deg, 3),
        "elevation_geom": round(elev_geom_deg, 3),
        "declination_deg": round(decl, 3),
        "is_daylight": elev_corr_deg > 0.0,
    }


# ─────────────────────────── sunrise/sunset ────────────────────────────

# Standard horizon depression for sunrise/sunset = 0.833° (16′ semidiameter +
# 34′ refraction). NOAA uses 90.833° as the zenith threshold.
_SUNRISE_SUNSET_ZENITH_DEG = 90.833


def _solar_noon_utc_minutes(lon: float, eot_min: float) -> float:
    """Solar noon in minutes UTC since 00:00 of the requested day."""
    return (720.0 - 4.0 * lon - eot_min) % 1440.0


def _hour_angle_horizon(lat: float, decl: float, zenith_deg: float) -> Optional[float]:
    """
    Hour angle (deg) at which the sun crosses ``zenith_deg``.
    Returns ``None`` if the sun never reaches that altitude on this day
    (polar day or polar night).
    """
    cos_h = (
                    math.cos(math.radians(zenith_deg))
                    - math.sin(math.radians(lat)) * math.sin(math.radians(decl))
            ) / (math.cos(math.radians(lat)) * math.cos(math.radians(decl)))
    if cos_h > 1.0 or cos_h < -1.0:
        return None
    return math.degrees(math.acos(cos_h))


def sunrise_sunset(
        lat: float,
        lon: float,
        date_utc: DateTimeLike = None,
) -> Dict[str, Any]:
    """
    Returns sunrise, solar noon, and sunset (all UTC ISO-8601 strings).

    On polar days/nights, sunrise/sunset are ``None`` and ``daylight_minutes``
    is 1440 (always up) or 0 (always down).
    """
    d = _to_utc_date(date_utc)
    midday = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    jc = _julian_century(_julian_day(midday))
    geom = _solar_geometry(jc)
    decl = geom["decl_deg"]
    eot = geom["eot_min"]

    noon_min = _solar_noon_utc_minutes(lon, eot)
    ha = _hour_angle_horizon(lat, decl, _SUNRISE_SUNSET_ZENITH_DEG)

    if ha is None:
        # Determine if it's polar day or polar night by sampling noon elevation
        midday_pos = sun_position(lat, lon, midday)
        always_up = midday_pos["elevation_deg"] > 0.0
        return {
            "date": d.isoformat(),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "sunrise": None,
            "sunset": None,
            "solar_noon": _minutes_to_iso(d, noon_min),
            "daylight_minutes": 1440 if always_up else 0,
            "polar": "day" if always_up else "night",
        }

    sunrise_min = (noon_min - 4.0 * ha) % 1440.0
    sunset_min = (noon_min + 4.0 * ha) % 1440.0
    daylight = 8.0 * ha

    return {
        "date": d.isoformat(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "sunrise": _minutes_to_iso(d, sunrise_min),
        "sunset": _minutes_to_iso(d, sunset_min),
        "solar_noon": _minutes_to_iso(d, noon_min),
        "daylight_minutes": round(daylight, 1),
        "polar": None,
    }


def _minutes_to_iso(d: date, minutes: float) -> str:
    """UTC midnight + N minutes -> ISO-8601 'Z'-suffixed string."""
    base = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    dt = base + timedelta(minutes=float(minutes))
    return dt.isoformat().replace("+00:00", "Z")


# ─────────────────────────── day arc ─────────────────────────────

def sun_path_for_day(
        lat: float,
        lon: float,
        date_utc: DateTimeLike = None,
        step_minutes: int = 15,
        daylight_only: bool = False,
) -> Dict[str, Any]:
    """
    Sun azimuth/elevation sampled at regular intervals across a full UTC day.

    Args:
        step_minutes: 1–60. Defaults to 15 (96 points/day).
        daylight_only: if True, drop samples where elevation ≤ 0.
    """
    step_minutes = max(1, min(60, int(step_minutes)))
    d = _to_utc_date(date_utc)
    base = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    rs = sunrise_sunset(lat, lon, d)

    points: List[Dict[str, Any]] = []
    minutes = 0
    while minutes < 1440:
        when = base + timedelta(minutes=minutes)
        p = sun_position(lat, lon, when)
        if not daylight_only or p["elevation_deg"] > 0.0:
            points.append({
                "ts": p["ts"],
                "az": p["azimuth_deg"],
                "el": p["elevation_deg"],
            })
        minutes += step_minutes

    return {
        "date": d.isoformat(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "step_minutes": step_minutes,
        "sunrise": rs["sunrise"],
        "sunset": rs["sunset"],
        "solar_noon": rs["solar_noon"],
        "daylight_minutes": rs["daylight_minutes"],
        "polar": rs["polar"],
        "points": points,
    }


# ─────────────────────────── year envelope ─────────────────────────────

def sun_path_year_envelope(
        lat: float,
        lon: float,
        year: Optional[int] = None,
        step_minutes: int = 15,
) -> Dict[str, Any]:
    """
    Three reference arcs that bound the year's sun paths:
      summer solstice, equinox (spring), winter solstice.

    Approximate dates are good enough for floor-plan visualisation.
    Pass ``year`` to pin to a specific year; otherwise the current UTC year
    is used.
    """
    if year is None:
        year = datetime.now(timezone.utc).year

    return {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "year": year,
        "summer_solstice": sun_path_for_day(lat, lon, date(year, 6, 21), step_minutes),
        "equinox": sun_path_for_day(lat, lon, date(year, 3, 20), step_minutes),
        "winter_solstice": sun_path_for_day(lat, lon, date(year, 12, 21), step_minutes),
    }


# ─────────────────────────────── self test ───────────────────────────────

if __name__ == "__main__":
    # Reference: London on 2026-06-21 12:00 UTC. London is at lon -0.128 so
    # solar noon falls about 30 seconds after 12:00 UTC; azimuth should be
    # essentially due south (~178–180°) and elevation ~61.5°.
    p = sun_position(51.5074, -0.1278, datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc))
    print("London midsummer noon:", p)
    assert 175.0 < p["azimuth_deg"] < 182.0, p["azimuth_deg"]
    assert 60.0 < p["elevation_deg"] < 64.0, p["elevation_deg"]

    # And in the morning, sun should be in the east (~90° az):
    p_morn = sun_position(51.5074, -0.1278, datetime(2026, 6, 21, 6, 0, 0, tzinfo=timezone.utc))
    print("London 06:00 UTC midsummer:", p_morn)
    assert 70.0 < p_morn["azimuth_deg"] < 95.0, p_morn["azimuth_deg"]

    rs = sunrise_sunset(51.5074, -0.1278, date(2026, 6, 21))
    print("London midsummer sunrise/sunset:", rs)
    # Astronomical sunrise on 2026-06-21 in London is ~03:43 UTC, sunset ~20:22 UTC
    assert rs["daylight_minutes"] is not None and rs["daylight_minutes"] > 16 * 60

    # Polar test: Tromsø (69.6°N) on summer solstice — sun never sets
    polar = sunrise_sunset(69.6, 18.95, date(2026, 6, 21))
    print("Tromsø midsummer:", polar)
    assert polar["polar"] == "day"
    assert polar["sunrise"] is None and polar["sunset"] is None

    # Polar test: Tromsø in midwinter — sun never rises
    polar2 = sunrise_sunset(69.6, 18.95, date(2026, 12, 21))
    print("Tromsø midwinter:", polar2)
    assert polar2["polar"] == "night"

    # Day arc length sanity
    arc = sun_path_for_day(51.5074, -0.1278, date(2026, 6, 21), step_minutes=30)
    print(f"London day arc: {len(arc['points'])} points; "
          f"daylight={arc['daylight_minutes']} min")
    assert len(arc["points"]) == 48

    # Year envelope smoke
    env = sun_path_year_envelope(51.5074, -0.1278, year=2026, step_minutes=60)
    print("Year envelope sample sizes:",
          len(env["summer_solstice"]["points"]),
          len(env["equinox"]["points"]),
          len(env["winter_solstice"]["points"]))
    print("\n✓ all sun_position self-tests passed")