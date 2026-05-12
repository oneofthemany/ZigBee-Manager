"""
Weather service using Open-Meteo (free, no API key required).
Polls current conditions + hourly forecast and caches locally.
Optionally publishes to MQTT for Home Assistant sensor discovery.

Config (config.yaml):
  weather:
    enabled: true
    latitude: 51.5074
    longitude: -0.1278
    poll_interval_minutes: 30
    mqtt_publish: true          # publish to {base_topic}/weather
"""
import asyncio
import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("modules.weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code → human label
WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


class WeatherService:
    """
    Periodic weather fetcher backed by Open-Meteo.
    No API key required.
    """

    def __init__(self, config: dict, mqtt_service=None):
        self.enabled = config.get("enabled", False)
        self.latitude = config.get("latitude")
        self.longitude = config.get("longitude")
        self.poll_interval = config.get("poll_interval_minutes", 30) * 60
        self.mqtt_publish = config.get("mqtt_publish", False)
        self.mqtt = mqtt_service

        self._current: Optional[Dict[str, Any]] = None
        self._forecast: Optional[Dict[str, Any]] = None
        self._last_fetch: float = 0.0
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current(self) -> Optional[Dict[str, Any]]:
        return self._current

    def get_forecast(self) -> Optional[Dict[str, Any]]:
        return self._forecast

    def get_outdoor_temperature(self) -> Optional[float]:
        """Convenience accessor for the automation engine."""
        if self._current:
            return self._current.get("temperature_2m")
        return None

    def get_solar_irradiance(self) -> Optional[float]:
        """
        Current global horizontal irradiance [W/m²] from Open-Meteo
        ``shortwave_radiation``. Returns None if not yet fetched.

        Pass directly to ``solar_gain.solar_gain_now()`` as ``shortwave_wm2``.
        Zero at night; None means no data yet (treat as unknown, not zero).
        """
        if self._current:
            return self._current.get("shortwave_radiation")
        return None

    def get_cloud_fraction(self) -> Optional[float]:
        """
        Current cloud cover as a fraction 0.0–1.0 (Open-Meteo reports 0–100 %).
        Returns None if not yet fetched.

        Pass to ``solar_gain`` functions as ``cloud_fraction``.
        """
        if self._current:
            cc = self._current.get("cloud_cover")
            if cc is not None:
                return float(cc) / 100.0
        return None

    def get_hourly_solar(self) -> Optional[dict]:
        """
        Hourly shortwave_radiation and cloud_cover arrays from the forecast,
        aligned with ``forecast["times"]``.

        Returns a dict with keys:
            ``times``               — ISO-8601 strings, one per hour
            ``shortwave_radiation`` — W/m² per hour
            ``cloud_fraction``      — 0.0–1.0 per hour (converted from %)

        Returns None if the forecast hasn't been fetched yet.
        Pass to ``solar_gain.solar_gain_forecast()`` as
        ``hourly_shortwave_wm2`` / ``hourly_cloud_fraction``.
        """
        if not self._forecast:
            return None
        raw_cc = self._forecast.get("cloud_cover") or []
        return {
            "times": self._forecast.get("times", []),
            "shortwave_radiation": self._forecast.get("shortwave_radiation", []),
            "cloud_fraction": [float(v) / 100.0 if v is not None else 0.0
                               for v in raw_cc],
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            logger.info("Weather service disabled")
            return
        if not self.latitude or not self.longitude:
            logger.warning("Weather service: latitude/longitude not configured — disabled")
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"Weather service started "
            f"(lat={self.latitude}, lon={self.longitude}, "
            f"interval={self.poll_interval // 60}min)"
        )

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        while True:
            try:
                await self._fetch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Weather fetch failed: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self):
        try:
            import urllib.request
            import json as _json
            import urllib.parse

            params = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "current": ",".join([
                    "temperature_2m",
                    "relative_humidity_2m",
                    "apparent_temperature",
                    "weather_code",
                    "wind_speed_10m",
                    "precipitation",
                    "cloud_cover",
                    "shortwave_radiation",
                ]),
                "hourly": ",".join([
                    "temperature_2m",
                    "precipitation_probability",
                    "weather_code",
                    "shortwave_radiation",
                    "cloud_cover",
                ]),
                "forecast_days": 3,
                "wind_speed_unit": "mph",
                "temperature_unit": "celsius",
                "timezone": "auto",
            }

            url = OPEN_METEO_URL + "?" + urllib.parse.urlencode(params)
            loop = asyncio.get_event_loop()

            def _blocking_get():
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return _json.loads(resp.read().decode())

            data = await loop.run_in_executor(None, _blocking_get)

            current_raw = data.get("current", {})
            code = current_raw.get("weather_code")

            self._current = {
                **current_raw,
                "weather_description": WMO_CODES.get(code, f"Code {code}"),
                "fetched_at": time.time(),
                "latitude": self.latitude,
                "longitude": self.longitude,
            }

            hourly = data.get("hourly", {})
            self._forecast = {
                "times": hourly.get("time", []),
                "temperature_2m": hourly.get("temperature_2m", []),
                "precipitation_probability": hourly.get("precipitation_probability", []),
                "weather_code": hourly.get("weather_code", []),
                "shortwave_radiation": hourly.get("shortwave_radiation", []),
                "cloud_cover": hourly.get("cloud_cover", []),
                "fetched_at": time.time(),
            }

            self._last_fetch = time.time()
            logger.info(
                f"Weather updated: {self._current.get('temperature_2m')}°C, "
                f"{self._current.get('weather_description')}"
            )

            if self.mqtt_publish and self.mqtt:
                await self._publish_mqtt()

            # Persist to telemetry so thermal-profile fits can use real
            # historical outdoor temps instead of a constant proxy.
            try:
                from modules.telemetry_db import write_device_state
                t_c = self._current.get("temperature_2m")
                if isinstance(t_c, (int, float)):
                    write_device_state("__weather__", "outdoor_temperature_c", t_c)
                code = self._current.get("weather_code")
                if isinstance(code, (int, float)):
                    write_device_state("__weather__", "weather_code", int(code))
                hum = self._current.get("relative_humidity_2m")
                if isinstance(hum, (int, float)):
                    write_device_state("__weather__", "humidity_percent", hum)
            except Exception as e:
                logger.debug(f"Weather telemetry write failed: {e}")

        except Exception as e:
            logger.error(f"Weather fetch error: {e}")
            raise

    async def _publish_mqtt(self):
        """Publish current weather to MQTT for HA sensor discovery."""
        if not self._current:
            return
        try:
            import json as _json
            topic = f"{self.mqtt.base_topic}/weather"
            payload = _json.dumps(self._current)
            await self.mqtt.publish(topic, payload, retain=True)
            logger.debug(f"Weather published to {topic}")
        except Exception as e:
            logger.warning(f"Weather MQTT publish failed: {e}")

    def get_ha_discovery_configs(self) -> list:
        """
        Generate HA MQTT Discovery configs for weather sensors.
        Call after MQTT is connected.
        """
        if not self.mqtt:
            return []

        base = self.mqtt.base_topic
        state_topic = f"{base}/weather"
        device_info = {
            "identifiers": ["zmm_weather"],
            "name": "ZMM Weather",
            "model": "Open-Meteo",
            "manufacturer": "open-meteo.com",
        }

        sensors = [
            ("temperature",       "temperature_2m",              "Temperature",       "temperature",        "°C",    "measurement"),
            ("humidity",          "relative_humidity_2m",        "Humidity",          "humidity",           "%",     "measurement"),
            ("apparent_temp",     "apparent_temperature",        "Feels Like",        "temperature",        "°C",    "measurement"),
            ("wind_speed",        "wind_speed_10m",              "Wind Speed",        "wind_speed",         "mph",   "measurement"),
            ("precipitation",     "precipitation",               "Precipitation",     "precipitation",      "mm",    "measurement"),
            ("cloud_cover",       "cloud_cover",                 "Cloud Cover",       None,                 "%",     "measurement"),
            ("shortwave_rad",     "shortwave_radiation",         "Solar Irradiance",  "irradiance",         "W/m²",  "measurement"),
            ("weather_condition", "weather_description",         "Conditions",        "enum",               None,    None),
        ]

        configs = []
        for obj_id, key, name, dev_class, unit, state_class in sensors:
            cfg = {
                "name": name,
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "unique_id": f"zmm_weather_{obj_id}",
                "device": device_info,
            }
            if dev_class and dev_class != "enum":
                cfg["device_class"] = dev_class
            if unit:
                cfg["unit_of_measurement"] = unit
            if state_class:
                cfg["state_class"] = state_class
            configs.append({
                "component": "sensor",
                "object_id": f"weather_{obj_id}",
                "config": cfg,
                "state_topic": state_topic,
            })

        return configs