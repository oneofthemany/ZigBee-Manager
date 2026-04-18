"""
Heating Advisor API routes.

Endpoints:
  GET  /api/heating/dashboard           — live dashboard payload
  GET  /api/heating/preheat             — on-demand pre-heat recommendation
  GET  /api/heating/history             — 24h+ history from telemetry_db
  GET  /api/heating/tips                — just the tips from dashboard

  GET  /api/heating/config              — full heating config (property/tariff/boiler/comfort/zones)
  POST /api/heating/config              — save heating config (merges into config.yaml)

  GET  /api/heating/zones               — list of zones
  POST /api/heating/zones               — replace the zones list
  POST /api/heating/zones/{zone_id}     — create/update a single zone
  DELETE /api/heating/zones/{zone_id}   — delete a single zone

  GET  /api/heating/thermostats         — HVAC-capable devices available for zone assignment
"""
import logging
import os
import yaml
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException

logger = logging.getLogger("routes.heating")

CONFIG_PATH = "./config/config.yaml"

# ─── Schema defaults ───────────────────────────────────────────────
_PROPERTY_DEFAULTS = {
    "type": "semi-detached",
    "age": 1970,
    "insulation": "partial",
    "glazing": "double",
    "floor_area_m2": 85,
    "floors": 2,
}
_TARIFF_DEFAULTS = {
    "type": "fixed",
    "unit_rate_p": 24.5,
    "standing_charge_p": 46.36,
    "off_peak_start": "00:00",
    "off_peak_end": "07:00",
    "off_peak_rate_p": 7.5,
}
_BOILER_DEFAULTS = {
    "type": "gas",
    "efficiency_percent": 89,
    "output_kw": 24,
}
_COMFORT_DEFAULTS = {
    "min_temp": 18.0,
    "target_temp": 21.0,
    "night_setback": 16.0,
    "preheat_max_minutes": 90,
}

_PROPERTY_TYPES = {"detached", "semi-detached", "mid-terrace", "flat"}
_INSULATION = {"none", "partial", "full", "cavity_wall"}
_GLAZING = {"single", "double", "triple"}
_BOILER_TYPES = {"gas", "oil", "electric", "heat_pump"}
_TARIFF_TYPES = {"fixed", "economy7", "agile", "variable"}


# ─── YAML helpers ──────────────────────────────────────────────────
def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _with_defaults(d: Optional[dict], defaults: dict) -> dict:
    out = dict(defaults)
    if isinstance(d, dict):
        out.update({k: v for k, v in d.items() if v is not None})
    return out


# ─── Validation / coercion ─────────────────────────────────────────
def _coerce_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clean_property(p: dict) -> dict:
    out = dict(_PROPERTY_DEFAULTS)
    if not isinstance(p, dict):
        return out
    t = str(p.get("type", out["type"])).lower()
    out["type"] = t if t in _PROPERTY_TYPES else out["type"]
    out["age"] = _coerce_int(p.get("age"), out["age"])
    ins = str(p.get("insulation", out["insulation"])).lower()
    out["insulation"] = ins if ins in _INSULATION else out["insulation"]
    gl = str(p.get("glazing", out["glazing"])).lower()
    out["glazing"] = gl if gl in _GLAZING else out["glazing"]
    out["floor_area_m2"] = _coerce_int(p.get("floor_area_m2"), out["floor_area_m2"])
    out["floors"] = _coerce_int(p.get("floors"), out["floors"])
    return out


def _clean_tariff(t: dict) -> dict:
    out = dict(_TARIFF_DEFAULTS)
    if not isinstance(t, dict):
        return out
    typ = str(t.get("type", out["type"])).lower()
    out["type"] = typ if typ in _TARIFF_TYPES else out["type"]
    out["unit_rate_p"] = _coerce_float(t.get("unit_rate_p"), out["unit_rate_p"])
    out["standing_charge_p"] = _coerce_float(t.get("standing_charge_p"), out["standing_charge_p"])
    out["off_peak_rate_p"] = _coerce_float(t.get("off_peak_rate_p"), out["off_peak_rate_p"])
    if t.get("off_peak_start"):
        out["off_peak_start"] = str(t["off_peak_start"])
    if t.get("off_peak_end"):
        out["off_peak_end"] = str(t["off_peak_end"])
    return out


