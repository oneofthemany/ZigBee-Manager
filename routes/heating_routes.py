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
    "flow_temperature_c": 70,     # Design flow temp; condensing boilers often 55–65
    "design_outdoor_c": -3.0,     # UK MCS standard for sizing calculations
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


# ─── Diagnostics helpers ───────────────────────────────────────────
def _safe_outdoor(advisor) -> Optional[float]:
    if advisor and getattr(advisor, "weather", None):
        try:
            return advisor.weather.get_outdoor_temperature()
        except Exception:
            return None
    return None


def _summarise_telemetry(temp_series: List[Dict[str, Any]],
                         attr_used: Optional[str]) -> Dict[str, Any]:
    import datetime as _dt
    if not temp_series:
        return {
            "sample_count": 0,
            "attribute": attr_used,
            "first_ts": None, "last_ts": None,
            "span_hours": 0,
            "min_value": None, "max_value": None,
        }
    vals, tss = [], []
    for p in temp_series:
        v = p.get("numeric_val")
        if v is None:
            try: v = float(p.get("value"))
            except (TypeError, ValueError): continue
        t = p.get("ts")
        if isinstance(t, _dt.datetime):
            tss.append(t.timestamp())
            vals.append(float(v))
    if not vals:
        return {"sample_count": 0, "attribute": attr_used,
                "first_ts": None, "last_ts": None, "span_hours": 0,
                "min_value": None, "max_value": None}
    first_ts, last_ts = min(tss), max(tss)
    return {
        "sample_count": len(vals),
        "attribute": attr_used,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "span_hours": round((last_ts - first_ts) / 3600.0, 1),
        "min_value": round(min(vals), 2),
        "max_value": round(max(vals), 2),
        "range_c": round(max(vals) - min(vals), 2),
    }


