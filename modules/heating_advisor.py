"""
Heating Advisor — Weather-aware smart heating intelligence.
============================================================
Correlates outdoor weather (from WeatherService), indoor temperatures
(from HVAC devices), and heating demand history (from telemetry_db)
to provide:
  - EPC-style efficiency rating for the property
  - Thermal decay modelling per zone
  - Pre-heat timing recommendations
  - Energy-saving tips based on usage patterns
  - Bill reduction guidance
  - Per-zone analysis when zones are configured

Config (config.yaml):
  heating:
    enabled: true
    property:
      type: semi-detached       # detached, semi-detached, mid-terrace, flat
      age: 1960                 # build year
      insulation: partial       # none, partial, full, cavity_wall
      glazing: double           # single, double, triple
      floor_area_m2: 85
      floors: 2
    tariff:
      type: fixed               # fixed, economy7, agile, variable
      unit_rate_p: 24.5         # pence per kWh
      standing_charge_p: 46.36  # daily standing charge
      off_peak_start: "00:00"   # economy7/agile off-peak window
      off_peak_end: "07:00"
      off_peak_rate_p: 7.5
    boiler:
      type: gas                 # gas, oil, electric, heat_pump
      efficiency_percent: 89    # SEDBUK rating
      output_kw: 24
    comfort:
      min_temp: 18.0            # don't let it drop below
      target_temp: 21.0         # default comfort target
      night_setback: 16.0       # overnight setback
      preheat_max_minutes: 90   # max pre-heat lead time
    zones:
      - id: living_room
        name: "Living Room"
        target_temp: 21.0
        night_setback: 17.0
        min_temp: 16.0
        priority: 5
        devices: ["00:15:8d:00:00:aa:bb:cc"]
        schedule:
          - days: [mon, tue, wed, thu, fri]
            start: "07:00"
            end: "22:00"
            temp: 21.0
"""
import asyncio
import logging
import time
import math
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.heating_advisor")

# ── Thermal constants ──────────────────────────────────────────────
HEAT_LOSS_COEFFICIENTS = {
    ("detached", "none"):       3.0,
    ("detached", "partial"):    2.2,
    ("detached", "full"):       1.4,
    ("detached", "cavity_wall"):1.0,
    ("semi-detached", "none"):  2.5,
    ("semi-detached", "partial"):1.8,
    ("semi-detached", "full"):  1.2,
    ("semi-detached", "cavity_wall"):0.9,
    ("mid-terrace", "none"):    2.0,
    ("mid-terrace", "partial"): 1.5,
    ("mid-terrace", "full"):    1.0,
    ("mid-terrace", "cavity_wall"):0.8,
    ("flat", "none"):           1.8,
    ("flat", "partial"):        1.3,
    ("flat", "full"):            0.9,
    ("flat", "cavity_wall"):     0.7,
}

GLAZING_FACTOR = {"single": 1.3, "double": 1.0, "triple": 0.8}
BOILER_EFFICIENCY = {"gas": 0.89, "oil": 0.85, "electric": 1.0, "heat_pump": 3.0}

# EPC band boundaries (kWh/m²/year)
EPC_BANDS = [
    ("A", 0, 25),    ("B", 25, 50),   ("C", 50, 75),
    ("D", 75, 100),  ("E", 100, 125), ("F", 125, 150),
    ("G", 150, 9999),
]

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── Helpers ────────────────────────────────────────────────────────
def _get_or(d: Optional[dict], key: str, default):
    """
    dict.get-style helper that also substitutes the default when the key
    is present but its value is None. Protects against YAML null values.
    """
    if not isinstance(d, dict):
        return default
    v = d.get(key)
    return default if v is None else v


