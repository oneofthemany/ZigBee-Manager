"""
Weather API routes.
"""
import logging
from fastapi import FastAPI

logger = logging.getLogger("routes.weather")


def register_weather_routes(app: FastAPI, get_weather_service):

    @app.get("/api/weather/current")
    async def get_current_weather():
        svc = get_weather_service()
        if not svc or not svc.enabled:
            return {"success": False, "error": "Weather service not enabled"}
        data = svc.get_current()
        if not data:
            return {"success": False, "error": "No weather data yet — fetch pending"}
        return {"success": True, "data": data}

    @app.get("/api/weather/forecast")
    async def get_forecast():
        svc = get_weather_service()
        if not svc or not svc.enabled:
            return {"success": False, "error": "Weather service not enabled"}
        data = svc.get_forecast()
        if not data:
            return {"success": False, "error": "No forecast data yet"}
        return {"success": True, "data": data}

    @app.post("/api/weather/refresh")
    async def refresh_weather():
        """Force an immediate weather refresh."""
        svc = get_weather_service()
        if not svc or not svc.enabled:
            return {"success": False, "error": "Weather service not enabled"}
        try:
            await svc._fetch()
            return {"success": True, "data": svc.get_current()}
        except Exception as e:
            return {"success": False, "error": str(e)}