def _summarise_ticks(tick_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    import datetime as _dt
    if not tick_rows:
        return {
            "tick_count": 0,
            "active_count": 0,
            "active_fraction": None,
            "first_ts": None, "last_ts": None,
            "gap_count_over_3min": 0,
            "longest_gap_sec": 0,
            "status": "no_data",
        }
    tss: List[float] = []
    active = 0
    for r in tick_rows:
        t = r.get("ts")
        if isinstance(t, _dt.datetime):
            tss.append(t.timestamp())
        if r.get("heating_active"):
            active += 1
    tss.sort()
    gaps = [tss[i] - tss[i-1] for i in range(1, len(tss))]
    big_gaps = sum(1 for g in gaps if g > 180)  # > 3× expected cadence
    longest = max(gaps) if gaps else 0
    total = len(tick_rows)
    frac = active / total if total else None
    # Status classification
    if total < 60:
        status = "sparse"       # < ~1h of data at 1/min
    elif frac is not None and frac > 0.9:
        status = "heating_dominant"
    elif frac is not None and frac < 0.05:
        status = "heating_idle"
    else:
        status = "healthy"
    return {
        "tick_count": total,
        "active_count": active,
        "active_fraction": round(frac, 3) if frac is not None else None,
        "first_ts": tss[0] if tss else None,
        "last_ts": tss[-1] if tss else None,
        "gap_count_over_3min": big_gaps,
        "longest_gap_sec": round(longest),
        "status": status,
    }


def _summarise_windows(
        temp_series: List[Dict[str, Any]],
        tick_rows: List[Dict[str, Any]],
        outdoor_temp_c: Optional[float],
        floor_area_m2: float,
        ceiling_height_m: float,
) -> Dict[str, Any]:
    """
    Run _find_cooldown_windows twice (no gate, gate) and score each window
    with _fit_newton_cooling so we can report the full funnel:
      raw candidates → survived heating gate → survived R² → counted in τ.
    """
    from modules.thermal_profile import (
        _find_cooldown_windows, _fit_newton_cooling,
        LEARN_MIN_DURATION_SEC, LEARN_MIN_DROP_C, LEARN_MAX_DURATION_SEC,
    )

    summary = {
        "raw_candidates": 0,
        "after_heating_gate": 0,
        "after_r2_filter": 0,
        "r2_threshold": 0.5,
        "thresholds": {
            "min_duration_sec": LEARN_MIN_DURATION_SEC,
            "min_drop_c": LEARN_MIN_DROP_C,
            "max_duration_sec": LEARN_MAX_DURATION_SEC,
        },
        "rejection_reasons": {
            "heating_active": 0,
            "r2_below_threshold": 0,
            "fit_failed": 0,
        },
        "samples": [],  # Up to last 5 passing windows, compact
    }

    if not temp_series:
        return summary

    # Raw: no gate
    raw = _find_cooldown_windows(
        temp_series, None,
        min_duration_sec=LEARN_MIN_DURATION_SEC,
        min_drop_c=LEARN_MIN_DROP_C,
        max_duration_sec=LEARN_MAX_DURATION_SEC,
    )
    summary["raw_candidates"] = len(raw)

    # With gate (if we have tick rows)
    if tick_rows:
        try:
            from modules.heating_anomaly_watcher import _build_heating_state_getter
            getter = _build_heating_state_getter(tick_rows)
        except Exception:
            getter = None
    else:
        getter = None

    if getter is not None:
        gated = _find_cooldown_windows(
            temp_series, None,
            min_duration_sec=LEARN_MIN_DURATION_SEC,
            min_drop_c=LEARN_MIN_DROP_C,
            max_duration_sec=LEARN_MAX_DURATION_SEC,
            heating_state_getter=getter,
        )
        summary["after_heating_gate"] = len(gated)
        summary["rejection_reasons"]["heating_active"] = max(0, len(raw) - len(gated))
    else:
        gated = raw
        summary["after_heating_gate"] = len(gated)  # no gate applied

    # Score each gated window with Newton fit
    outdoor = outdoor_temp_c if outdoor_temp_c is not None else 10.0
    passed = 0
    recent_samples = []
    for (t0, t1, samples) in gated:
        tau, r2 = _fit_newton_cooling(samples, outdoor)
        if tau is None:
            summary["rejection_reasons"]["fit_failed"] += 1
            continue
        if r2 is None or r2 < 0.5:
            summary["rejection_reasons"]["r2_below_threshold"] += 1
            continue
        passed += 1
        recent_samples.append({
            "start_ts": t0, "end_ts": t1,
            "duration_min": round((t1 - t0) / 60),
            "temp_drop_c": round(samples[0][1] - samples[-1][1], 2),
            "tau_hours": round(tau / 3600.0, 2),
            "r2": round(r2, 3),
        })
    summary["after_r2_filter"] = passed
    # Most recent 5 passing windows, newest first
    recent_samples.sort(key=lambda s: s["end_ts"], reverse=True)
    summary["samples"] = recent_samples[:5]
    return summary


def _derive_verdict(
        dimensions: Optional[Dict[str, Any]],
        telemetry: Dict[str, Any],
        ticks: Dict[str, Any],
        windows: Dict[str, Any],
        profile,
) -> Dict[str, str]:
    """Plain-English summary of why a room is or isn't learning."""
    if not dimensions or not dimensions.get("floor_area_m2"):
        return {"code": "dimensions_missing",
                "message": "Room has no floor area configured — static profile "
                           "cannot be computed and measured τ has no thermal "
                           "mass to reference. Add dimensions in heating config."}

    if telemetry["sample_count"] < 10:
        return {"code": "no_temperature_data",
                "message": "Fewer than 10 temperature samples in the window. "
                           "Check the room's temperature sensor is reporting."}

    if telemetry.get("range_c", 0) < 0.5:
        return {"code": "telemetry_flat",
                "message": f"Temperature only varied by "
                           f"{telemetry.get('range_c', 0)}°C over "
                           f"{telemetry['span_hours']}h. Sensor may be stuck "
                           f"or quantising too coarsely."}

    if ticks["status"] == "no_data":
        return {"code": "no_tick_data_yet",
                "message": "No heating controller ticks recorded for this room "
                           "yet. The heating-off gate is not active — baseline "
                           "may be noisy. Give it an hour of controller runtime."}

    if ticks["status"] == "heating_dominant":
        return {"code": "heating_always_on",
                "message": f"Heating was active in "
                           f"{int(ticks['active_fraction']*100)}% of ticks. "
                           f"Natural cool-down windows are rare — baseline "
                           f"learning needs at least some heating-off periods "
                           f"(e.g. overnight setback, away mode, or mild days)."}

    if windows["raw_candidates"] == 0:
        return {"code": "no_candidate_windows",
                "message": "No monotonically-falling temperature runs met the "
                           "minimum duration and drop thresholds. Temperature "
                           "may be too noisy or oscillating tightly around setpoint."}

    if windows["after_heating_gate"] == 0:
        return {"code": "all_windows_during_heating",
                "message": "All cool-down candidates overlapped active heating. "
                           "Wait for a natural off period, or loosen heating "
                           "schedules to create learning opportunities."}

    if windows["after_r2_filter"] == 0:
        return {"code": "all_windows_failed_r2",
                "message": "Cool-down windows exist but Newton's-law fits are "
                           "poor (R² < 0.5). Usually means sensor noise or "
                           "residual radiator warmth extending into the window."}

    if profile.measured_w_per_k is None:
        return {"code": "measured_not_applied",
                "message": "Windows passed fitting but no measured W/K was "
                           "computed — check dimensions and fit outputs."}

    if profile.measured_confidence < 0.3:
        return {"code": "low_confidence",
                "message": f"Measured τ exists "
                           f"({windows['after_r2_filter']} windows) but "
                           f"confidence is {profile.measured_confidence:.2f}. "
                           f"Blended profile still favours static. Needs ~10 "
                           f"good windows for full confidence."}

    return {"code": "ok",
            "message": f"Learning healthily: {windows['after_r2_filter']} "
                       f"windows, confidence {profile.measured_confidence:.2f}."}

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
    # Flow temp & design outdoor — used for radiator sizing (Phase 4)
    flow_c = _coerce_float(b.get("flow_temperature_c"), out["flow_temperature_c"])
    out["flow_temperature_c"] = max(30.0, min(90.0, flow_c))
    design_out = _coerce_float(b.get("design_outdoor_c"), out["design_outdoor_c"])
    out["design_outdoor_c"] = max(-20.0, min(10.0, design_out))
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


def _generate_room_tips(room: dict, insulation: str) -> List[dict]:
    """
    Rule-based tips. Each tip is {id, severity, title, detail, action}.
    severity: info | warning
    """
    tips = []
    dim = room.get("dimensions") or {}
    rad = room.get("radiator") or {}
    windows = dim.get("windows") or []
    doors = dim.get("doors") or []

    # --- Radiator placement ---
    placement = (rad.get("placement") or "").lower()
    if placement == "under_window":
        tips.append({
            "id": "radiator_under_window",
            "severity": "warning",
            "title": "Radiator placed under a window",
            "detail": (
                "Rising warm air mixes with cold air falling off the window, "
                "boosting perceived draught and losing ~10% efficiency. If relocating "
                "isn't possible, fit a radiator shelf on top to deflect air into the "
                "room, and add thermal lining to the curtains (never let curtains "
                "drape over the radiator)."
            ),
            "action": "Consider a radiator shelf + thermally-lined curtains",
        })

    rad_wall = (rad.get("wall") or "").lower()
    walls = dim.get("walls") or {}
    rad_wall_type = (walls.get(rad_wall) or {}).get("type") if rad_wall else None
    # Treat these as "radiator is on an external wall":
    #   - selected wall is typed external
    #   - placement is 'external_wall'
    #   - placement is 'under_window' (windows are always on external walls)
    rad_is_on_external_wall = (
            rad_wall_type == "external"
            or placement == "external_wall"
            or placement == "under_window"
    )

    if rad.get("reflective_panel") is False:
        if rad_is_on_external_wall:
            tips.append({
                "id": "no_reflective_panel_external",
                "severity": "info",
                "title": "No reflective panel behind radiator",
                "detail": (
                    "A reflective panel (foil or purpose-made board) behind a "
                    "radiator on an external wall returns 3–8% more heat into "
                    "the room by cutting conductive loss through the cold wall "
                    "behind it. ~£10 per panel."
                ),
                "action": "Fit a reflective panel behind this radiator",
            })
        else:
            tips.append({
                "id": "no_reflective_panel_internal",
                "severity": "info",
                "title": "Consider a reflective panel",
                "detail": (
                    "Even on an internal wall, a reflective panel redirects "
                    "heat back into the room instead of warming the wall "
                    "fabric first. The gain is smaller than on external walls "
                    "(~1–3%) but it improves setpoint responsiveness, which "
                    "matters for TRV-controlled rooms."
                ),
                "action": "Fit a reflective panel behind this radiator",
            })
    elif rad.get("reflective_panel") is None:
        if rad_is_on_external_wall:
            tips.append({
                "id": "check_reflective_panel_external",
                "severity": "info",
                "title": "Reflective panel status unknown",
                "detail": "Radiator is on an external wall. Check whether a reflective panel is fitted — if not, it's worth adding.",
                "action": "Update the room config with reflective_panel true/false",
            })
        else:
            tips.append({
                "id": "check_reflective_panel_internal",
                "severity": "info",
                "title": "Reflective panel status unknown",
                "detail": "Mark whether a reflective panel is fitted behind this radiator. Useful on any wall, most impactful on external walls.",
                "action": "Update the room config with reflective_panel true/false",
            })

    # Radiator type (single vs double)
    rtype = (rad.get("type") or "").lower()
    if rtype == "single_panel":
        tips.append({
            "id": "single_panel_radiator",
            "severity": "info",
            "title": "Single-panel radiator",
            "detail": (
                "Single panels deliver roughly half the output of a same-sized "
                "double-panel + double-convector type. If this room struggles "
                "to reach target, upgrading the panel type is often cheaper "
                "than replacing to a larger footprint."
            ),
            "action": "Consider upgrading to a K2 / P+ type in the same footprint",
        })

    # --- Insulation ---
    if insulation == "none":
        tips.append({
            "id": "no_insulation",
            "severity": "warning",
            "title": "No insulation recorded",
            "detail": (
                "The whole-dwelling insulation level is set to 'none'. Wall, "
                "loft and floor insulation have the biggest single impact on "
                "running cost. A UK ECO-funded assessment may cover 100% of "
                "cavity-wall and loft works if eligible."
            ),
            "action": "Check eligibility for ECO4 grants; update property config after any works",
        })

    # --- Glazing ---
    single_glazed_count = sum(
        1 for w in windows if str(w.get("glazing", "")).lower() == "single"
    )
    if single_glazed_count:
        tips.append({
            "id": "single_glazing",
            "severity": "warning",
            "title": f"{single_glazed_count} single-glazed window{'s' if single_glazed_count > 1 else ''}",
            "detail": (
                "Single glazing loses ~4.8 W/m²/K — roughly 3× a modern double-glazed "
                "unit. Secondary glazing is a non-invasive alternative if replacement "
                "is not possible (listed buildings, rental)."
            ),
            "action": "Upgrade to double/triple glazing, or fit secondary glazing",
        })

    # --- External doors ---
    ext_door_area = sum(
        float(d.get("area_m2") or 0) for d in doors if d.get("type") == "external"
    )
    if ext_door_area > 0:
        tips.append({
            "id": "external_door",
            "severity": "info",
            "title": "External door in this room",
            "detail": (
                "External doors (especially older wooden ones) leak heat through "
                "their frame seals. Check compression on the weather seals, "
                "consider a draught excluder at the base, and a heavy curtain on "
                "the inside if the frame is particularly poor."
            ),
            "action": "Inspect seals, add a door curtain if needed",
        })

    # --- Floor type ---
    floor_type = (dim.get("floor_type") or "").lower()
    if floor_type in ("suspended", "wooden"):
        tips.append({
            "id": "suspended_floor",
            "severity": "info",
            "title": "Suspended / wooden floor",
            "detail": (
                "Suspended floors lose ~0.6 W/m²/K — about 40% more than a carpeted "
                "floor. Under-floor insulation (rockwool + mesh, or spray foam) is "
                "the fastest payback you can retrofit without lifting the floor."
            ),
            "action": "Consider under-floor insulation from below",
        })

    return tips

# ═══════════════════════════════════════════════════════════════════
def register_heating_routes(app: FastAPI, get_heating_advisor, get_zigbee_service,get_anomaly_watcher=None):
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


    @app.get("/api/heating/circuits/{circuit_id}/rooms/{room_id}/tips")
    async def circuit_room_tips(circuit_id: str, room_id: str):
        """
        Return actionable efficiency tips for a room based on its config.
        Generated from the room's dimensions / radiator / insulation data.
        """
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}

            found_room = None
            found_circuit = None
            for c in (heating.get("circuits") or []):
                if str(c.get("id")) != str(circuit_id):
                    continue
                found_circuit = c
                for r in (c.get("rooms") or []):
                    if str(r.get("id")) == room_id:
                        found_room = r
                        break
                break
            if not found_room:
                return {"success": False, "error": "Room not found"}

            insulation = (heating.get("property") or {}).get("insulation", "partial")
            tips = _generate_room_tips(found_room, insulation)
            return {"success": True, "tips": tips,
                    "meta": {"room_name": found_room.get("name"),
                             "circuit_name": found_circuit.get("name")}}
        except Exception as e:
            logger.error(f"Tips failed for {room_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/anomalies")
    async def heating_anomalies():
        watcher = get_anomaly_watcher() if get_anomaly_watcher else None
        if not watcher:
            return {"success": True, "data": {
                "last_scan_ts": None, "last_scan_age_seconds": None,
                "active": [], "recently_resolved": [],
            }}
        return {"success": True, "data": watcher.get_snapshot()}

    @app.post("/api/heating/anomalies/scan")
    async def scan_anomalies_now():
        watcher = get_anomaly_watcher() if get_anomaly_watcher else None
        if not watcher:
            return {"success": False, "error": "Anomaly watcher not initialised"}
        try:
            new = await watcher.scan_once()
            return {"success": True, "new_anomalies": new, "data": watcher.get_snapshot()}
        except Exception as e:
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


    @app.get("/api/heating/circuits/{circuit_id}/rooms/{room_id}/thermal")
    async def circuit_room_thermal(circuit_id: str, room_id: str, days: int = 14):
        """Thermal profile for a specific room in a specific circuit."""
        return await _thermal_for_room(circuit_id, room_id, days)


    # ── Diagnostics: why isn't this room learning its τ? ──────────────
    @app.get("/api/heating/diagnostics/{circuit_id}/rooms/{room_id}")
    async def circuit_room_diagnostics(circuit_id: str, room_id: str,
                                       days: int = 14):
        return await _diagnostics_for_room(circuit_id, room_id, days)

    @app.get("/api/heating/diagnostics/rooms/{room_id}")
    async def room_diagnostics_legacy(room_id: str, days: int = 14):
        """Back-compat when circuit_id isn't known (e.g. ambiguous 'room_1')."""
        return await _diagnostics_for_room(None, room_id, days)

    async def _diagnostics_for_room(circuit_id, room_id: str, days: int):
        """
        Walk the thermal-profile learning pipeline and count what survives
        at each stage. This is the endpoint the UI uses to explain *why* a
        room is or isn't producing a measured τ, without anyone needing to
        run SQL against the telemetry DB.

        Cheap-ish — runs compute_measured once with instrumentation — but
        not something to call every second. Meant for an on-demand "why?"
        button on the thermal panel.
        """
        adv = _resolve_advisor()
        if not adv:
            return {"success": False, "error": "Heating advisor not available"}

        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            insulation = (heating.get("property") or {}).get("insulation", "partial")

            # ── Resolve the room (same logic as /thermal) ─────────────
            circuits = heating.get("circuits") or []
            found_room = None
            found_circuit = None
            matches = 0
            for c in circuits:
                if circuit_id is not None and str(c.get("id")) != str(circuit_id):
                    continue
                for r in (c.get("rooms") or []):
                    if str(r.get("id")) == room_id:
                        matches += 1
                        if not found_room:
                            found_room = r
                            found_circuit = c
                        if circuit_id is not None:
                            break
                if found_room and circuit_id is not None:
                    break
            if not found_room:
                return {"success": False,
                        "error": f"Room '{room_id}' not found"
                                 + (f" in circuit '{circuit_id}'" if circuit_id else "")}

            cid = str(found_circuit.get("id")) if found_circuit else None
            dimensions = found_room.get("dimensions")
            sensor_ieee = found_room.get("temperature_sensor_ieee")
            if not sensor_ieee:
                trvs = found_room.get("trvs") or []
                if trvs and isinstance(trvs[0], dict):
                    sensor_ieee = trvs[0].get("ieee")

            # ── Stage 1: Telemetry ────────────────────────────────────
            temp_series = []
            telemetry_attr_used = None
            if sensor_ieee:
                try:
                    from modules.telemetry_db import query_device_state_history
                    hours = max(24, int(days) * 24)
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = query_device_state_history(sensor_ieee, attr, hours) or []
                        if rows:
                            temp_series = rows
                            telemetry_attr_used = attr
                            break
                except Exception as e:
                    logger.warning(f"diagnostics: telemetry fetch failed: {e}")

            telemetry_section = _summarise_telemetry(temp_series, telemetry_attr_used)

            # ── Stage 2: Ticks (heating-state gate data) ──────────────
            tick_rows = []
            if cid:
                try:
                    from modules.telemetry_db import query_room_heating_state
                    tick_rows = query_room_heating_state(
                        circuit_id=cid, room_id=room_id, hours=int(days) * 24,
                    )
                except Exception as e:
                    logger.debug(f"diagnostics: tick fetch failed: {e}")

            ticks_section = _summarise_ticks(tick_rows)

            # ── Stage 3: Cool-down window funnel ──────────────────────
            # Recompute the funnel with instrumentation. We call
            # _find_cooldown_windows twice: once WITHOUT the gate, once WITH,
            # so we can report how many candidates the gate rejected.
            windows_section = _summarise_windows(
                temp_series=temp_series,
                tick_rows=tick_rows,
                outdoor_temp_c=_safe_outdoor(adv),
                floor_area_m2=float((dimensions or {}).get("floor_area_m2") or 0.0),
                ceiling_height_m=float((dimensions or {}).get("ceiling_height_m") or 2.4),
            )

            # ── Stage 4: Final profile (same call as /thermal would make) ──
            from modules.thermal_profile import compute_profile
            outdoor_getter = None
            current_out = _safe_outdoor(adv)
            if current_out is not None:
                outdoor_getter = lambda _ts, _v=current_out: _v

            heating_state_getter = None
            try:
                from modules.heating_anomaly_watcher import _build_heating_state_getter
                if tick_rows:
                    heating_state_getter = _build_heating_state_getter(tick_rows)
            except Exception:
                pass

            profile = compute_profile(
                room_id=room_id,
                dimensions=dimensions,
                insulation=insulation,
                temperature_series=temp_series,
                outdoor_temp_getter=outdoor_getter,
                heating_state_getter=heating_state_getter,
            )

            # ── Verdict ───────────────────────────────────────────────
            verdict = _derive_verdict(
                dimensions=dimensions,
                telemetry=telemetry_section,
                ticks=ticks_section,
                windows=windows_section,
                profile=profile,
            )

            return {
                "success": True,
                "verdict": verdict["code"],
                "verdict_message": verdict["message"],
                "meta": {
                    "circuit_id": cid,
                    "circuit_name": found_circuit.get("name") if found_circuit else None,
                    "room_name": found_room.get("name"),
                    "sensor_ieee": sensor_ieee,
                    "insulation": insulation,
                    "days": int(days),
                    "ambiguous_id": (circuit_id is None and matches > 1),
                    "match_count": matches,
                },
                "telemetry": telemetry_section,
                "ticks": ticks_section,
                "cooldown_windows": windows_section,
                "profile": profile.to_dict(),
            }

        except Exception as e:
            logger.error(f"Diagnostics failed for {room_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/rooms/{room_id}/thermal")
    async def room_thermal_legacy(room_id: str, days: int = 14):
        """
        Back-compat: if multiple circuits share the room_id (common when users
        accept the default 'room_1' everywhere), fall back to the first match
        and warn.
        """
        return await _thermal_for_room(None, room_id, days)

    async def _thermal_for_room(circuit_id, room_id: str, days: int):
        adv = _resolve_advisor()
        if not adv:
            return {"success": False, "error": "Heating advisor not available"}
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            insulation = (heating.get("property") or {}).get("insulation", "partial")

            circuits = heating.get("circuits") or []
            found_room = None
            found_circuit = None
            matches = 0
            for c in circuits:
                if circuit_id is not None and str(c.get("id")) != str(circuit_id):
                    continue
                for r in (c.get("rooms") or []):
                    if str(r.get("id")) == room_id:
                        matches += 1
                        if not found_room:
                            found_room = r
                            found_circuit = c
                        if circuit_id is not None:
                            break  # exact match, stop searching
                if found_room and circuit_id is not None:
                    break

            if not found_room:
                return {"success": False,
                        "error": f"Room '{room_id}' not found"
                                 + (f" in circuit '{circuit_id}'" if circuit_id else "")}

            ambiguous = (circuit_id is None and matches > 1)

            dimensions = found_room.get("dimensions")

            sensor_ieee = found_room.get("temperature_sensor_ieee")
            if not sensor_ieee:
                trvs = found_room.get("trvs") or []
                if trvs and isinstance(trvs[0], dict):
                    sensor_ieee = trvs[0].get("ieee")

            temp_series = []
            if sensor_ieee:
                try:
                    from modules.telemetry_db import query_device_state_history
                    hours = max(24, int(days) * 24)
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = query_device_state_history(sensor_ieee, attr, hours) or []
                        if rows:
                            temp_series = rows
                            break
                except Exception as e:
                    logger.warning(f"history fetch failed: {e}")

            outdoor_temp_getter = None
            if adv and getattr(adv, "weather", None):
                try:
                    current_out = adv.weather.get_outdoor_temperature()
                    outdoor_temp_getter = lambda _ts, _v=current_out: _v
                except Exception:
                    pass

            from modules.thermal_profile import compute_profile
            profile = compute_profile(
                room_id=room_id,
                dimensions=dimensions,
                insulation=insulation,
                temperature_series=temp_series,
                outdoor_temp_getter=outdoor_temp_getter,
            )

            return {
                "success": True,
                "thermal": profile.to_dict(),
                "meta": {
                    "circuit_id": str(found_circuit.get("id")) if found_circuit else None,
                    "circuit_name": found_circuit.get("name") if found_circuit else None,
                    "room_name": found_room.get("name"),
                    "sensor_ieee": sensor_ieee,
                    "insulation": insulation,
                    "temperature_samples": len(temp_series),
                    "days": int(days),
                    "ambiguous_id": ambiguous,
                    "match_count": matches,
                },
            }
        except Exception as e:
            logger.error(f"Thermal profile failed for {room_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


    @app.get("/api/heating/circuits/{circuit_id}/rooms/{room_id}/sizing")
    async def circuit_room_sizing(circuit_id: str, room_id: str):
        """
        Radiator / BTU sizing for one room.
        Uses Phase 3 thermal profile + design outdoor temp + flow temp
        from the boiler config. If the user has entered installed radiator
        capacity, also returns adequate/under/oversized status.
        """
        adv = _resolve_advisor()
        if not adv:
            return {"success": False, "error": "Heating advisor not available"}
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            boiler = _with_defaults(heating.get("boiler"), _BOILER_DEFAULTS)
            insulation = (heating.get("property") or {}).get("insulation", "partial")

            # Locate room
            circuits = heating.get("circuits") or []
            found_room = None
            found_circuit = None
            for c in circuits:
                if str(c.get("id")) != str(circuit_id):
                    continue
                found_circuit = c
                for r in (c.get("rooms") or []):
                    if str(r.get("id")) == room_id:
                        found_room = r
                        break
                break
            if not found_room:
                return {"success": False,
                        "error": f"Room '{room_id}' not found in circuit '{circuit_id}'"}

            # Build the thermal profile first (same as Phase 3 endpoint)
            dimensions = found_room.get("dimensions")
            sensor_ieee = found_room.get("temperature_sensor_ieee")
            if not sensor_ieee:
                trvs = found_room.get("trvs") or []
                if trvs and isinstance(trvs[0], dict):
                    sensor_ieee = trvs[0].get("ieee")

            temp_series = []
            if sensor_ieee:
                try:
                    from modules.telemetry_db import query_device_state_history
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = query_device_state_history(sensor_ieee, attr, 14 * 24) or []
                        if rows:
                            temp_series = rows
                            break
                except Exception as e:
                    logger.debug(f"history fetch failed: {e}")

            outdoor_getter = None
            if getattr(adv, "weather", None):
                try:
                    current_out = adv.weather.get_outdoor_temperature()
                    outdoor_getter = lambda _ts, _v=current_out: _v
                except Exception:
                    pass

            from modules.thermal_profile import compute_profile
            from modules.radiator_sizing import compute_sizing

            profile = compute_profile(
                room_id=room_id,
                dimensions=dimensions,
                insulation=insulation,
                temperature_series=temp_series,
                outdoor_temp_getter=outdoor_getter,
            )

            # Now the sizing calc
            target = float(found_room.get("target_temp") or 21.0)
            design_out = float(boiler.get("design_outdoor_c", -3.0))
            flow_c = float(boiler.get("flow_temperature_c", 70.0))

            rad_cfg = found_room.get("radiator") or {}
            installed_w = rad_cfg.get("watts_at_dt50")
            room_flow = rad_cfg.get("flow_temperature_c", flow_c)

            sizing = compute_sizing(
                room_id=room_id,
                w_per_k=profile.blended_w_per_k,
                target_temp_c=target,
                design_outdoor_c=design_out,
                installed_watts_at_dt50=installed_w,
                flow_temperature_c=room_flow,
            )

            return {
                "success": True,
                "sizing": sizing.to_dict(),
                "thermal": profile.to_dict(),
                "meta": {
                    "circuit_id": str(found_circuit.get("id")),
                    "circuit_name": found_circuit.get("name"),
                    "room_name": found_room.get("name"),
                    "insulation": insulation,
                    "flow_temperature_c": flow_c,
                    "design_outdoor_c": design_out,
                    "radiator_description": rad_cfg.get("description"),
                },
            }

        except Exception as e:
            logger.error(f"Sizing failed for {room_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


    @app.get("/api/heating/circuits/{circuit_id}/rooms/{room_id}/preheat")
    async def circuit_room_preheat(
            circuit_id: str,
            room_id: str,
            target_temp: float = None,
    ):
        """
        Per-room pre-heat recommendation.

        If target_temp is not supplied, uses the room's configured target.
        Uses the live room temperature (external sensor preferred, first TRV
        as fallback) and current outdoor temperature.
        """
        adv = _resolve_advisor()
        if not adv:
            return {"success": False, "error": "Heating advisor not available"}
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            boiler = _with_defaults(heating.get("boiler"), _BOILER_DEFAULTS)
            insulation = (heating.get("property") or {}).get("insulation", "partial")

            # Locate room
            circuits = heating.get("circuits") or []
            found_room = None
            found_circuit = None
            for c in circuits:
                if str(c.get("id")) != str(circuit_id):
                    continue
                found_circuit = c
                for r in (c.get("rooms") or []):
                    if str(r.get("id")) == room_id:
                        found_room = r
                        break
                break
            if not found_room:
                return {"success": False,
                        "error": f"Room '{room_id}' not found in circuit '{circuit_id}'"}

            # Build thermal profile (re-uses Phase 3)
            dimensions = found_room.get("dimensions")
            sensor_ieee = found_room.get("temperature_sensor_ieee")
            if not sensor_ieee:
                trvs = found_room.get("trvs") or []
                if trvs and isinstance(trvs[0], dict):
                    sensor_ieee = trvs[0].get("ieee")

            temp_series = []
            if sensor_ieee:
                try:
                    from modules.telemetry_db import query_device_state_history
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = query_device_state_history(sensor_ieee, attr, 14 * 24) or []
                        if rows:
                            temp_series = rows
                            break
                except Exception as e:
                    logger.debug(f"history fetch failed: {e}")

            outdoor_getter = None
            current_outdoor = None
            if getattr(adv, "weather", None):
                try:
                    current_outdoor = adv.weather.get_outdoor_temperature()
                    outdoor_getter = lambda _ts, _v=current_outdoor: _v
                except Exception:
                    pass
            if current_outdoor is None:
                current_outdoor = 10.0   # UK mean fallback

            from modules.thermal_profile import compute_profile, compute_preheat
            from modules.radiator_sizing import compute_sizing, derate_radiator

            profile = compute_profile(
                room_id=room_id,
                dimensions=dimensions,
                insulation=insulation,
                temperature_series=temp_series,
                outdoor_temp_getter=outdoor_getter,
            )

            # Live current room temp
            # Prefer sensor reading; fall back to first TRV's local_temperature
            from_temp = None
            devices = {}
            if hasattr(adv, "_get_devices"):
                try:
                    devices = adv._get_devices() or {}
                except Exception:
                    devices = {}

            def _live_temp(ieee):
                if not ieee or ieee not in devices:
                    return None
                dev = devices[ieee]
                state = dev.get("state") if isinstance(dev, dict) else getattr(dev, "state", None) or {}
                for k in ("temperature", "local_temperature", "current_temperature", "internal_temperature"):
                    v = state.get(k)
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    if f != 0 and -20 < f < 50:
                        return f
                return None

            from_temp = _live_temp(sensor_ieee)
            if from_temp is None:
                for t in (found_room.get("trvs") or []):
                    if isinstance(t, dict):
                        from_temp = _live_temp(t.get("ieee"))
                        if from_temp is not None:
                            break

            if from_temp is None:
                return {
                    "success": False,
                    "error": f"No live temperature available for room '{room_id}'",
                }

            # Resolve target
            to_temp = float(target_temp) if target_temp is not None \
                else float(found_room.get("target_temp") or 21.0)

            # Resolve installed radiator output at the current flow temp
            flow_c = float(boiler.get("flow_temperature_c", 70.0))
            rad_cfg = found_room.get("radiator") or {}
            installed_w = rad_cfg.get("watts_at_dt50")
            room_flow = rad_cfg.get("flow_temperature_c", flow_c)

            radiator_effective = None
            if installed_w:
                if room_flow == 70:
                    radiator_effective = float(installed_w)
                else:
                    radiator_effective = derate_radiator(
                        float(installed_w), float(room_flow), to_temp
                    )
            else:
                # No installed capacity known — fall back to the Phase 4
                # "required with margin" figure as a best-guess radiator.
                sizing = compute_sizing(
                    room_id=room_id,
                    w_per_k=profile.blended_w_per_k,
                    target_temp_c=to_temp,
                    design_outdoor_c=float(boiler.get("design_outdoor_c", -3.0)),
                )
                if sizing.required_watts_with_margin:
                    radiator_effective = sizing.required_watts_with_margin
                    # Derate if not at ΔT50
                    if room_flow != 70:
                        radiator_effective = derate_radiator(
                            radiator_effective, float(room_flow), to_temp
                        )

            # Confidence derives from the thermal profile blend:
            #   - measured_confidence >= 0.7 → high
            #   - 0.3 .. 0.7 → medium
            #   - else → low
            mc = profile.measured_confidence or 0.0
            if mc >= 0.7:
                conf = "high"
            elif mc >= 0.3:
                conf = "medium"
            else:
                conf = "low"

            est = compute_preheat(
                room_id=room_id,
                from_temp_c=from_temp,
                to_temp_c=to_temp,
                outdoor_temp_c=current_outdoor,
                w_per_k=profile.blended_w_per_k,
                tau_seconds=profile.tau_seconds,
                radiator_watts_effective=radiator_effective,
                confidence_in=conf,
            )

            return {
                "success": True,
                "preheat": est.to_dict(),
                "thermal": profile.to_dict(),
                "meta": {
                    "circuit_id": str(found_circuit.get("id")),
                    "circuit_name": found_circuit.get("name"),
                    "room_name": found_room.get("name"),
                    "sensor_ieee": sensor_ieee,
                    "flow_temperature_c": flow_c,
                    "design_outdoor_c": float(boiler.get("design_outdoor_c", -3.0)),
                    "live_current_temp_c": from_temp,
                    "live_outdoor_temp_c": current_outdoor,
                },
            }

        except Exception as e:
            logger.error(f"Preheat failed for {room_id}: {e}", exc_info=True)
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