def _clean_boiler(b: dict) -> dict:
    out = dict(_BOILER_DEFAULTS)
    if not isinstance(b, dict):
        return out
    typ = str(b.get("type", out["type"])).lower()
    out["type"] = typ if typ in _BOILER_TYPES else out["type"]
    out["efficiency_percent"] = _coerce_int(b.get("efficiency_percent"), out["efficiency_percent"])
    out["output_kw"] = _coerce_float(b.get("output_kw"), out["output_kw"])
    # Clamp
    out["efficiency_percent"] = max(1, min(400, out["efficiency_percent"]))  # 400 allows heat pump COP
    return out


def _clean_comfort(c: dict) -> dict:
    out = dict(_COMFORT_DEFAULTS)
    if not isinstance(c, dict):
        return out
    out["min_temp"] = _coerce_float(c.get("min_temp"), out["min_temp"])
    out["target_temp"] = _coerce_float(c.get("target_temp"), out["target_temp"])
    out["night_setback"] = _coerce_float(c.get("night_setback"), out["night_setback"])
    out["preheat_max_minutes"] = _coerce_int(c.get("preheat_max_minutes"), out["preheat_max_minutes"])
    # Sanity: min <= setback <= target
    if out["night_setback"] > out["target_temp"]:
        out["night_setback"] = out["target_temp"]
    if out["min_temp"] > out["night_setback"]:
        out["min_temp"] = out["night_setback"]
    return out


