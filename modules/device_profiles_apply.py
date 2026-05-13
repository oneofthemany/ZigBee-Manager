# modules/device_profile_apply.py
"""
Apply a device profile to a live device.
========================================

Three things happen when a profile is applied:

1. **Capabilities** — the device's capability set is augmented with whatever
   the profile declares, so the Control tab, automation triggers, MQTT
   discovery, etc., all see the device as the right type.

2. **Actions** — actions defined in the profile are registered on the
   device under ``device.profile_actions``. The Control tab reads this
   list and presents buttons; the action runner below executes them.

3. **Reporting** — the profile's reporting rows are pushed to the device
   immediately on apply, and re-pushed on every interview-complete. The
   apply runner is idempotent; battery devices that miss the first
   attempt eventually pick it up.

4. **State transforms** — for every raw ``cluster_XXXX_attr_XXXX`` key in
   the device state that has a mapping in the profile or the IEEE
   overrides, a friendly key is added to the state dict.

This module is intentionally read-only on the profile — never mutates it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from modules.device_profiles import (
    get_profile_store, CAPABILITY_TO_HA, _to_int,
)

logger = logging.getLogger("modules.device_profile_apply")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_device_identity(device) -> Dict[str, Any]:
    """Pull model / manufacturer / vendor_id / product_id from a live device.

    Works for both ZigManDevice (Zigbee) and MatterDevice — falls back to
    sensible defaults when fields are missing.
    """
    ident: Dict[str, Any] = {
        "ieee":         str(getattr(device, "ieee", "")),
        "protocol":     getattr(device, "protocol", None) or "zigbee",
        "model":        "",
        "manufacturer": "",
        "vendor_id":    None,
        "product_id":   None,
    }

    # Zigbee path
    zdev = getattr(device, "zigpy_dev", None)
    if zdev is not None:
        ident["protocol"]     = "zigbee"
        ident["model"]        = str(getattr(zdev, "model", "") or "")
        ident["manufacturer"] = str(getattr(zdev, "manufacturer", "") or "")
        return ident

    # Matter path — only enter when there is no zigpy device backing this
    # wrapper, or when the IEEE explicitly carries the matter_ prefix.
    is_matter = (
            str(getattr(device, "ieee", "")).startswith("matter_")
            or getattr(device, "protocol", None) == "matter"
    )
    if is_matter:
        state = getattr(device, "state", None) or {}
        ident["protocol"]     = "matter"
        ident["vendor_id"]    = _to_int(state.get("vendor_id"))
        ident["product_id"]   = state.get("product_id") or state.get("part_number")
        ident["manufacturer"] = state.get("vendor_name") or ident["manufacturer"]
        ident["model"]        = state.get("product_name") or ident["model"]

    return ident


def resolve_profile_for_device(device) -> Optional[Dict[str, Any]]:
    """Return the profile that applies to this device, or None."""
    ident = _get_device_identity(device)
    if not ident["ieee"]:
        return None
    return get_profile_store().get_profile_for_device(**ident)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def _apply_capabilities(device, profile: Dict[str, Any]) -> List[str]:
    """
    Merge profile capabilities into device.capabilities (set). Returns
    the list of capabilities newly added.
    """
    added: List[str] = []
    caps_obj = getattr(device, "capabilities", None)
    if caps_obj is None:
        # The device hasn't been wrapped with a DeviceCapabilities; fall
        # back to a plain set on the device itself.
        existing = set(getattr(device, "_profile_capabilities", set()) or [])
        new = set(profile.get("capabilities") or [])
        device._profile_capabilities = existing | new
        added = sorted(new - existing)
    else:
        existing = set(getattr(caps_obj, "_capabilities", set()) or set())
        for cap in profile.get("capabilities") or []:
            if cap not in existing:
                try:
                    caps_obj._capabilities.add(cap)
                    added.append(cap)
                except Exception:
                    pass
    if added:
        logger.info(f"[{device.ieee}] Profile added capabilities: {added}")
    return added


# ---------------------------------------------------------------------------
# State transforms
# ---------------------------------------------------------------------------

def transform_state_with_profile(device, state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add friendly keys to *state* based on the profile + IEEE overrides.

    Pure: returns a new dict (does not mutate ``state``).
    """
    out = dict(state or {})
    profile = resolve_profile_for_device(device)
    store   = get_profile_store()
    ieee    = str(getattr(device, "ieee", ""))
    mappings = store.get_ieee_mappings(ieee) or {}

    # Build a quick lookup of (cluster_id, attr_id) -> mapping from the profile
    profile_attrs: Dict[Tuple[int, int], Dict[str, Any]] = {}
    if profile:
        for ep in profile["endpoints"].values():
            for cl_hex, cl in ep.get("clusters", {}).items():
                cid = _to_int(cl_hex)
                if cid is None:
                    continue
                for at_hex, attr in (cl.get("attributes") or {}).items():
                    aid = _to_int(at_hex)
                    if aid is None:
                        continue
                    profile_attrs[(cid, aid)] = attr

    for key, raw_value in list(out.items()):
        if not key.startswith("cluster_"):
            continue
        # cluster_XXXX_attr_YYYY
        try:
            parts = key.split("_")
            if len(parts) < 4 or parts[2] != "attr":
                continue
            cid = int(parts[1], 16)
            aid = int(parts[3], 16)
        except (ValueError, IndexError):
            continue

        mapping = mappings.get(key) or profile_attrs.get((cid, aid))
        if not mapping or not mapping.get("name"):
            continue

        try:
            value = _apply_attr_transform(raw_value, mapping)
        except Exception as e:
            logger.debug(f"[{ieee}] transform failed for {key}: {e}")
            continue

        # Don't clobber an existing friendly key that might have come from a
        # dedicated handler — handlers always win.
        if mapping["name"] not in out:
            out[mapping["name"]] = value

    return out


