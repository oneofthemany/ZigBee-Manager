# routes/device_profile_routes.py
"""
REST routes for the unified device-profile system.

Mounted from main.py with::

    from routes.device_profile_routes import register_profile_routes
    register_profile_routes(app)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

logger = logging.getLogger("routes.device_profiles")


# Late-binding helpers — keeps this module testable on its own
def _store():
    from modules.device_profiles import get_profile_store
    return get_profile_store()


def _get_service():
    """Resolve the live ZigbeeService — same accessor used everywhere else."""
    try:
        from core.service import get_zigbee_service
        return get_zigbee_service()
    except Exception:
        pass
    try:
        from main import get_zigbee_service  # type: ignore
        return get_zigbee_service()
    except Exception:
        return None


def _get_matter_bridge():
    try:
        from main import get_matter_bridge  # type: ignore
        return get_matter_bridge()
    except Exception:
        return None


def _find_device(ieee: str):
    """Locate a device across Zigbee + Matter."""
    if ieee.startswith("matter_"):
        bridge = _get_matter_bridge()
        if bridge:
            return bridge.devices.get(ieee)
    svc = _get_service()
    if svc:
        return svc.devices.get(ieee)
    return None


def register_profile_routes(app):

    # =====================================================================
    # PROFILE CRUD - SPECIFIC ROUTES FIRST
    # =====================================================================

    @app.get("/api/profiles")
    async def list_profiles(source: Optional[str] = None):
        """List all profiles. Optional ``source`` filter: user | bundled."""
        return {
            "success":  True,
            "profiles": _store().list_profiles(source=source),
            "ieee":     _store().list_ieee_state(),
        }

    @app.post("/api/profiles")
    async def create_or_update_profile(data: Dict[str, Any]):
        """Create or update a user profile. Body = profile JSON."""
        try:
            p = _store().upsert_profile(data)
            return {"success": True, "profile": p}
        except Exception as e:
            logger.exception("upsert_profile failed")
            return {"success": False, "error": str(e)}

    @app.post("/api/profiles/import")
    async def import_profile(data: Dict[str, Any]):
        """Import a profile JSON document."""
        body = data.get("profile") if isinstance(data, dict) and "profile" in data else data
        try:
            p = _store().upsert_profile(body)
            return {"success": True, "profile": p}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =====================================================================
    # DEVICE TYPES (for the UI dropdown)
    # =====================================================================

    @app.get("/api/profiles/device_types")
    async def list_device_types():
        from modules.device_profiles import DEVICE_TYPES
        return {
            "success": True,
            "types": [
                {"id": k, **v} for k, v in DEVICE_TYPES.items()
            ],
        }

    # =====================================================================
    # IEEE PINS + LEGACY MAPPINGS
    # =====================================================================

    @app.post("/api/profiles/pin")
    async def pin_ieee(data: Dict[str, Any]):
        ieee = data.get("ieee")
        pid = data.get("profile_id")
        if not ieee or not pid:
            return {"success": False, "error": "ieee and profile_id required"}
        if not _store().pin_ieee(ieee, pid):
            return {"success": False, "error": "Unknown profile_id"}
        # Apply immediately if device is online
        dev = _find_device(ieee)
        if dev:
            from modules.device_profile_apply import apply_profile
            asyncio.create_task(apply_profile(dev))
        return {"success": True}

    @app.post("/api/profiles/unpin")
    async def unpin_ieee(data: Dict[str, Any]):
        ieee = data.get("ieee")
        if not ieee:
            return {"success": False, "error": "ieee required"}
        return {"success": _store().unpin_ieee(ieee)}

    @app.post("/api/profiles/ieee_mapping")
    async def set_ieee_mapping(data: Dict[str, Any]):
        ieee   = data["ieee"]
        raw    = data["raw_key"]
        name   = data["friendly_name"]
        scale  = data.get("scale", 1)
        unit   = data.get("unit", "")
        dclass = data.get("device_class", "")
        invert = bool(data.get("invert", False))
        _store().set_ieee_mapping(ieee, raw, name, scale=scale, unit=unit,
                                  device_class=dclass, invert=invert)
        # Refresh device state so the friendly key appears right away
        dev = _find_device(ieee)
        if dev:
            from modules.device_profile_apply import transform_state_with_profile
            try:
                new = transform_state_with_profile(dev, dev.state)
                added = {k: v for k, v in new.items() if k not in dev.state}
                if added and hasattr(dev, "update_state"):
                    dev.update_state(added)
            except Exception:
                pass
        return {"success": True}

    @app.delete("/api/profiles/ieee_mapping")
    async def remove_ieee_mapping(data: Dict[str, Any]):
        ieee = data["ieee"]
        raw  = data["raw_key"]
        return {"success": _store().remove_ieee_mapping(ieee, raw)}

    # =====================================================================
    # DEVICE-CENTRIC VIEW
    # =====================================================================

    @app.get("/api/profiles/device/{ieee}")
    async def device_profile_view(ieee: str):
        """
        Return everything the UI needs to render the Profile tab for one
        device: the matching profile (or null), the IEEE-pinned profile (if
        any), the per-IEEE attribute mappings, the cached cluster/attribute
        topology, and the currently-unmapped raw keys.
        """
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}

        from modules.device_profile_apply import (
            _get_device_identity, resolve_profile_for_device,
        )

        ident = _get_device_identity(dev)
        profile = resolve_profile_for_device(dev)
        pin = _store().get_ieee_pin(ieee)
        ieee_mappings = _store().get_ieee_mappings(ieee)

        # Cached topology — endpoints / clusters / attributes
        topology = _device_topology_summary(dev)

        # Unmapped raw keys currently in state
        unmapped: List[str] = []
        state = getattr(dev, "state", {}) or {}
        mapped_names = set(ieee_mappings.keys())
        for k in state.keys():
            if k.startswith("cluster_") and k not in mapped_names:
                unmapped.append(k)

        return {
            "success":        True,
            "ieee":           ieee,
            "identity":       ident,
            "profile":        profile,
            "ieee_pin":       pin,
            "ieee_mappings":  ieee_mappings,
            "topology":       topology,
            "unmapped_keys":  unmapped,
            # SAFELY serialise raw values so enums/bytes don't cause 500 errors
            "raw_state":      {k: _safe_jsonify(state[k]) for k in state if k.startswith("cluster_")},
        }

    # =====================================================================
    # APPLY / RUN / CONFIG
    # =====================================================================

    @app.post("/api/profiles/apply/{ieee}")
    async def apply_profile_to_device(ieee: str, data: Dict[str, Any] = None):
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}
        from modules.device_profile_apply import apply_profile
        push_reporting = True
        if isinstance(data, dict) and "push_reporting" in data:
            push_reporting = bool(data["push_reporting"])
        return await apply_profile(dev, push_reporting=push_reporting)

    @app.post("/api/profiles/run_action")
    async def run_profile_action(data: Dict[str, Any]):
        ieee = data.get("ieee")
        action_id = data.get("action_id")
        args = data.get("args") or []
        if not ieee or not action_id:
            return {"success": False, "error": "ieee and action_id required"}
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}
        from modules.device_profile_apply import run_action
        return await run_action(dev, action_id, args=args)

    @app.post("/api/profiles/configure_reporting/{ieee}")
    async def configure_reporting(ieee: str):
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}
        from modules.device_profile_apply import resolve_profile_for_device, apply_reporting
        profile = resolve_profile_for_device(dev)
        if not profile:
            return {"success": False, "error": "No profile applied"}
        return await apply_reporting(dev, profile)

    # =====================================================================
    # FULL INTROSPECTION (one-button "discover everything")
    # =====================================================================

    @app.post("/api/profiles/introspect/{ieee}")
    async def full_introspect(ieee: str):
        """
        Run the full-spectrum cluster introspection against every cluster
        the device exposes. Results are written to the zigbee_cache and
        returned to the caller for the UI to render.

        For Matter devices this is a no-op — Matter attributes are already
        published by the matter-server.
        """
        svc = _get_service()
        if not svc:
            return {"success": False, "error": "Zigbee service unavailable"}
        if ieee.startswith("matter_"):
            return {"success": False, "error": "Introspection is Zigbee-only"}

        dev = svc.devices.get(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}

        try:
            from modules.diag_attributes import introspect_cluster
        except Exception as e:
            return {"success": False, "error": f"diag module unavailable: {e}"}

        zdev = getattr(dev, "zigpy_dev", None)
        if not zdev:
            return {"success": False, "error": "Device has no zigpy backing"}

        # Pace requests so we don't flood the radio
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for ep_id, ep in zdev.endpoints.items():
            if ep_id == 0:
                continue
            for cl_id in list((ep.in_clusters or {}).keys()):
                try:
                    res = await introspect_cluster(svc, ieee, ep_id, cl_id)
                    results.append({
                        "ep": ep_id, "cluster": f"0x{cl_id:04X}",
                        "attrs": len(res.get("attributes") or []),
                        "cmds": (
                                len((res.get("commands") or {}).get("received") or [])
                                + len((res.get("commands") or {}).get("generated") or [])
                        ),
                    })
                except Exception as e:
                    errors.append({"ep": ep_id, "cluster": f"0x{cl_id:04X}", "error": str(e)})
                await asyncio.sleep(0.2)
        return {"success": not errors, "results": results, "errors": errors}

    # =====================================================================
    # PROFILE CRUD - WILDCARD ROUTES
    # =====================================================================

    @app.get("/api/profiles/export/{profile_id}")
    async def export_profile(profile_id: str):
        """Download a single profile as JSON (suitable for sharing)."""
        p = _store().get_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        # Strip volatile meta before export so two exports of the same profile
        # produce byte-identical JSON (helps with sharing / version control).
        export_copy = dict(p)
        if "meta" in export_copy:
            meta = dict(export_copy["meta"])
            meta.pop("created_at", None)
            meta.pop("updated_at", None)
            meta["source"] = "imported"
            export_copy["meta"] = meta
        return {"success": True, "profile": export_copy}

    @app.get("/api/profiles/{profile_id}")
    async def get_profile(profile_id: str):
        p = _store().get_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"success": True, "profile": p}

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str):
        if not _store().delete_profile(profile_id):
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"success": True, "deleted": profile_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_topology_summary(dev) -> Dict[str, Any]:
    """
    Build a compact topology summary suitable for the UI's tree view.
    Reads from the cache for Zigbee, and from the live state for Matter.
    """
    out: Dict[str, Any] = {"endpoints": {}}

    ieee = str(getattr(dev, "ieee", ""))

    # Matter: state holds "ep/cluster/attr" keys
    if ieee.startswith("matter_"):
        state = getattr(dev, "state", {}) or {}
        for k, v in state.items():
            parts = str(k).split("/")
            if len(parts) != 3:
                continue
            try:
                ep_id = int(parts[0])
                cl_id = int(parts[1])
                at_id = int(parts[2])
            except ValueError:
                continue
            ep = out["endpoints"].setdefault(str(ep_id), {"clusters": {}})
            cl = ep["clusters"].setdefault(f"0x{cl_id:04X}", {"attributes": {}, "commands": {}})
            cl["attributes"][f"0x{at_id:04X}"] = {
                "id":       at_id,
                "name":     "",
                "value":    _safe_jsonify(v),
                "readable": True,
                "writable": False,
            }
        return out

    # Zigbee: pull from the cache (richer metadata) with live zigpy_dev fallback
    try:
        from modules.zigbee_cache import get_topology, get_cached_attributes
        topo = get_topology(ieee) or {}
        for ep in topo.get("endpoints", []) or []:
            ep_id = ep.get("endpoint_id")
            ep_out = out["endpoints"].setdefault(str(ep_id), {"clusters": {}})
            for cl in ep.get("clusters", []) or []:
                cl_id = cl.get("id_int")
                if cl_id is None:
                    continue
                cl_out = ep_out["clusters"].setdefault(
                    f"0x{cl_id:04X}",
                    {"attributes": {}, "commands": {}, "name": cl.get("name"),
                     "direction": cl.get("direction")},
                )
                cached = get_cached_attributes(ieee, ep_id, cl_id) or {}
                for a in cached.get("attributes", []) or []:
                    aid = a.get("id_int")
                    if aid is None:
                        continue
                    cl_out["attributes"][f"0x{aid:04X}"] = {
                        "id":       aid,
                        "name":     a.get("name") or "",
                        "type":     a.get("type") or "",
                        "value":    _safe_jsonify(a.get("value")),
                        "readable": a.get("readable"),
                        "writable": a.get("writable"),
                    }
    except Exception as e:
        logger.debug(f"[{ieee}] topology summary fallback: {e}")
        # Fall back to live zigpy attribute cache
        zdev = getattr(dev, "zigpy_dev", None)
        if zdev:
            for ep_id, ep in zdev.endpoints.items():
                if ep_id == 0:
                    continue
                ep_out = out["endpoints"].setdefault(str(ep_id), {"clusters": {}})
                for cl_id, cluster in (ep.in_clusters or {}).items():
                    cl_out = ep_out["clusters"].setdefault(
                        f"0x{cl_id:04X}",
                        {"attributes": {}, "commands": {}, "name": getattr(cluster, "name", "")},
                    )
                    for aid, val in (getattr(cluster, "_attr_cache", {}) or {}).items():
                        if hasattr(val, "value"):
                            val = val.value
                        cl_out["attributes"][f"0x{aid:04X}"] = {
                            "id":       aid,
                            "name":     "",
                            "type":     "",
                            "value":    _safe_jsonify(val),
                            "readable": True,
                            "writable": None,
                        }
    return out


def _safe_jsonify(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    try:
        return str(v)
    except Exception:
        return repr(v)