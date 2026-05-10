"""
Sun position API routes.

Computes sun azimuth/elevation, sunrise/sunset, day-long arcs and yearly
envelopes locally (no network). When ``lat``/``lon`` are not supplied, falls
back to the values configured on the ``WeatherService``.

All endpoints return ``{"success": bool, "data": ..., "error": ...}`` to match
the rest of the heating/weather API shape.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Query

from modules.sun_position import (
    sun_position,
    sunrise_sunset,
    sun_path_for_day,
    sun_path_year_envelope,
)

logger = logging.getLogger("routes.sun")


def _resolve_latlon(
        lat: Optional[float],
        lon: Optional[float],
        get_weather_service,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Use query params if given; otherwise fall back to WeatherService config."""
    if lat is not None and lon is not None:
        return lat, lon, None
    svc = get_weather_service() if get_weather_service else None
    if svc and svc.latitude is not None and svc.longitude is not None:
        return float(svc.latitude), float(svc.longitude), None
    return None, None, "lat/lon required (not supplied and weather service has none configured)"


def register_sun_routes(app: FastAPI, get_weather_service=None):

    @app.get("/api/sun/position")
    async def get_sun_position(
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            ts: Optional[str] = Query(None, description="ISO-8601 UTC timestamp; defaults to now"),
    ):
        """
        Sun azimuth and elevation at a single instant.

        Query params:
            lat, lon  — optional; default to weather service config
            ts        — optional ISO-8601 timestamp (UTC); defaults to now
        """
        lat_v, lon_v, err = _resolve_latlon(lat, lon, get_weather_service)
        if err:
            return {"success": False, "error": err}
        try:
            data = sun_position(lat_v, lon_v, ts)
            return {"success": True, "data": data}
        except (ValueError, TypeError) as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/sun/sunrise-sunset")
    async def get_sunrise_sunset(
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD); defaults to today UTC"),
    ):
        """Sunrise, sunset, and solar noon for a given UTC date."""
        lat_v, lon_v, err = _resolve_latlon(lat, lon, get_weather_service)
        if err:
            return {"success": False, "error": err}
        try:
            data = sunrise_sunset(lat_v, lon_v, date)
            return {"success": True, "data": data}
        except (ValueError, TypeError) as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/sun/day")
    async def get_sun_day(
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD); defaults to today UTC"),
            step_minutes: int = Query(15, ge=1, le=60),
            daylight_only: bool = Query(False),
    ):
        """
        Sun path sampled across a UTC day. Each point is `{ts, az, el}`.

        Use this to overlay the sun's track on the floor plan.
        """
        lat_v, lon_v, err = _resolve_latlon(lat, lon, get_weather_service)
        if err:
            return {"success": False, "error": err}
        try:
            data = sun_path_for_day(lat_v, lon_v, date, step_minutes, daylight_only)
            return {"success": True, "data": data}
        except (ValueError, TypeError) as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/sun/year")
    async def get_sun_year(
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            year: Optional[int] = Query(None, ge=1900, le=2100),
            step_minutes: int = Query(30, ge=1, le=60),
    ):
        """
        Three reference sun paths bounding the year:
        summer solstice, equinox (vernal), winter solstice.
        Used to draw a sun-path envelope on the floor plan.
        """
        lat_v, lon_v, err = _resolve_latlon(lat, lon, get_weather_service)
        if err:
            return {"success": False, "error": err}
        try:
            data = sun_path_year_envelope(lat_v, lon_v, year, step_minutes)
            return {"success": True, "data": data}
        except (ValueError, TypeError) as e:
            return {"success": False, "error": str(e)}