def _as_float(v, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(v, default: int) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class HeatingAdvisor:
    """Weather-aware heating intelligence engine."""

    def __init__(self, config: dict, weather_service, device_getter: Callable):
        config = config or {}
        self.enabled = bool(_get_or(config, "enabled", False))
        self.weather = weather_service
        self._get_devices = device_getter

        # Property profile
        prop = config.get("property") or {}
        self.prop_type = _get_or(prop, "type", "semi-detached")
        self.prop_age = _as_int(_get_or(prop, "age", 1970), 1970)
        self.insulation = _get_or(prop, "insulation", "partial")
        self.glazing = _get_or(prop, "glazing", "double")
        self.floor_area = _as_float(_get_or(prop, "floor_area_m2", 85), 85.0)
        self.floors = _as_int(_get_or(prop, "floors", 2), 2)

        # Tariff
        tariff = config.get("tariff") or {}
        self.tariff_type = _get_or(tariff, "type", "fixed")
        self.unit_rate = _as_float(_get_or(tariff, "unit_rate_p", 24.5), 24.5) / 100
        self.standing_charge = _as_float(_get_or(tariff, "standing_charge_p", 46.36), 46.36) / 100
        self.off_peak_start = str(_get_or(tariff, "off_peak_start", "00:00"))
        self.off_peak_end = str(_get_or(tariff, "off_peak_end", "07:00"))
        self.off_peak_rate = _as_float(_get_or(tariff, "off_peak_rate_p", 7.5), 7.5) / 100

        # Boiler
        boiler = config.get("boiler") or {}
        self.boiler_type = _get_or(boiler, "type", "gas")
        self.boiler_eff = _as_float(_get_or(boiler, "efficiency_percent", 89), 89.0) / 100
        self.boiler_kw = _as_float(_get_or(boiler, "output_kw", 24), 24.0)

        # Comfort
        comfort = config.get("comfort") or {}
        self.min_temp = _as_float(_get_or(comfort, "min_temp", 18.0), 18.0)
        self.target_temp = _as_float(_get_or(comfort, "target_temp", 21.0), 21.0)
        self.night_setback = _as_float(_get_or(comfort, "night_setback", 16.0), 16.0)
        self.preheat_max = _as_int(_get_or(comfort, "preheat_max_minutes", 90), 90)

        # Zones (optional)
        self.zones = self._clean_zones(config.get("zones") or [])

        # Derived
        key = (self.prop_type, self.insulation)
        self._u_value = HEAT_LOSS_COEFFICIENTS.get(key, 1.8)
        self._glazing_factor = GLAZING_FACTOR.get(self.glazing, 1.0)
        self._total_heat_loss_coeff = (
                self._u_value * self._glazing_factor * self.floor_area
        )  # watts per °C delta

        # Thermal mass estimate (kJ/°C)
        mass_per_m2 = 80 if self.prop_age < 1960 else 60
        self._thermal_mass = mass_per_m2 * self.floor_area

        # Per-zone thermal mass (pro-rated by floor area share; falls back to equal split)
        self._zone_mass_map = self._compute_zone_mass()

        # Learned thermal decay rate
        self._learned_decay_rate: Optional[float] = None

        self._task: Optional[asyncio.Task] = None
        self._last_analysis: Optional[Dict] = None
        self._last_analysis_ts: float = 0

        if self.enabled:
            logger.info(
                f"Heating Advisor: {self.prop_type}, {self.insulation} insulation, "
                f"{self.floor_area}m², U≈{self._u_value}, "
                f"boiler={self.boiler_type} {self.boiler_kw}kW, "
                f"zones={len(self.zones)}"
            )

    # ── Zone normalisation ─────────────────────────────────────────
    def _clean_zones(self, zones: list) -> List[Dict]:
        """Normalise + default zone definitions."""
        if not isinstance(zones, list):
            return []
        out = []
        for z in zones:
            if not isinstance(z, dict) or not z.get("name"):
                continue
            zid = str(z.get("id") or z["name"]).lower().replace(" ", "_")
            devices = z.get("devices") or []
            if not isinstance(devices, list):
                devices = []
            schedule = z.get("schedule") or []
            if not isinstance(schedule, list):
                schedule = []
            clean_schedule = []
            for slot in schedule:
                if not isinstance(slot, dict):
                    continue
                days = slot.get("days") or []
                if not isinstance(days, list):
                    days = []
                clean_schedule.append({
                    "days": [d for d in days if d in DAY_KEYS],
                    "start": str(_get_or(slot, "start", "07:00")),
                    "end": str(_get_or(slot, "end", "22:00")),
                    "temp": _as_float(_get_or(slot, "temp", 20.0), 20.0),
                })
            out.append({
                "id": zid,
                "name": str(z["name"]),
                "target_temp": _as_float(_get_or(z, "target_temp", self_target := 21.0), self_target),
                "night_setback": _as_float(_get_or(z, "night_setback", 17.0), 17.0),
                "min_temp": _as_float(_get_or(z, "min_temp", 16.0), 16.0),
                "priority": _as_int(_get_or(z, "priority", 5), 5),
                "devices": [str(d) for d in devices if d],
                "schedule": clean_schedule,
            })
        return out

    def _compute_zone_mass(self) -> Dict[str, float]:
        """Estimate per-zone thermal mass. Equal split if no area hints."""
        if not self.zones:
            return {}
        share = self._thermal_mass / max(1, len(self.zones))
        return {z["id"]: share for z in self.zones}

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self):
        if not self.enabled:
            logger.info("Heating Advisor disabled")
            return
        self._task = asyncio.create_task(self._analysis_loop())
        logger.info("Heating Advisor started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _analysis_loop(self):
        while True:
            try:
                await asyncio.sleep(5)
                self._last_analysis = self._run_analysis()
                self._last_analysis_ts = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heating analysis failed: {e}", exc_info=True)
            await asyncio.sleep(900)

    # ── Public API ─────────────────────────────────────────────────
    def get_dashboard(self, force: bool = False) -> Dict[str, Any]:
        # Cache at 60s — long enough to dampen accidental spam from the UI
        # but short enough that live values feel live. `force=True` bypasses.
        if (not force) and self._last_analysis and (time.time() - self._last_analysis_ts < 60):
            return self._last_analysis
        try:
            self._last_analysis = self._run_analysis()
            self._last_analysis_ts = time.time()
        except Exception as e:
            logger.error(f"Heating dashboard analysis failed: {e}", exc_info=True)
            # Return a minimal safe payload so the frontend still renders
            return {
                "error": str(e),
                "outdoor": {"temperature": None, "forecast_3h": [], "weather": None},
                "indoor": {"temperatures": {}, "average": None},
                "heating": {"active": False, "avg_demand_percent": 0, "devices": []},
                "epc": {"band": "G", "score": 0, "kwh_per_m2_year": 0,
                        "annual_kwh": 0, "annual_cost_gbp": 0, "degree_days": 0},
                "cost": {"daily_gbp": None, "monthly_gbp": None},
                "preheat": None,
                "zones": [],
                "tips": [{
                    "category": "error", "priority": "high", "icon": "exclamation-triangle",
                    "title": "Analysis error",
                    "detail": f"Heating analysis failed: {e}. Check server logs for details.",
                }],
                "property": {
                    "type": self.prop_type, "age": self.prop_age,
                    "insulation": self.insulation, "glazing": self.glazing,
                    "floor_area_m2": self.floor_area,
                    "boiler": self.boiler_type, "boiler_kw": self.boiler_kw,
                    "boiler_efficiency": self.boiler_eff,
                },
                "tariff": {
                    "type": self.tariff_type,
                    "unit_rate_p": round(self.unit_rate * 100, 1),
                    "off_peak_rate_p": round(self.off_peak_rate * 100, 1),
                },
                "ts": time.time(),
            }
        return self._last_analysis

    def get_preheat_recommendation(self, target_temp: float = None,
                                   target_time: str = None) -> Dict:
        target_temp = target_temp or self.target_temp
        outdoor = self._get_outdoor_temp()
        indoor = self._get_avg_indoor_temp()

        if outdoor is None or indoor is None:
            return {"error": "Insufficient sensor data"}

        minutes = self._calc_preheat_minutes(indoor, target_temp, outdoor)
        return {
            "current_indoor": indoor,
            "current_outdoor": outdoor,
            "target_temp": target_temp,
            "preheat_minutes": min(minutes, self.preheat_max),
            "recommendation": f"Start heating {min(minutes, self.preheat_max)} minutes before needed",
        }

    # ── Core Analysis ──────────────────────────────────────────────
    def _run_analysis(self) -> Dict[str, Any]:
        outdoor = self._get_outdoor_temp()
        forecast = self._get_forecast_temps()

        try:
            hvac_devices = self._find_hvac_devices()
        except Exception as e:
            logger.error(f"Device discovery failed: {e}", exc_info=True)
            hvac_devices = []

        try:
            indoor_temps = self._get_indoor_temps(hvac_devices)
        except Exception as e:
            logger.error(f"Indoor temp extraction failed: {e}", exc_info=True)
            indoor_temps = {}

        avg_indoor = sum(indoor_temps.values()) / len(indoor_temps) if indoor_temps else None

        try:
            epc = self._estimate_epc(outdoor)
        except Exception as e:
            logger.error(f"EPC estimation failed: {e}")
            epc = {"band": "G", "score": 0, "kwh_per_m2_year": 0,
                   "annual_kwh": 0, "annual_cost_gbp": 0, "degree_days": 0}

        heating_active = False
        try:
            heating_active = any(
                (d.get("state") or {}).get("hvac_action") == "heating"
                for d in hvac_devices
            )
        except Exception as e:
            logger.debug(f"heating_active check failed: {e}")

        total_demand = self._get_avg_demand(hvac_devices)
        daily_cost = self._estimate_daily_cost(total_demand, outdoor)
        tips = self._generate_tips(outdoor, avg_indoor, heating_active, epc, forecast)

        # Global pre-heat
        preheat = None
        if outdoor is not None and avg_indoor is not None:
            try:
                ph_mins = self._calc_preheat_minutes(self.night_setback, self.target_temp, outdoor)
                preheat = {
                    "from_temp": self.night_setback,
                    "to_temp": self.target_temp,
                    "outdoor_temp": outdoor,
                    "minutes_needed": min(ph_mins, self.preheat_max),
                }
            except Exception as e:
                logger.debug(f"preheat calc failed: {e}")

        # Zones analysis
        zones_payload = []
        if self.zones:
            try:
                zones_payload = self._analyse_zones(hvac_devices, outdoor)
            except Exception as e:
                logger.error(f"Zone analysis failed: {e}", exc_info=True)

        # Weather current — must be JSON-serialisable
        weather_current = None
        if self.weather:
            try:
                weather_current = self.weather.get_current()
                # Ensure it's a dict (some services may return non-serialisable types)
                if weather_current is not None and not isinstance(weather_current, dict):
                    weather_current = None
            except Exception:
                weather_current = None

        # Build device list defensively
        device_list = []
        for d in hvac_devices:
            try:
                state = d.get("state") or {}
                device_list.append({
                    "ieee": d.get("ieee"),
                    "name": d.get("friendly_name") or d.get("ieee"),
                    "temperature": state.get("local_temperature") or state.get("current_temperature"),
                    "setpoint": state.get("occupied_heating_setpoint") or state.get("target_temp"),
                    "demand": self._best_demand(state),
                    "running": self._best_running(state),
                    "mode": state.get("system_mode", "unknown"),
                    "action": state.get("hvac_action", "unknown"),
                    "zone": self._device_to_zone_id(d.get("ieee")),
                })
            except Exception as e:
                logger.debug(f"Skipping malformed device entry: {e}")

        return {
            "outdoor": {
                "temperature": outdoor,
                "forecast_3h": forecast[:3] if forecast else [],
                "weather": weather_current,
            },
            "indoor": {
                "temperatures": indoor_temps,
                "average": round(avg_indoor, 1) if avg_indoor else None,
            },
            "heating": {
                "active": heating_active,
                "avg_demand_percent": total_demand,
                "devices": device_list,
            },
            "epc": epc,
            "cost": daily_cost,
            "preheat": preheat,
            "zones": zones_payload,
            "tips": tips,
            "property": {
                "type": self.prop_type,
                "age": self.prop_age,
                "insulation": self.insulation,
                "glazing": self.glazing,
                "floor_area_m2": self.floor_area,
                "boiler": self.boiler_type,
                "boiler_kw": self.boiler_kw,
                "boiler_efficiency": self.boiler_eff,
            },
            "tariff": {
                "type": self.tariff_type,
                "unit_rate_p": round(self.unit_rate * 100, 1),
                "off_peak_rate_p": round(self.off_peak_rate * 100, 1),
            },
            "ts": time.time(),
        }

    # ── Zone Analysis ──────────────────────────────────────────────
    def _analyse_zones(self, hvac_devices: List[dict], outdoor: Optional[float]) -> List[Dict]:
        """Produce per-zone analysis: current temps, effective target, demand, pre-heat."""
        by_ieee = {d.get("ieee"): d for d in hvac_devices if d.get("ieee")}
        now = datetime.now()
        now_day = DAY_KEYS[now.weekday()]
        now_minutes = now.hour * 60 + now.minute

        zones_out = []
        for z in self.zones:
            z_devices = []
            temps = []
            setpoints = []
            demands = []
            any_heating = False

            for ieee in z.get("devices", []):
                dev = by_ieee.get(ieee)
                if not dev:
                    # Device not currently visible — include a stub
                    z_devices.append({
                        "ieee": ieee,
                        "name": ieee,
                        "online": False,
                    })
                    continue
                state = dev.get("state", {})
                temp = state.get("local_temperature") or state.get("current_temperature")
                setpoint = state.get("occupied_heating_setpoint") or state.get("target_temp")
                demand = state.get("heating_demand")
                action = state.get("hvac_action") or "unknown"

                if temp is not None:
                    try: temps.append(float(temp))
                    except (TypeError, ValueError): pass
                if setpoint is not None:
                    try: setpoints.append(float(setpoint))
                    except (TypeError, ValueError): pass
                if demand is not None:
                    try: demands.append(float(demand))
                    except (TypeError, ValueError): pass
                if action == "heating":
                    any_heating = True

                z_devices.append({
                    "ieee": ieee,
                    "name": dev.get("friendly_name", ieee),
                    "temperature": temp,
                    "setpoint": setpoint,
                    "demand": demand or 0,
                    "mode": state.get("system_mode", "unknown"),
                    "action": action,
                    "online": True,
                })

            avg_temp = round(sum(temps) / len(temps), 1) if temps else None
            avg_setpoint = round(sum(setpoints) / len(setpoints), 1) if setpoints else None
            avg_demand = round(sum(demands) / len(demands), 0) if demands else 0

            # Effective target — check schedule first, fall back to zone default
            effective_target, source = self._effective_target_for_zone(z, now_day, now_minutes)

            # Status vs target
            if avg_temp is None:
                status = "unknown"
            elif avg_temp < (effective_target - 0.5):
                status = "below"
            elif avg_temp > (effective_target + 0.5):
                status = "above"
            else:
                status = "ontarget"

            # Zone-specific pre-heat estimate
            zone_preheat = None
            if outdoor is not None and avg_temp is not None and avg_temp < effective_target:
                mins = self._calc_preheat_minutes(
                    start_temp=avg_temp,
                    target_temp=effective_target,
                    outdoor_temp=outdoor,
                    zone_id=z["id"],
                )
                zone_preheat = min(mins, self.preheat_max)

            zones_out.append({
                "id": z["id"],
                "name": z["name"],
                "target_temp": effective_target,
                "target_source": source,        # "schedule" | "default" | "setback"
                "configured_target": z["target_temp"],
                "night_setback": z["night_setback"],
                "min_temp": z["min_temp"],
                "priority": z.get("priority", 5),
                "current_temp": avg_temp,
                "avg_setpoint": avg_setpoint,
                "avg_demand_percent": avg_demand,
                "heating_active": any_heating,
                "status": status,               # below | ontarget | above | unknown
                "preheat_minutes": zone_preheat,
                "device_count": len(z.get("devices", [])),
                "devices": z_devices,
            })

        # Sort by priority desc (higher priority first), then by name
        zones_out.sort(key=lambda z: (-z.get("priority", 5), z["name"]))
        return zones_out

    def _effective_target_for_zone(self, zone: dict, day: str,
                                   now_minutes: int) -> Tuple[float, str]:
        """
        Find the active schedule slot; fall back to zone default, using setback overnight.
        Returns (temp, source).
        """
        for slot in zone.get("schedule", []):
            if day not in (slot.get("days") or []):
                continue
            start_m = self._parse_hhmm(slot.get("start", "00:00"))
            end_m = self._parse_hhmm(slot.get("end", "23:59"))
            if start_m is None or end_m is None:
                continue
            # Overnight slot support (end < start)
            in_slot = (start_m <= now_minutes < end_m) if start_m <= end_m \
                else (now_minutes >= start_m or now_minutes < end_m)
            if in_slot:
                return float(slot.get("temp", zone["target_temp"])), "schedule"

        # No active slot — apply night setback during typical overnight window (22:00–06:00)
        if now_minutes >= 22 * 60 or now_minutes < 6 * 60:
            return float(zone.get("night_setback", self.night_setback)), "setback"

        return float(zone.get("target_temp", self.target_temp)), "default"

    @staticmethod
    def _parse_hhmm(s: str) -> Optional[int]:
        try:
            hh, mm = str(s).split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return None

    def _device_to_zone_id(self, ieee: Optional[str]) -> Optional[str]:
        if not ieee:
            return None
        for z in self.zones:
            if ieee in (z.get("devices") or []):
                return z["id"]
        return None

    # ── Tips Engine ────────────────────────────────────────────────
    def _generate_tips(self, outdoor: Optional[float], indoor: Optional[float],
                       heating_active: bool, epc: Dict,
                       forecast: List) -> List[Dict]:
        tips = []

        if indoor and indoor > self.target_temp + 1:
            saving = round((indoor - self.target_temp) * 0.03 * epc.get("annual_cost_gbp", 0), 0)
            tips.append({
                "category": "temperature",
                "priority": "high",
                "icon": "thermometer-half",
                "title": "Room is over-heating",
                "detail": f"Indoor temp is {indoor}°C vs target {self.target_temp}°C. "
                          f"Each 1°C reduction saves ~3% on bills (~£{saving}/year).",
            })

        if outdoor is not None and outdoor > 15 and heating_active:
            tips.append({
                "category": "weather",
                "priority": "high",
                "icon": "sun",
                "title": "Mild weather — consider turning heating off",
                "detail": f"It's {outdoor}°C outside. Natural warmth and solar gain "
                          f"may be sufficient today.",
            })

        if forecast:
            min_forecast = min(forecast[:6]) if len(forecast) >= 6 else None
            if min_forecast is not None and min_forecast < 2:
                tips.append({
                    "category": "weather",
                    "priority": "medium",
                    "icon": "snowflake",
                    "title": "Cold temperatures forecast",
                    "detail": f"Temperature dropping to {min_forecast}°C in coming hours. "
                              f"Consider pre-heating now to avoid demand spike.",
                })

        if epc.get("band", "G") in ("E", "F", "G"):
            tips.append({
                "category": "insulation",
                "priority": "high",
                "icon": "home",
                "title": "Insulation upgrade recommended",
                "detail": f"EPC band {epc['band']} indicates high heat loss. "
                          f"Cavity wall or loft insulation could save 20-40% on heating costs.",
            })

        if self.glazing == "single":
            tips.append({
                "category": "glazing",
                "priority": "medium",
                "icon": "border-all",
                "title": "Upgrade to double glazing",
                "detail": "Single glazing loses ~2x more heat than double. "
                          "Secondary glazing is a lower-cost alternative.",
            })

        if heating_active:
            now_hour = datetime.now().hour
            if now_hour >= 22 or now_hour < 6:
                tips.append({
                    "category": "schedule",
                    "priority": "medium",
                    "icon": "moon",
                    "title": "Night setback active hours",
                    "detail": f"Reducing to {self.night_setback}°C overnight saves ~10% "
                              f"on heating bills. Set a schedule to automate this.",
                })

        if self.tariff_type in ("economy7", "agile"):
            tips.append({
                "category": "tariff",
                "priority": "low",
                "icon": "bolt",
                "title": "Off-peak heating available",
                "detail": f"Your {self.tariff_type} tariff has cheap rates "
                          f"{self.off_peak_start}–{self.off_peak_end}. "
                          f"Consider pre-heating during off-peak (storage heater effect).",
            })

        if self.boiler_eff < 0.9 and self.boiler_type == "gas":
            tips.append({
                "category": "boiler",
                "priority": "low",
                "icon": "fire",
                "title": "Boiler efficiency could improve",
                "detail": f"Your boiler is rated at {int(self.boiler_eff*100)}% efficiency. "
                          f"Modern condensing boilers achieve 92-94%, saving ~£150/year.",
            })

        if self.boiler_type in ("gas", "oil") and epc.get("band", "G") in ("A", "B", "C"):
            tips.append({
                "category": "heat_pump",
                "priority": "low",
                "icon": "leaf",
                "title": "Heat pump candidate",
                "detail": "Your property's insulation level is suitable for a heat pump. "
                          "COP of 3.0 means 3x the heat per kWh vs gas. "
                          "BUS grant covers £7,500 of installation cost.",
            })

        # Zone-aware tips
        if self.zones:
            cold_zones = []
            for z in self.zones:
                # Reuse cached analysis if available
                pass  # (zone tips derived at analysis time if desired)

            # Unassigned devices tip
            hvac = self._find_hvac_devices()
            assigned = set()
            for z in self.zones:
                assigned.update(z.get("devices") or [])
            unassigned = [d for d in hvac if d.get("ieee") not in assigned]
            if unassigned and len(self.zones) > 0:
                tips.append({
                    "category": "zones",
                    "priority": "medium",
                    "icon": "layer-group",
                    "title": f"{len(unassigned)} heating device(s) not in a zone",
                    "detail": "Unassigned devices aren't included in zone-level "
                              "analysis or scheduling. Assign them in Heating Settings → Zones.",
                })

        return tips

    # ── Device Discovery ───────────────────────────────────────────
    def _find_hvac_devices(self) -> List[Dict]:
        """
        Find HVAC-capable devices. Tolerant of:
          - dict-of-dicts: {ieee: {"state": {...}, "friendly_name": "..."}}
          - dict-of-objects: {ieee: ZigManDevice} — extracts .state, .friendly_name
        Returns a normalised list of plain dicts with "state", "ieee", "friendly_name".
        """
        try:
            raw = self._get_devices() or {}
        except Exception as e:
            logger.error(f"device_getter raised: {e}", exc_info=True)
            return []

        hvac = []
        for ieee, dev in raw.items():
            # Extract state — support both dict and object
            if isinstance(dev, dict):
                state = dev.get("state") or {}
                friendly_name = dev.get("friendly_name") or dev.get("name") or ieee
                manufacturer = dev.get("manufacturer")
                model = dev.get("model")
            else:
                # Assume object with attributes (e.g. ZigManDevice)
                state = getattr(dev, "state", None) or {}
                friendly_name = (
                        getattr(dev, "friendly_name", None)
                        or getattr(dev, "name", None)
                        or ieee
                )
                manufacturer = getattr(dev, "manufacturer", None)
                model = getattr(dev, "model", None)

            if not isinstance(state, dict):
                continue

            if any(k in state for k in (
                    "local_temperature", "current_temperature",
                    "occupied_heating_setpoint", "system_mode",
                    "heating_demand", "hvac_action"
            )):
                hvac.append({
                    "ieee": str(ieee),
                    "state": state,
                    "friendly_name": str(friendly_name),
                    "manufacturer": manufacturer,
                    "model": model,
                })
        return hvac

    def _get_indoor_temps(self, hvac_devices: List[Dict]) -> Dict[str, float]:
        temps = {}
        for d in hvac_devices:
            state = d.get("state", {})
            temp = state.get("local_temperature") or state.get("current_temperature")
            if temp is not None:
                try:
                    name = d.get("friendly_name") or d.get("ieee", "unknown")
                    temps[name] = float(temp)
                except (TypeError, ValueError):
                    pass
        return temps

    def _get_avg_indoor_temp(self) -> Optional[float]:
        hvac = self._find_hvac_devices()
        temps = self._get_indoor_temps(hvac)
        if not temps:
            return None
        return sum(temps.values()) / len(temps)

    def _get_outdoor_temp(self) -> Optional[float]:
        if self.weather:
            try:
                return self.weather.get_outdoor_temperature()
            except Exception as e:
                logger.debug(f"Outdoor temp fetch failed: {e}")
        return None

    def _get_forecast_temps(self) -> List[float]:
        if not self.weather:
            return []
        try:
            fc = self.weather.get_forecast()
        except Exception:
            return []
        if not fc:
            return []
        return fc.get("temperature_2m", [])[:24]

    @staticmethod
    def _best_demand(state: Dict[str, Any]) -> float:
        """
        Percent heat-demand, in order of authoritative → inferred.

        - heating_demand / pi_heating_demand: explicit % from the device.
        - running_state bit 0 or hvac_action=='heating': the device reports
          it is actively calling for heat → 100%.
        - Anything else (including system_mode='heat' while idle) → 0%.
        """
        for k in ("heating_demand", "pi_heating_demand"):
            v = state.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        rs = state.get("running_state")
        if isinstance(rs, (int, float)) and int(rs) & 0x0001:
            return 100.0
        if isinstance(rs, str) and "heat" in rs.lower():
            return 100.0
        if state.get("hvac_action") == "heating":
            return 100.0
        return 0.0

    @staticmethod
    def _best_running(state: Dict[str, Any]) -> bool:
        rs = state.get("running_state")
        if isinstance(rs, (int, float)) and int(rs) & 0x0001:
            return True
        if isinstance(rs, str) and "heat" in rs.lower():
            return True
        return state.get("hvac_action") == "heating"

    def _get_avg_demand(self, hvac_devices: List[Dict]) -> float:
        demands = [self._best_demand(d.get("state", {})) for d in hvac_devices]
        demands = [x for x in demands if x is not None]
        return round(sum(demands) / len(demands), 0) if demands else 0

    # ── EPC Estimation ─────────────────────────────────────────────
    def _estimate_epc(self, outdoor_temp: Optional[float]) -> Dict:
        degree_days = 2200
        if outdoor_temp is not None:
            seasonal_bias = max(0, 15.5 - outdoor_temp) / 15.5
            degree_days = int(2200 * (0.8 + 0.4 * seasonal_bias))

        annual_kwh = (self._total_heat_loss_coeff * degree_days * 24 / 1000)
        annual_fuel_kwh = annual_kwh / max(0.01, self.boiler_eff)
        kwh_per_m2 = annual_fuel_kwh / max(1, self.floor_area)

        band = "G"
        for b, low, high in EPC_BANDS:
            if low <= kwh_per_m2 < high:
                band = b
                break

        score = max(1, min(100, int(100 - kwh_per_m2 * 0.6)))
        annual_cost = annual_fuel_kwh * self.unit_rate + self.standing_charge * 365

        return {
            "band": band,
            "score": score,
            "kwh_per_m2_year": round(kwh_per_m2, 0),
            "annual_kwh": round(annual_fuel_kwh, 0),
            "annual_cost_gbp": round(annual_cost, 0),
            "degree_days": degree_days,
        }

    # ── Pre-heat Calculation ───────────────────────────────────────
    def _calc_preheat_minutes(self, start_temp: float, target_temp: float,
                              outdoor_temp: float, zone_id: Optional[str] = None) -> int:
        """
        Minutes needed to reach target_temp from start_temp.
        If zone_id is given, uses the zone's pro-rated thermal mass.
        """
        delta_t = target_temp - start_temp
        if delta_t <= 0:
            return 0

        avg_delta_outdoor = ((start_temp + target_temp) / 2) - outdoor_temp
        # For zone pre-heat, scale the loss coefficient by mass share too
        if zone_id and zone_id in self._zone_mass_map:
            mass_share = self._zone_mass_map[zone_id] / max(1, self._thermal_mass)
            loss_coeff = self._total_heat_loss_coeff * mass_share
            thermal_mass = self._zone_mass_map[zone_id]
            # Assume zone receives its share of boiler capacity
            boiler_watts = (self.boiler_kw * 1000 * self.boiler_eff) * mass_share
        else:
            loss_coeff = self._total_heat_loss_coeff
            thermal_mass = self._thermal_mass
            boiler_watts = self.boiler_kw * 1000 * self.boiler_eff

        heat_loss_watts = loss_coeff * max(0, avg_delta_outdoor)
        net_watts = boiler_watts - heat_loss_watts
        if net_watts <= 0:
            return self.preheat_max

        energy_kj = thermal_mass * delta_t
        seconds = energy_kj * 1000 / net_watts
        minutes = int(math.ceil(seconds / 60))
        return max(1, minutes)

    # ── Cost Estimation ────────────────────────────────────────────
    def _estimate_daily_cost(self, avg_demand: float, outdoor: Optional[float]) -> Dict:
        if outdoor is None:
            return {"daily_gbp": None, "monthly_gbp": None}

        heating_hours = (avg_demand / 100) * 16 if avg_demand else 0
        daily_kwh = self.boiler_kw * self.boiler_eff * heating_hours
        daily_cost = daily_kwh * self.unit_rate + self.standing_charge

        return {
            "daily_gbp": round(daily_cost, 2),
            "monthly_gbp": round(daily_cost * 30, 0),
            "daily_kwh": round(daily_kwh, 1),
            "heating_hours": round(heating_hours, 1),
        }

    # ── Historical Analysis ────────────────────────────────────────
    def get_heating_history(self, hours: int = 24) -> Dict:
        """
        Telemetry history per HVAC device. Tries several attribute names for
        each conceptual series and returns whichever has data — so Hive SLRs
        (running_state), Aqara TRVs (pi_heating_demand) and generic thermostats
        (heating_demand) all populate correctly.
        """
        try:
            from modules.telemetry_db import query_device_state_history
            hvac = self._find_hvac_devices()
            history = {}

            def _pick(ieee, keys):
                for k in keys:
                    try:
                        data = query_device_state_history(ieee, k, hours) or []
                    except Exception as e:
                        logger.debug(f"history[{ieee}.{k}] failed: {e}")
                        continue
                    if data:
                        return {"series": data, "attribute": k}
                return {"series": [], "attribute": None}

            for d in hvac:
                ieee = d.get("ieee")
                name = d.get("friendly_name") or ieee
                temp = _pick(ieee, ["local_temperature", "current_temperature",
                                    "temperature", "internal_temperature"])
                setp = _pick(ieee, ["occupied_heating_setpoint",
                                    "heating_setpoint", "temperature_setpoint"])
                dem = _pick(ieee, ["heating_demand", "pi_heating_demand",
                                   "running_state"])
                # running_state is a ZCL bitmap, not a percentage. Map
                # bit 0 (HEAT) → 100% so it renders consistently with the
                # demand series from other devices.
                if dem["attribute"] == "running_state":
                    for pt in dem["series"]:
                        nv = pt.get("numeric_val")
                        if nv is not None:
                            pt["numeric_val"] = 100.0 if (int(nv) & 0x0001) else 0.0
                        else:
                            # Fall back to parsing the string value
                            try:
                                pt["numeric_val"] = 100.0 if (int(pt.get("value", 0)) & 0x0001) else 0.0
                            except (TypeError, ValueError):
                                pt["numeric_val"] = 0.0
                history[name] = {
                    "ieee": ieee,
                    "temperature": temp["series"],
                    "setpoint": setp["series"],
                    "demand": dem["series"],
                    "sources": {
                        "temperature": temp["attribute"],
                        "setpoint": setp["attribute"],
                        "demand": dem["attribute"],
                    },
                }
            return {"devices": history, "hours": hours}
        except Exception as e:
            logger.error(f"Failed to query heating history: {e}", exc_info=True)
            return {"devices": {}, "hours": hours, "error": str(e)}

    def get_daily_runtime(self, hours: int = 24) -> Dict[str, Any]:
        """
        Compute per-device heating on-time as a percentage of the window.
        Uses whichever telemetry series that device actually records:
          heating_demand / pi_heating_demand (>0 = on)
          running_state (bit 0 = on)
        Returns: { ieee: { name, on_minutes, percent, source } }
        """
        try:
            from modules.telemetry_db import query_device_state_history
        except Exception as e:
            logger.error(f"telemetry_db import failed: {e}")
            return {}

        hvac = self._find_hvac_devices()
        window_seconds = max(60, int(hours) * 3600)
        out = {}

        def _as_on(attr: str, numeric_val, raw_value) -> bool:
            if numeric_val is not None:
                try:
                    n = float(numeric_val)
                except (TypeError, ValueError):
                    n = None
            else:
                n = None
            if n is None:
                try:
                    n = float(raw_value)
                except (TypeError, ValueError):
                    return False
            if attr == "running_state":
                return bool(int(n) & 0x0001)
            return n > 0.0

        for d in hvac:
            ieee = d.get("ieee")
            name = d.get("friendly_name") or ieee
            series = None
            chosen = None
            for attr in ("heating_demand", "pi_heating_demand", "running_state"):
                try:
                    data = query_device_state_history(ieee, attr, hours) or []
                except Exception:
                    data = []
                if data:
                    series = data
                    chosen = attr
                    break

            if not series:
                out[ieee] = {
                    "name": name, "on_seconds": 0,
                    "percent": 0.0, "source": None,
                }
                continue

            # Integrate time-on across the window using step functions between
            # successive samples. ts is a DuckDB datetime — already UTC.
            import datetime as _dt
            on_seconds = 0.0
            for i, pt in enumerate(series):
                is_on = _as_on(chosen, pt.get("numeric_val"), pt.get("value"))
                if not is_on:
                    continue
                t0 = pt["ts"]
                t1 = series[i + 1]["ts"] if i + 1 < len(series) else _dt.datetime.now(t0.tzinfo)
                dt = (t1 - t0).total_seconds()
                if dt > 0:
                    on_seconds += min(dt, window_seconds)

            pct = 100.0 * on_seconds / window_seconds
            out[ieee] = {
                "name": name,
                "on_seconds": int(on_seconds),
                "percent": round(max(0.0, min(100.0, pct)), 1),
                "source": chosen,
            }

        return out