def _apply_attr_transform(value: Any, mapping: Dict[str, Any]) -> Any:
    """Apply scale / value_map / invert to a raw value."""
    if value is None:
        return None

    # value_map (e.g. {"0": "closed", "1": "open"}) — match by str(value)
    vmap = mapping.get("value_map")
    if isinstance(vmap, dict) and vmap:
        key = str(value)
        if key in vmap:
            return vmap[key]

    # invert (boolean attributes)
    if mapping.get("invert"):
        if isinstance(value, bool):
            value = not value
        elif isinstance(value, (int, float)):
            value = 1 - value if value in (0, 1) else value

    # scale
    scale = mapping.get("scale")
    if scale and isinstance(value, (int, float)) and scale not in (0, 1):
        try:
            value = value / float(scale)
        except Exception:
            pass

    return value


# ---------------------------------------------------------------------------
# Reporting configuration
# ---------------------------------------------------------------------------

async def apply_reporting(device, profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Push the profile's reporting rows to the device. Idempotent.

    Returns a result dict with per-row success/failure for the API to
    surface in the UI.
    """
    rows = profile.get("reporting") or []
    if not rows:
        return {"success": True, "configured": 0, "results": []}

    zdev = getattr(device, "zigpy_dev", None)
    if zdev is None:
        return {"success": False, "error": "Reporting requires Zigbee device", "results": []}

    results: List[Dict[str, Any]] = []
    ok = 0
    for r in rows:
        ep_id = r["ep"]
        cid   = _to_int(r["cluster"])
        aid   = _to_int(r["attr"])
        if ep_id is None or cid is None or aid is None:
            results.append({**r, "ok": False, "error": "bad row"})
            continue
        ep = zdev.endpoints.get(ep_id)
        if not ep:
            results.append({**r, "ok": False, "error": f"endpoint {ep_id} missing"})
            continue
        cluster = (ep.in_clusters or {}).get(cid)
        if not cluster:
            results.append({**r, "ok": False, "error": f"cluster 0x{cid:04X} missing"})
            continue
        try:
            try:
                await cluster.bind()
            except Exception as e:
                logger.debug(f"[{device.ieee}] bind failed for 0x{cid:04X}: {e}")
            async with asyncio.timeout(8.0):
                await cluster.configure_reporting(
                    aid,
                    min_interval=int(r.get("min") or 30),
                    max_interval=int(r.get("max") or 600),
                    reportable_change=r.get("delta", 1),
                )
            ok += 1
            results.append({**r, "ok": True})
        except asyncio.TimeoutError:
            results.append({**r, "ok": False, "error": "timeout"})
        except Exception as e:
            results.append({**r, "ok": False, "error": str(e)})

    logger.info(f"[{device.ieee}] Reporting applied: {ok}/{len(rows)} OK")
    return {"success": ok == len(rows), "configured": ok, "results": results}


# ---------------------------------------------------------------------------
# Action runner
# ---------------------------------------------------------------------------

async def run_action(device, action_id: str, args: Optional[List[Any]] = None) -> Dict[str, Any]:
    """
    Execute a profile-defined action on a device.

    Two action shapes are supported:

    * ``command`` action — single ZCL command to a cluster (optionally with args).
    * ``writes`` action — one or more attribute writes. If ``atomic`` is set
      and there's more than one write, they're batched into a single
      ``write_attributes`` call against the *same* cluster, matching the
      Hive SLR1c protocol pattern.
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return {"success": False, "error": "No profile applied to this device"}

    action = next((a for a in profile.get("actions") or [] if a.get("id") == action_id), None)
    if not action:
        return {"success": False, "error": f"Action {action_id!r} not defined in profile"}

    zdev = getattr(device, "zigpy_dev", None)
    if zdev is None:
        return {"success": False, "error": "Profile actions require a Zigbee device"}

    ep_id = action["ep"]
    cl_id = _to_int(action["cluster"])
    ep = zdev.endpoints.get(ep_id) if ep_id is not None else None
    if not ep:
        return {"success": False, "error": f"Endpoint {ep_id} missing"}
    cluster = (ep.in_clusters or {}).get(cl_id) or (ep.out_clusters or {}).get(cl_id)
    if not cluster:
        return {"success": False, "error": f"Cluster 0x{cl_id:04X} missing on EP{ep_id}"}

    # Path A — writes (preferred for atomic multi-write actions)
    writes = action.get("writes") or []
    if writes:
        try:
            # Group writes by (ep, cluster) — usually all on one cluster
            grouped: Dict[Tuple[int, int], Dict[int, Any]] = {}
            for w in writes:
                w_ep = w.get("ep", ep_id)
                w_cl = _to_int(w.get("cluster")) if w.get("cluster") else cl_id
                w_at = _to_int(w.get("attr"))
                if w_at is None:
                    continue
                grouped.setdefault((w_ep, w_cl), {})[w_at] = w.get("value")
            for (w_ep, w_cl), attrs in grouped.items():
                target_ep = zdev.endpoints.get(w_ep)
                if not target_ep:
                    return {"success": False, "error": f"Endpoint {w_ep} missing"}
                target_cl = (target_ep.in_clusters or {}).get(w_cl)
                if not target_cl:
                    return {"success": False, "error": f"Cluster 0x{w_cl:04X} missing"}
                async with asyncio.timeout(10.0):
                    await target_cl.write_attributes(attrs)
            return {"success": True, "action": action_id, "writes": len(writes)}
        except asyncio.TimeoutError:
            return {"success": False, "error": "write timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Path B — single command
    cmd_id = _to_int(action.get("command"))
    if cmd_id is None:
        return {"success": False, "error": "Action has neither writes nor command"}

    cmd_args = list(args or []) or list(action.get("args") or [])
    try:
        async with asyncio.timeout(10.0):
            await cluster.command(cmd_id, *cmd_args)
        return {"success": True, "action": action_id, "command": f"0x{cmd_id:02X}"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Top-level apply
# ---------------------------------------------------------------------------

async def apply_profile(
        device,
        *,
        push_reporting: bool = True,
) -> Dict[str, Any]:
    """
    Apply the matching profile to a device. Called from the device
    lifecycle when an interview completes, and also on demand from the API.

    Idempotent — re-applying never breaks anything.
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return {"success": True, "applied": False, "reason": "No matching profile"}

    ieee = str(getattr(device, "ieee", ""))
    summary: Dict[str, Any] = {
        "success":      True,
        "applied":      True,
        "profile_id":   profile["id"],
        "device_type":  profile["device_type"],
        "capabilities": [],
        "actions":      [],
        "reporting":    None,
    }

    # 1. Capabilities
    summary["capabilities"] = _apply_capabilities(device, profile)

    # 2. Actions — record them on the device for the Control tab to show
    actions = profile.get("actions") or []
    try:
        device.profile_actions = actions
    except Exception:
        # Some device wrappers may forbid attribute assignment
        setattr(device, "_profile_actions", actions)
    summary["actions"] = [a["id"] for a in actions]

    # 3. Reporting — opt-out via push_reporting=False
    if push_reporting and profile.get("reporting"):
        try:
            summary["reporting"] = await apply_reporting(device, profile)
        except Exception as e:
            logger.exception(f"[{ieee}] apply_reporting failed: {e}")
            summary["reporting"] = {"success": False, "error": str(e)}

    # 4. State transform — re-process current state so friendly keys appear
    try:
        if hasattr(device, "state") and isinstance(device.state, dict):
            new_state = transform_state_with_profile(device, device.state)
            # Update via the device's own update path so listeners see it
            if hasattr(device, "update_state"):
                added = {k: v for k, v in new_state.items() if k not in device.state}
                if added:
                    device.update_state(added)
            else:
                device.state.update(new_state)
    except Exception as e:
        logger.debug(f"[{ieee}] state retransform skipped: {e}")

    logger.info(
        f"[{ieee}] Profile applied: {profile['id']} "
        f"(type={profile['device_type']}, caps={summary['capabilities']}, "
        f"actions={len(summary['actions'])})"
    )
    return summary


# ---------------------------------------------------------------------------
# MQTT discovery generation for a profile
# ---------------------------------------------------------------------------

def build_discovery_configs(device) -> List[Dict[str, Any]]:
    """
    Generate Home Assistant MQTT discovery configs for the friendly keys
    the profile exposes. Only fills *gaps* — if a built-in handler has
    already produced a discovery config for the same object_id, the
    caller is responsible for not duplicating it (de-duped at MQTT layer
    by ``unique_id``).
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return []

    configs: List[Dict[str, Any]] = []
    # Collect friendly names + their attribute metadata
    for ep in profile["endpoints"].values():
        for cl in ep.get("clusters", {}).values():
            for attr in (cl.get("attributes") or {}).values():
                name = attr.get("name")
                if not name:
                    continue
                cap = attr.get("capability") or _infer_capability(attr, profile)
                if not cap or cap not in CAPABILITY_TO_HA:
                    continue
                component = CAPABILITY_TO_HA[cap]["component"]
                cfg: Dict[str, Any] = {
                    "component": component,
                    "object_id": name,
                    "config": {
                        "name": _humanise(name),
                        "value_template": f"{{{{ value_json.{name} }}}}",
                    },
                }
                if attr.get("device_class"):
                    cfg["config"]["device_class"] = attr["device_class"]
                if attr.get("unit"):
                    cfg["config"]["unit_of_measurement"] = attr["unit"]
                if component == "binary_sensor":
                    vmap = attr.get("value_map") or {}
                    on_v  = next((k for k, v in vmap.items() if v in ("open", "wet", "motion", "on")), None)
                    off_v = next((k for k, v in vmap.items() if v in ("closed", "dry", "no_motion", "off")), None)
                    if on_v and off_v:
                        cfg["config"]["payload_on"]  = on_v
                        cfg["config"]["payload_off"] = off_v
                if component == "sensor":
                    cfg["config"]["state_class"] = "measurement"
                configs.append(cfg)

    return configs


def _infer_capability(attr: Dict[str, Any], profile: Dict[str, Any]) -> Optional[str]:
    """Best-effort capability inference from attribute mapping fields."""
    name = (attr.get("name") or "").lower()
    if "temp" in name:        return "temperature"
    if "humid" in name:       return "humidity"
    if "battery" in name:     return "battery"
    if "press" in name:       return "pressure"
    if "lux" in name or "illum" in name: return "illuminance"
    if "power" in name:       return "power"
    if "voltage" in name:     return "voltage"
    if "current" in name:     return "current"
    if name == "contact":     return "contact"
    if name in ("motion", "occupancy"): return "motion"
    if name in ("leak", "water_leak"):  return "water_leak"
    if name == "smoke":       return "smoke"
    if name == "vibration":   return "vibration"
    if name in ("state", "switch", "on_off"): return "on_off"
    # Fall back to first capability of the profile if any single one is implied
    dt = profile.get("device_type")
    if dt == "contact_sensor":     return "contact"
    if dt == "motion_sensor":      return "motion"
    if dt == "temperature_sensor": return "temperature"
    return None


def _humanise(name: str) -> str:
    return name.replace("_", " ").strip().title()