def _slugify(s: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return slug or "zone"


def _clean_zone(z: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(z, dict) or not z.get("name"):
        return None
    existing_ids = existing_ids or set()

    zid = str(z.get("id") or _slugify(z["name"]))
    # de-dup id
    base = zid
    n = 2
    while zid in existing_ids:
        zid = f"{base}_{n}"
        n += 1

    devices = z.get("devices") or []
    if not isinstance(devices, list):
        devices = []
    devices = [str(d) for d in devices if d]

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
            "days": [d for d in days if d in
                     ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
            "start": str(slot.get("start", "07:00")),
            "end": str(slot.get("end", "22:00")),
            "temp": _coerce_float(slot.get("temp"), 20.0),
        })

    return {
        "id": zid,
        "name": str(z["name"]),
        "target_temp": _coerce_float(z.get("target_temp"), 21.0),
        "night_setback": _coerce_float(z.get("night_setback"), 17.0),
        "min_temp": _coerce_float(z.get("min_temp"), 16.0),
        "priority": _coerce_int(z.get("priority"), 5),  # 1-10, higher = more important
        "devices": devices,
        "schedule": clean_schedule,
    }


def _clean_zones(zones: List[dict]) -> List[dict]:
    if not isinstance(zones, list):
        return []
    result = []
    seen_ids = set()
    for z in zones:
        cleaned = _clean_zone(z, seen_ids)
        if cleaned:
            result.append(cleaned)
            seen_ids.add(cleaned["id"])
    return result


# ─── HVAC device discovery (mirrors heating_advisor._find_hvac_devices) ──
def _find_thermostats(devices: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Find HVAC-capable devices. Tolerant of:
      - dict-of-dicts: {ieee: {"state": {...}, "friendly_name": "..."}}
      - dict-of-objects: {ieee: ZigManDevice} — extracts .state, .friendly_name, etc.
    """
    out = []
    for ieee, dev in (devices or {}).items():
        # Extract fields — support both dict and object
        if isinstance(dev, dict):
            state = dev.get("state") or {}
            friendly = dev.get("friendly_name") or dev.get("name")
            manufacturer = dev.get("manufacturer")
            model = dev.get("model")
        else:
            state = getattr(dev, "state", None) or {}
            friendly = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
            manufacturer = getattr(dev, "manufacturer", None)
            model = getattr(dev, "model", None)
            # ZigbeeService stores friendly names on the service, try that
            if not friendly:
                service = getattr(dev, "service", None)
                if service is not None:
                    fn_map = getattr(service, "friendly_names", None) or {}
                    friendly = fn_map.get(str(ieee))

        if not isinstance(state, dict):
            continue

        if any(k in state for k in (
                "local_temperature", "current_temperature",
                "occupied_heating_setpoint", "system_mode",
                "heating_demand", "hvac_action"
        )):
            out.append({
                "ieee": str(ieee),
                "name": friendly or str(ieee),
                "manufacturer": manufacturer,
                "model": model,
                "temperature": state.get("local_temperature") or state.get("current_temperature"),
                "setpoint": state.get("occupied_heating_setpoint"),
                "mode": state.get("system_mode"),
                "action": state.get("hvac_action"),
            })
    return out


# ═══════════════════════════════════════════════════════════════════
def register_heating_routes(app: FastAPI, get_heating_advisor, get_zigbee_service=None):
    """
    Register heating routes.

    Args:
        app: FastAPI app
        get_heating_advisor: callable returning the HeatingAdvisor instance
        get_zigbee_service: optional callable returning the zigbee service
                            (used for thermostat device listing)
    """

    def _resolve_advisor():
        """Unwrap advisor; tolerates `lambda: advisor_ref` or `lambda: lambda: advisor_ref`."""
        adv = get_heating_advisor()
        # Defensive: if someone wired `lambda: get_heating_advisor` by mistake
        if callable(adv):
            try:
                adv = adv()
            except Exception:
                pass
        return adv

    # ═════════ Dashboard / analysis ═════════
    @app.get("/api/heating/dashboard")
    async def heating_dashboard(force: int = 0):
        adv = _resolve_advisor()
        if not adv or not getattr(adv, "enabled", False):
            return {"success": False, "error": "Heating advisor not enabled"}
        try:
            return {"success": True, "data": adv.get_dashboard(force=bool(force))}
        except Exception as e:
            logger.error(f"Dashboard endpoint failed: {e}", exc_info=True)
            return {"success": False, "error": f"Dashboard generation failed: {e}"}

    @app.get("/api/heating/preheat")
    async def preheat_recommendation(target_temp: float = None, target_time: str = None):
        adv = _resolve_advisor()
        if not adv or not getattr(adv, "enabled", False):
            return {"success": False, "error": "Heating advisor not enabled"}
        try:
            return {"success": True, "data": adv.get_preheat_recommendation(target_temp, target_time)}
        except Exception as e:
            logger.error(f"Preheat endpoint failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/history")
    async def heating_history(hours: int = 24):
        adv = _resolve_advisor()
        if not adv or not getattr(adv, "enabled", False):
            return {"success": False, "error": "Heating advisor not enabled"}
        try:
            return {"success": True, "data": adv.get_heating_history(hours)}
        except Exception as e:
            logger.error(f"History endpoint failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


    @app.get("/api/heating/runtime")
    async def heating_runtime(hours: int = 24):
        adv = _resolve_advisor()
        if not adv or not getattr(adv, "enabled", False):
            return {"success": False, "error": "Heating advisor not enabled"}
        try:
            return {
                "success": True,
                "hours": hours,
                "devices": adv.get_daily_runtime(hours),
            }
        except Exception as e:
            logger.error(f"Runtime endpoint failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/tips")
    async def heating_tips():
        adv = _resolve_advisor()
        if not adv or not getattr(adv, "enabled", False):
            return {"success": False, "error": "Heating advisor not enabled"}
        try:
            dashboard = adv.get_dashboard()
            return {"success": True, "data": dashboard.get("tips", [])}
        except Exception as e:
            logger.error(f"Tips endpoint failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ═════════ Config ═════════
    @app.get("/api/heating/config")
    async def get_heating_config():
        """Return the full heating config block (with defaults filled in)."""
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            return {
                "success": True,
                "config": {
                    "enabled": bool(heating.get("enabled", False)),
                    "property": _with_defaults(heating.get("property"), _PROPERTY_DEFAULTS),
                    "tariff": _with_defaults(heating.get("tariff"), _TARIFF_DEFAULTS),
                    "boiler": _with_defaults(heating.get("boiler"), _BOILER_DEFAULTS),
                    "comfort": _with_defaults(heating.get("comfort"), _COMFORT_DEFAULTS),
                    "zones": _clean_zones(heating.get("zones") or []),
                },
                "schema": {
                    "property_types": sorted(_PROPERTY_TYPES),
                    "insulation": sorted(_INSULATION),
                    "glazing": sorted(_GLAZING),
                    "boiler_types": sorted(_BOILER_TYPES),
                    "tariff_types": sorted(_TARIFF_TYPES),
                },
            }
        except Exception as e:
            logger.error(f"Failed to read heating config: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/config")
    async def save_heating_config(data: dict):
        """
        Save the heating config. Accepts any subset of:
          enabled, property, tariff, boiler, comfort, zones
        Fields not present are left untouched.
        """
        try:
            cfg = _load_config()
            incoming = data.get("config", data) if isinstance(data, dict) else {}
            heating = cfg.setdefault("heating", {})

            if "enabled" in incoming:
                heating["enabled"] = bool(incoming["enabled"])

            if "property" in incoming:
                heating["property"] = _clean_property(incoming["property"])

            if "tariff" in incoming:
                heating["tariff"] = _clean_tariff(incoming["tariff"])

            if "boiler" in incoming:
                heating["boiler"] = _clean_boiler(incoming["boiler"])

            if "comfort" in incoming:
                heating["comfort"] = _clean_comfort(incoming["comfort"])

            if "zones" in incoming:
                heating["zones"] = _clean_zones(incoming["zones"])

            _save_config(cfg)
            logger.info("Heating config saved via API")
            return {
                "success": True,
                "message": "Heating config saved. Restart the service for changes to take full effect.",
                "restart_required": True,
            }
        except Exception as e:
            logger.error(f"Failed to save heating config: {e}")
            return {"success": False, "error": str(e)}

    # ═════════ Zones (dedicated endpoints) ═════════
    @app.get("/api/heating/zones")
    async def list_zones():
        try:
            cfg = _load_config()
            zones = (cfg.get("heating") or {}).get("zones") or []
            return {"success": True, "zones": _clean_zones(zones)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/zones")
    async def replace_zones(data: dict):
        """Replace the entire zones list."""
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            zones = data.get("zones") if isinstance(data, dict) else None
            if not isinstance(zones, list):
                return {"success": False, "error": "`zones` must be a list"}
            heating["zones"] = _clean_zones(zones)
            _save_config(cfg)
            logger.info(f"Heating zones replaced — {len(heating['zones'])} zone(s)")
            return {"success": True, "zones": heating["zones"]}
        except Exception as e:
            logger.error(f"Failed to replace zones: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/zones/{zone_id}")
    async def upsert_zone(zone_id: str, data: dict):
        """Create or update a single zone."""
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            zones = _clean_zones(heating.get("zones") or [])

            # Build clean candidate (force matching id)
            incoming = dict(data) if isinstance(data, dict) else {}
            incoming["id"] = zone_id
            cleaned = _clean_zone(incoming)
            if not cleaned:
                return {"success": False, "error": "Invalid zone data (missing name?)"}
            cleaned["id"] = zone_id  # preserve path id even after _clean_zone re-slugs

            # Upsert
            replaced = False
            for i, z in enumerate(zones):
                if z["id"] == zone_id:
                    zones[i] = cleaned
                    replaced = True
                    break
            if not replaced:
                zones.append(cleaned)

            heating["zones"] = zones
            _save_config(cfg)
            logger.info(f"Heating zone {'updated' if replaced else 'created'}: {zone_id}")
            return {"success": True, "zone": cleaned, "created": not replaced}
        except Exception as e:
            logger.error(f"Failed to upsert zone {zone_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.delete("/api/heating/zones/{zone_id}")
    async def delete_zone(zone_id: str):
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            zones = heating.get("zones") or []
            before = len(zones)
            zones = [z for z in zones if z.get("id") != zone_id]
            if len(zones) == before:
                return {"success": False, "error": f"Zone '{zone_id}' not found"}
            heating["zones"] = zones
            _save_config(cfg)
            logger.info(f"Heating zone deleted: {zone_id}")
            return {"success": True, "deleted": zone_id, "remaining": len(zones)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═════════ Thermostat discovery (for zone assignment UI) ═════════
    @app.get("/api/heating/thermostats")
    async def list_thermostats():
        """List all HVAC-capable devices with their current zone assignments."""
        try:
            devices = {}
            # Prefer advisor's device getter (what the analysis actually sees)
            adv = _resolve_advisor()
            if adv and hasattr(adv, "_get_devices"):
                try:
                    devices = adv._get_devices() or {}
                except Exception as e:
                    logger.debug(f"Advisor device getter failed: {e}")
            # Fallback to zigbee_service
            if not devices and get_zigbee_service:
                try:
                    zs = get_zigbee_service()
                    if zs and hasattr(zs, "get_all_devices_json"):
                        devices = zs.get_all_devices_json() or {}
                except Exception as e:
                    logger.debug(f"zigbee_service fallback failed: {e}")

            thermostats = _find_thermostats(devices)

            # Annotate with current zone assignment
            cfg = _load_config()
            zones = (cfg.get("heating") or {}).get("zones") or []
            ieee_to_zone = {}
            for z in zones:
                for d in z.get("devices") or []:
                    ieee_to_zone[d] = {"id": z.get("id"), "name": z.get("name")}

            for t in thermostats:
                t["zone"] = ieee_to_zone.get(t["ieee"])

            return {
                "success": True,
                "thermostats": thermostats,
                "count": len(thermostats),
            }
        except Exception as e:
            logger.error(f"Failed to list thermostats: {e}")
            return {"success": False, "error": str(e)}