"""
Heating Controller API routes.

Endpoints:
  GET  /api/heating/controller/state       — last-tick decisions snapshot
  POST /api/heating/controller/tick        — force a tick now
  POST /api/heating/controller/dry-run     — toggle dry-run mode at runtime
  GET  /api/heating/controller/config      — full circuits config
  POST /api/heating/controller/config      — replace circuits config
"""
import logging
import os
import yaml
from typing import Any, Dict, List, Optional

from fastapi import FastAPI

logger = logging.getLogger("routes.heating_controller")

CONFIG_PATH = "./config/config.yaml"


# ─── YAML helpers ──────────────────────────────────────────────────
def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _as_float(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _slugify(s: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return slug or "item"


def _clean_room(r: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(r, dict) or not r.get("name"):
        return None
    existing_ids = existing_ids or set()
    rid = str(r.get("id") or _slugify(r["name"]))
    base = rid; n = 2
    while rid in existing_ids:
        rid = f"{base}_{n}"; n += 1

    trv_ieees = r.get("trv_ieees") or []
    if not isinstance(trv_ieees, list):
        trv_ieees = []

    schedule = r.get("schedule") or []
    if not isinstance(schedule, list):
        schedule = []
    clean_sched = []
    for slot in schedule:
        if not isinstance(slot, dict):
            continue
        days = slot.get("days") or []
        if not isinstance(days, list):
            days = []
        clean_sched.append({
            "days": [d for d in days if d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
            "start": str(slot.get("start", "07:00")),
            "end": str(slot.get("end", "22:00")),
            "temp": _as_float(slot.get("temp"), 20.0),
        })

    return {
        "id": rid,
        "name": str(r["name"]),
        "target_temp": _as_float(r.get("target_temp"), 21.0),
        "night_setback": _as_float(r.get("night_setback"), 17.0),
        "min_temp": _as_float(r.get("min_temp"), 16.0),
        "trv_ieees": [str(t) for t in trv_ieees if t],
        "schedule": clean_sched,
    }


def _clean_circuit(c: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(c, dict) or not c.get("name"):
        return None
    existing_ids = existing_ids or set()
    cid = str(c.get("id") or _slugify(c["name"]))
    base = cid; n = 2
    while cid in existing_ids:
        cid = f"{base}_{n}"; n += 1

    room_ids: set = set()
    rooms = []
    for r in (c.get("rooms") or []):
        cleaned = _clean_room(r, room_ids)
        if cleaned:
            rooms.append(cleaned)
            room_ids.add(cleaned["id"])

    receiver_command = str(c.get("receiver_command", "thermostat")).lower()
    if receiver_command not in ("switch", "thermostat"):
        receiver_command = "thermostat"

    return {
        "id": cid,
        "name": str(c["name"]),
        "receiver_ieee": (str(c.get("receiver_ieee")).strip() if c.get("receiver_ieee") else None) or None,
        "receiver_command": receiver_command,
        "receiver_endpoint": c.get("receiver_endpoint"),
        "rooms": rooms,
    }


def _clean_circuits(circuits: list) -> List[dict]:
    if not isinstance(circuits, list):
        return []
    out = []
    seen = set()
    for c in circuits:
        cleaned = _clean_circuit(c, seen)
        if cleaned:
            out.append(cleaned)
            seen.add(cleaned["id"])
    return out


# ═══════════════════════════════════════════════════════════════════
def register_heating_controller_routes(app: FastAPI, get_controller, get_zigbee_service=None):

    def _resolve():
        c = get_controller()
        if callable(c):
            try: c = c()
            except Exception: pass
        return c

    @app.get("/api/heating/controller/state")
    async def controller_state():
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        return {"success": True, "state": ctrl.get_state()}

    @app.post("/api/heating/controller/tick")
    async def controller_tick():
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        if not ctrl.enabled:
            return {"success": False, "error": "Controller not enabled"}
        try:
            state = await ctrl.force_tick()
            return {"success": True, "state": state}
        except Exception as e:
            logger.error(f"Force tick failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/dry-run")
    async def toggle_dry_run(data: dict):
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        ctrl.dry_run = bool(data.get("dry_run", True))
        logger.warning(f"Heating controller dry_run set to {ctrl.dry_run} via API")
        return {"success": True, "dry_run": ctrl.dry_run}

    @app.get("/api/heating/controller/config")
    async def get_controller_config():
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            controller_block = heating.get("controller") or {}
            return {
                "success": True,
                "config": {
                    "enabled": bool(controller_block.get("enabled", False)),
                    "dry_run": bool(controller_block.get("dry_run", False)),
                    "circuits": _clean_circuits(heating.get("circuits") or []),
                },
            }
        except Exception as e:
            logger.error(f"Failed to read controller config: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/config")
    async def save_controller_config(data: dict):
        """
        Save circuits + controller flags. Accepts:
          { "config": { "enabled": bool, "dry_run": bool, "circuits": [...] } }
        """
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            incoming = data.get("config", data) if isinstance(data, dict) else {}

            # Controller flags
            controller_block = heating.setdefault("controller", {})
            if "enabled" in incoming:
                controller_block["enabled"] = bool(incoming["enabled"])
            if "dry_run" in incoming:
                controller_block["dry_run"] = bool(incoming["dry_run"])

            # Circuits
            if "circuits" in incoming:
                heating["circuits"] = _clean_circuits(incoming["circuits"])

            _save_config(cfg)
            logger.info("Heating controller config saved via API")
            return {
                "success": True,
                "message": "Saved. Restart the service to apply changes to the controller.",
                "restart_required": True,
            }
        except Exception as e:
            logger.error(f"Failed to save controller config: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/controller/devices")
    async def list_controllable_devices():
        """
        Return receiver candidates and TRV candidates.
        Receivers = mains-powered devices with thermostat cluster (system_mode).
        TRVs = any device with thermostat attributes (temperature, setpoint).
        """
        try:
            devices = {}
            if get_zigbee_service:
                try:
                    zs = get_zigbee_service()
                    if zs and hasattr(zs, "devices"):
                        devices = zs.devices or {}
                except Exception as e:
                    logger.debug(f"zigbee_service access failed: {e}")

            receivers = []
            thermostats = []

            # Build current circuit/room assignments for annotation
            cfg = _load_config()
            assignments_recv: Dict[str, Dict] = {}
            assignments_trv: Dict[str, Dict] = {}
            for c in (cfg.get("heating") or {}).get("circuits") or []:
                if c.get("receiver_ieee"):
                    assignments_recv[c["receiver_ieee"]] = {
                        "circuit_id": c.get("id"), "circuit_name": c.get("name"),
                    }
                for r in c.get("rooms") or []:
                    for t in r.get("trv_ieees") or []:
                        assignments_trv[t] = {
                            "circuit_id": c.get("id"), "circuit_name": c.get("name"),
                            "room_id": r.get("id"), "room_name": r.get("name"),
                        }

            for ieee, dev in devices.items():
                if isinstance(dev, dict):
                    state = dev.get("state") or {}
                    name = dev.get("friendly_name") or dev.get("name") or str(ieee)
                    manuf = dev.get("manufacturer"); model = dev.get("model")
                    power_source = dev.get("power_source")
                else:
                    state = getattr(dev, "state", None) or {}
                    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
                    if not name:
                        service = getattr(dev, "service", None)
                        if service:
                            name = (getattr(service, "friendly_names", None) or {}).get(str(ieee))
                    name = name or str(ieee)
                    manuf = getattr(dev, "manufacturer", None)
                    model = getattr(dev, "model", None)
                    power_source = getattr(dev, "power_source", None)
                    # Try zigpy device for power source
                    if power_source is None:
                        zigpy_dev = getattr(dev, "zigpy_dev", None)
                        if zigpy_dev:
                            try:
                                power_source = zigpy_dev.node_desc.mac_capability_flags.mains_powered
                            except Exception:
                                pass

                ieee_s = str(ieee)
                model_s = str(model or "").upper()
                base = {"ieee": ieee_s, "name": name, "manufacturer": manuf, "model": model}

                has_thermostat = any(k in state for k in (
                    "system_mode", "occupied_heating_setpoint",
                    "local_temperature", "current_temperature",
                    "heating_demand", "hvac_action"
                ))

                if not has_thermostat:
                    continue

                # Determine if this is a receiver (mains-powered) or a TRV (battery)
                is_mains = False
                if power_source is True or power_source == "Mains":
                    is_mains = True
                elif "SLR" in model_s or "RECEIVER" in model_s:
                    is_mains = True
                elif "SLT" not in model_s and "TRV" not in model_s:
                    # Heuristic: if has system_mode and NOT a known battery model
                    # check if it has on_off or other mains indicators
                    if "on_off" in state or state.get("power_source") == "Mains":
                        is_mains = True

                entry = dict(base)
                entry["temperature"] = state.get("local_temperature") or state.get("current_temperature")
                entry["setpoint"] = state.get("occupied_heating_setpoint")
                entry["system_mode"] = state.get("system_mode")
                entry["hvac_action"] = state.get("hvac_action")

                if is_mains:
                    entry["assigned"] = assignments_recv.get(ieee_s)
                    receivers.append(entry)

                # All thermostat devices are also TRV candidates
                entry_trv = dict(base)
                entry_trv["temperature"] = entry["temperature"]
                entry_trv["setpoint"] = entry["setpoint"]
                entry_trv["assigned"] = assignments_trv.get(ieee_s)
                thermostats.append(entry_trv)

            return {
                "success": True,
                "receivers": receivers,
                "thermostats": thermostats,
            }
        except Exception as e:
            logger.error(f"Failed to list controllable devices: {e}", exc_info=True)
            return {"success": False, "error": str(e)}