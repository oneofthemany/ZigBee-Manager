"""
Heating Controller API routes.

Endpoints:
  GET  /api/heating/controller/state              — last-tick decisions snapshot
  POST /api/heating/controller/tick               — force a tick now
  POST /api/heating/controller/dry-run            — toggle dry-run mode at runtime
  GET  /api/heating/controller/config             — full circuits config
  POST /api/heating/controller/config             — replace circuits config
  GET  /api/heating/controller/devices            — receiver + TRV candidates
  GET  /api/heating/controller/sensors            — room-sensor candidates
  POST /api/heating/controller/trv/settings       — write per-TRV config (window_detection, child_lock, valve_detection)
  POST /api/heating/controller/trv/calibrate      — one-shot start motor calibration
  POST /api/heating/controller/trv/apply-config   — re-apply persistent settings to one TRV
"""
import logging
import os
import yaml
from typing import Any, Dict, List, Optional

from fastapi import FastAPI

logger = logging.getLogger("routes.heating_controller")

CONFIG_PATH = "./config/config.yaml"

VALID_EXT_MODES = ("off", "advisory", "push")


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


def _as_bool(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled", "lock"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled", "unlock"):
            return False
    return default


def _slugify(s: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return slug or "item"


def _clean_trvs(room_in: dict) -> List[dict]:
    """
    Normalise a room's TRV list. Accept either:
      trvs: [{ieee, window_detection?, child_lock?, valve_detection?}, ...]
      trv_ieees: ["aa:bb:...", ...]   (legacy)
    Dict form wins on conflict.
    """
    by_ieee: Dict[str, dict] = {}

    legacy = room_in.get("trv_ieees") or []
    if isinstance(legacy, list):
        for ieee in legacy:
            if not ieee:
                continue
            ieee_s = str(ieee).strip()
            if ieee_s:
                by_ieee[ieee_s] = {"ieee": ieee_s}

    new = room_in.get("trvs") or []
    if isinstance(new, list):
        for t in new:
            if isinstance(t, str):
                ieee_s = t.strip()
                if ieee_s:
                    by_ieee.setdefault(ieee_s, {"ieee": ieee_s})
            elif isinstance(t, dict):
                ieee_s = str(t.get("ieee") or "").strip()
                if not ieee_s:
                    continue
                entry: Dict[str, Any] = {"ieee": ieee_s}
                for k in ("window_detection", "child_lock", "valve_detection"):
                    if k in t:
                        b = _as_bool(t.get(k), None)
                        if b is not None:
                            entry[k] = b
                by_ieee[ieee_s] = entry

    return list(by_ieee.values())


def _clean_room(r: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(r, dict) or not r.get("name"):
        return None
    existing_ids = existing_ids or set()
    rid = str(r.get("id") or _slugify(r["name"]))
    base = rid; n = 2
    while rid in existing_ids:
        rid = f"{base}_{n}"; n += 1

    trvs = _clean_trvs(r)

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

    sensor_ieee = r.get("temperature_sensor_ieee")
    sensor_ieee = str(sensor_ieee).strip() if sensor_ieee else ""
    sensor_ieee = sensor_ieee or None

    mode = str(r.get("external_temp_mode", "advisory" if sensor_ieee else "off")).lower()
    if mode not in VALID_EXT_MODES:
        mode = "advisory" if sensor_ieee else "off"
    if not sensor_ieee and mode == "push":
        mode = "off"

    push_interval_raw = _as_float(r.get("external_temp_push_interval_sec"), 300.0)
    push_interval = int(push_interval_raw) if push_interval_raw else 300

    return {
        "id": rid,
        "name": str(r["name"]),
        "target_temp": _as_float(r.get("target_temp"), 21.0),
        "night_setback": _as_float(r.get("night_setback"), 17.0),
        "min_temp": _as_float(r.get("min_temp"), 16.0),
        "temperature_sensor_ieee": sensor_ieee,
        "external_temp_mode": mode,
        "external_temp_push_interval_sec": push_interval,
        "trvs": trvs,
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


def _find_trv_in_config(cfg: Dict[str, Any], ieee: str):
    """Locate a TRV inside the heating.circuits config. Returns (circuit, room, trv_dict) or None."""
    heating = cfg.get("heating") or {}
    for c in heating.get("circuits") or []:
        for r in c.get("rooms") or []:
            # Check new-style list
            for t in (r.get("trvs") or []):
                if isinstance(t, dict) and t.get("ieee") == ieee:
                    return c, r, t
            # Fall back to legacy list — create a dict inline
            legacy = r.get("trv_ieees") or []
            if ieee in legacy:
                # Promote to new-style entry
                trvs_list = r.setdefault("trvs", [])
                # Ensure not double-listed
                for tt in trvs_list:
                    if isinstance(tt, dict) and tt.get("ieee") == ieee:
                        return c, r, tt
                new_entry = {"ieee": ieee}
                trvs_list.append(new_entry)
                return c, r, new_entry
    return None


# ═══════════════════════════════════════════════════════════════════
def register_heating_controller_routes(app: FastAPI, get_controller, get_zigbee_service=None):

    def _resolve():
        c = get_controller()
        if callable(c):
            try:
                c = c()
            except Exception:
                pass
        return c

    def _devices() -> Dict[str, Any]:
        if not get_zigbee_service:
            return {}
        try:
            zs = get_zigbee_service()
            if zs and hasattr(zs, "devices"):
                return zs.devices or {}
        except Exception as e:
            logger.debug(f"zigbee_service access failed: {e}")
        return {}

    @app.get("/api/heating/controller/state")
    async def controller_state():
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        return {"success": True, "state": ctrl.get_state()}

    @app.get("/api/heating/controller/managed")
    async def managed_ieees():
        """
        Return { enabled, ieees[] } — IEEEs currently managed by the heating
        controller (receivers + TRVs). Used by the device modal to disable
        direct heating controls when the controller is running.
        """
        ctrl = _resolve()
        if not ctrl:
            return {"success": True, "enabled": False, "ieees": []}
        managed = set()
        for c in ctrl.circuits:
            rx = c.get("receiver_ieee")
            if rx:
                managed.add(str(rx))
            for r in c.get("rooms", []) or []:
                for t in r.get("trvs", []) or []:
                    if isinstance(t, dict) and t.get("ieee"):
                        managed.add(str(t["ieee"]))
                for legacy in r.get("trv_ieees", []) or []:
                    if legacy:
                        managed.add(str(legacy))
        return {
            "success": True,
            "enabled": bool(getattr(ctrl, "enabled", False)),
            "ieees": sorted(managed),
        }

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

            controller_block = heating.setdefault("controller", {})
            if "enabled" in incoming:
                controller_block["enabled"] = bool(incoming["enabled"])
            if "dry_run" in incoming:
                controller_block["dry_run"] = bool(incoming["dry_run"])

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
            devices = _devices()
            receivers = []
            thermostats = []

            cfg = _load_config()
            assignments_recv: Dict[str, Dict] = {}
            assignments_trv: Dict[str, Dict] = {}
            for c in (cfg.get("heating") or {}).get("circuits") or []:
                if c.get("receiver_ieee"):
                    assignments_recv[c["receiver_ieee"]] = {
                        "circuit_id": c.get("id"), "circuit_name": c.get("name"),
                    }
                for r in c.get("rooms") or []:
                    trv_list = []
                    for t in (r.get("trvs") or []):
                        if isinstance(t, dict) and t.get("ieee"):
                            trv_list.append(t["ieee"])
                    for legacy_ieee in (r.get("trv_ieees") or []):
                        if legacy_ieee and legacy_ieee not in trv_list:
                            trv_list.append(legacy_ieee)
                    for t_ieee in trv_list:
                        assignments_trv[t_ieee] = {
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

                is_mains = False
                if power_source is True or power_source == "Mains":
                    is_mains = True
                elif "SLR" in model_s or "RECEIVER" in model_s:
                    is_mains = True
                elif "SLT" not in model_s and "TRV" not in model_s:
                    if "on_off" in state or state.get("power_source") == "Mains":
                        is_mains = True

                # Heuristic for Aqara TRV recognition so the UI can show its
                # calibration / child-lock / window-detection toggles.
                manuf_s = str(manuf or "").lower()
                is_aqara_trv = (
                                       ("lumi" in manuf_s or "aqara" in manuf_s) and
                                       any(marker in model_s.lower() for marker in ("airrtc", "agl001", "thermostat"))
                               ) or "AGL001" in model_s

                entry = dict(base)
                entry["temperature"] = state.get("local_temperature") or state.get("current_temperature")
                entry["setpoint"] = state.get("occupied_heating_setpoint")
                entry["system_mode"] = state.get("system_mode")
                entry["hvac_action"] = state.get("hvac_action")
                entry["is_aqara_trv"] = is_aqara_trv

                if is_mains:
                    entry["assigned"] = assignments_recv.get(ieee_s)
                    receivers.append(entry)

                entry_trv = dict(base)
                entry_trv["temperature"] = entry["temperature"]
                entry_trv["setpoint"] = entry["setpoint"]
                entry_trv["is_aqara_trv"] = is_aqara_trv
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

    @app.get("/api/heating/controller/sensors")
    async def list_sensor_candidates():
        """
        Return devices that can be used as a room temperature source for a circuit room.
        Includes:
          - bare temperature sensors (cluster 0x0402 / 'temperature' in state)
          - Hive SLTs and similar thermostats (they report accurate room temps)
          - any device exposing local_temperature / current_temperature / temperature
        Deliberately inclusive: the user picks which device actually sits in the room.
        """
        try:
            devices = _devices()
            sensors = []

            for ieee, dev in devices.items():
                if isinstance(dev, dict):
                    state = dev.get("state") or {}
                    name = dev.get("friendly_name") or dev.get("name") or str(ieee)
                    manuf = dev.get("manufacturer")
                    model = dev.get("model")
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

                temp = None
                temp_key = None
                for k in ("local_temperature", "current_temperature", "temperature"):
                    v = state.get(k)
                    try:
                        f = float(v) if v is not None else None
                    except (TypeError, ValueError):
                        f = None
                    if f is not None and f != 0:
                        temp = f
                        temp_key = k
                        break

                if temp is None:
                    continue

                sensors.append({
                    "ieee": str(ieee),
                    "name": name,
                    "manufacturer": manuf,
                    "model": model,
                    "temperature": temp,
                    "source_key": temp_key,
                    "is_thermostat": any(k in state for k in (
                        "system_mode", "occupied_heating_setpoint", "heating_demand"
                    )),
                })

            return {"success": True, "sensors": sensors}
        except Exception as e:
            logger.error(f"Failed to list sensor candidates: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/trv/settings")
    async def update_trv_settings(data: dict):
        """
        Update a TRV's persistent config (window_detection, child_lock, valve_detection)
        both in the running controller and in config.yaml on disk, then apply to the device.

        Body: { "ieee": "...", "window_detection": true, "child_lock": false, "valve_detection": true }
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}

        updates = {}
        for k in ("window_detection", "child_lock", "valve_detection"):
            if k in data:
                b = _as_bool(data.get(k), None)
                if b is not None:
                    updates[k] = b

        if not updates:
            return {"success": False, "error": "no valid settings in request"}

        # 1) Persist to disk
        try:
            cfg = _load_config()
            located = _find_trv_in_config(cfg, ieee)
            if not located:
                return {"success": False, "error": f"TRV {ieee} not found in any room"}
            _, _, trv_dict = located
            trv_dict.update(updates)
            _save_config(cfg)
        except Exception as e:
            logger.error(f"Failed to persist TRV settings: {e}", exc_info=True)
            return {"success": False, "error": f"persist failed: {e}"}

        # 2) Apply in-memory + push to device
        ctrl = _resolve()
        apply_result = None
        if ctrl:
            ctrl.update_trv_settings(ieee, updates)
            try:
                apply_result = await ctrl.apply_trv_config(ieee)
            except Exception as e:
                logger.error(f"apply_trv_config({ieee}) failed: {e}", exc_info=True)
                apply_result = {"success": False, "error": str(e)}

        return {
            "success": True,
            "ieee": ieee,
            "updates": updates,
            "apply_result": apply_result,
        }

    @app.post("/api/heating/controller/trv/calibrate")
    async def trigger_trv_calibration(data: dict):
        """
        One-shot: start motor calibration on an Aqara TRV.
        Body: { "ieee": "..." }
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        return await ctrl.trigger_calibration(ieee)

    @app.post("/api/heating/controller/trv/apply-config")
    async def reapply_trv_config(data: dict):
        """
        Re-send persistent settings (and sensor_type if applicable) to one TRV.
        Useful if a TRV was offline at controller startup.
        Body: { "ieee": "..." }
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        try:
            return await ctrl.apply_trv_config(ieee)
        except Exception as e:
            logger.error(f"apply_trv_config({ieee}) failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}