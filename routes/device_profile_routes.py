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
import traceback
from typing import Any, Dict, List, Optional, Tuple
import re

from fastapi import HTTPException

logger = logging.getLogger("routes.device_profiles")

_RAW_KEY_RE = re.compile(r"cluster_([0-9a-f]+)_attr_([0-9a-f]+)$", re.I)

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


def _safe_json(value: Any) -> Any:
    """JSON-safe recursive sanitiser. Uses the codebase's prepare_for_json when
    available; falls back to a minimal local one otherwise."""
    try:
        from modules.json_helpers import prepare_for_json
        return prepare_for_json(value)
    except Exception:
        return _fallback_sanitise(value)


def _fallback_sanitise(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:    return v.decode("utf-8")
        except Exception: return v.hex()
    if hasattr(v, "value"):
        try:    return _fallback_sanitise(v.value)
        except Exception: pass
    if isinstance(v, dict):
        return {str(k): _fallback_sanitise(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_fallback_sanitise(x) for x in v]
    try:    return str(v)
    except Exception: return repr(v)


def _friendly_for(cid: int, aid: int) -> str:
    """Cluster name + zigpy attribute name. Falls back to hex when unknown."""
    cluster_name = None
    try:
        from modules.zigbee_debug import CLUSTER_NAMES
        cluster_name = CLUSTER_NAMES.get(cid)
    except Exception:
        pass

    attr_name = None
    try:
        from zigpy.zcl import Cluster
        cls = Cluster._registry.get(cid)
        if cls and getattr(cls, "attributes", None):
            attr_def = cls.attributes.get(aid)
            if attr_def is not None:
                attr_name = getattr(attr_def, "name", None)
    except Exception:
        pass

    return f"{cluster_name or f'Cluster 0x{cid:04X}'} · {attr_name or f'attr 0x{aid:04X}'}"


def _friendly_label_for_raw(raw_key: str, topology: Dict[str, Any] | None = None) -> str:
    """Topology lookup first (richest names from the live introspect),
       then zigpy's static cluster registry."""
    m = _RAW_KEY_RE.match(raw_key or "")
    if not m:
        return ""
    try:
        cid = int(m.group(1), 16)
        aid = int(m.group(2), 16)
    except ValueError:
        return ""

    # Topology may carry richer names (manufacturer-specific clusters)
    if topology:
        for ep in (topology.get("endpoints") or {}).values():
            cl = (ep.get("clusters") or {}).get(f"0x{cid:04X}")
            if not cl:
                continue
            attr = (cl.get("attributes") or {}).get(f"0x{aid:04X}")
            cname = cl.get("name")
            aname = (attr or {}).get("name")
            if cname or aname:
                return f"{cname or f'Cluster 0x{cid:04X}'} · {aname or f'attr 0x{aid:04X}'}"
    return _friendly_for(cid, aid)

def register_profile_routes(app):

    # =====================================================================
    # STATIC PATHS FIRST  (must come before /api/profiles/{profile_id})
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

    @app.get("/api/profiles/device_types")
    async def list_device_types():
        from modules.device_profiles import DEVICE_TYPES
        return {
            "success": True,
            "types": [
                {"id": k, **v} for k, v in DEVICE_TYPES.items()
            ],
        }

    @app.post("/api/profiles/import")
    async def import_profile(data: Dict[str, Any]):
        """Import a profile JSON document."""
        body = data.get("profile") if isinstance(data, dict) and "profile" in data else data
        try:
            p = _store().upsert_profile(body)
            return {"success": True, "profile": p}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
            from modules.device_profiles_apply import apply_profile
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
            from modules.device_profiles_apply import transform_state_with_profile
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
        from modules.device_profiles_apply import run_action
        return await run_action(dev, action_id, args=args)

    # =====================================================================
    # TWO-SEGMENT STATIC PREFIXES  (also before /{profile_id})
    # =====================================================================

    @app.get("/api/profiles/export/{profile_id}")
    async def export_profile(profile_id: str):
        """Download a single profile as JSON (suitable for sharing)."""
        p = _store().get_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        export_copy = dict(p)
        if "meta" in export_copy:
            meta = dict(export_copy["meta"])
            meta.pop("created_at", None)
            meta.pop("updated_at", None)
            meta["source"] = "imported"
            export_copy["meta"] = meta
        return {"success": True, "profile": export_copy}

    @app.get("/api/profiles/device/{ieee}")
    async def device_profile_view(ieee: str):
        """
        Return everything the UI needs to render the Profile tab for one
        device. Wrapped in a top-level try so a single dodgy attribute value
        doesn't produce an opaque 500 — the response carries the error.
        """
        try:
            dev = _find_device(ieee)
            if not dev:
                return {"success": False, "error": "Device not found"}

            from modules.device_profiles_apply import (
                _get_device_identity, resolve_profile_for_device,
            )

            ident = _get_device_identity(dev)
            profile = resolve_profile_for_device(dev)
            pin = _store().get_ieee_pin(ieee)
            ieee_mappings = _store().get_ieee_mappings(ieee)

            topology = _device_topology_summary(dev)

            state = getattr(dev, "state", {}) or {}
            mapped_names = set(ieee_mappings.keys())

            # 1. Unmapped keys from live state
            unmapped: List[str] = [
                k for k in state.keys()
                if k.startswith("cluster_") and k not in mapped_names
            ]
            raw_state = {k: state[k] for k in state if k.startswith("cluster_")}

            friendly_labels = {k: _friendly_label_for_raw(k, topology) for k in (unmapped + list(mapped_names))}

            # 2. Unmapped keys from topology (discovered but not yet reported)
            # This ensures they appear in the Map tab immediately after discovery.
            for ep_id, ep in (topology.get("endpoints") or {}).items():
                for cl_id, cluster in (ep.get("clusters") or {}).items():
                    for attr_id, attr in (cluster.get("attributes") or {}).items():
                        # Topology uses hex keys like '0x0006' and '0x0000'
                        # Format must match: cluster_0006_attr_0000
                        try:
                            c_part = cl_id.replace("0x", "").lower().zfill(4)
                            a_part = attr_id.replace("0x", "").lower().zfill(4)
                            raw_key = f"cluster_{c_part}_attr_{a_part}"

                            if raw_key not in mapped_names and raw_key not in unmapped:
                                unmapped.append(raw_key)
                                if raw_key not in raw_state:
                                    raw_state[raw_key] = attr.get("value")
                        except Exception:
                            continue

            return _safe_json({
                "success":        True,
                "ieee":           ieee,
                "identity":       ident,
                "profile":        profile,
                "ieee_pin":       pin,
                "ieee_mappings":  ieee_mappings,
                "topology":       topology,
                "unmapped_keys":  unmapped,
                "raw_state":      raw_state,
                "friendly_labels": friendly_labels,
            })
        except Exception as e:
            logger.exception(f"device_profile_view failed for {ieee}")
            return {
                "success": False,
                "error":   f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(limit=8),
            }

    @app.post("/api/profiles/apply/{ieee}")
    async def apply_profile_to_device(ieee: str, data: Dict[str, Any] = None):
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}
        from modules.device_profiles_apply import apply_profile
        push_reporting = True
        if isinstance(data, dict) and "push_reporting" in data:
            push_reporting = bool(data["push_reporting"])
        return await apply_profile(dev, push_reporting=push_reporting)

    @app.post("/api/profiles/configure_reporting/{ieee}")
    async def configure_reporting(ieee: str):
        dev = _find_device(ieee)
        if not dev:
            return {"success": False, "error": "Device not found"}
        from modules.device_profiles_apply import resolve_profile_for_device, apply_reporting
        profile = resolve_profile_for_device(dev)
        if not profile:
            return {"success": False, "error": "No profile applied"}
        return await apply_reporting(dev, profile)

    @app.post("/api/profiles/introspect/{ieee}")
    async def full_introspect(ieee: str, data: Dict[str, Any] = None):
        """Run full-spectrum cluster introspection. See INTROSPECTION_FIX.md."""
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

        data = data or {}
        single_ep = data.get("endpoint_id")
        single_cl = data.get("cluster_id")
        pace = float(data.get("pace_seconds") or 1.0)
        include_write = not bool(data.get("skip_write_probe"))

        targets: List[Tuple[int, int]] = []
        if single_cl is not None:
            from modules.device_profiles import _to_int
            cid = _to_int(single_cl)
            if cid is None:
                return {"success": False, "error": f"Bad cluster_id: {single_cl}"}
            eid = int(single_ep) if single_ep is not None else 1
            targets = [(eid, cid)]
        else:
            for ep_id, ep in zdev.endpoints.items():
                if ep_id == 0:
                    continue
                if single_ep is not None and ep_id != int(single_ep):
                    continue
                for cl_id in list((ep.in_clusters or {}).keys()):
                    targets.append((ep_id, cl_id))

        logger.info(
            f"[{ieee}] Introspection starting: {len(targets)} clusters, "
            f"pace={pace}s, write_probe={include_write}"
        )

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for ep_id, cl_id in targets:
            row_label = f"EP{ep_id}/0x{cl_id:04X}"
            try:
                async with asyncio.timeout(30.0):
                    res = await introspect_cluster(
                        svc, ieee, ep_id, cl_id,
                        include_write_probe=include_write,
                    )
            except asyncio.TimeoutError:
                msg = "introspect_cluster() took longer than 30s"
                logger.warning(f"[{ieee}] {row_label}: {msg}")
                errors.append({"ep": ep_id, "cluster": f"0x{cl_id:04X}", "error": msg})
                await asyncio.sleep(pace)
                continue
            except Exception as e:
                logger.warning(f"[{ieee}] {row_label}: {type(e).__name__}: {e}")
                errors.append({
                    "ep": ep_id, "cluster": f"0x{cl_id:04X}",
                    "error": f"{type(e).__name__}: {e}",
                })
                await asyncio.sleep(pace)
                continue

            else:
                attrs_list = res.get("attributes") or []
                attrs = len(attrs_list)
                cmds_dict = res.get("commands") or {}
                cmds = (len(cmds_dict.get("received") or [])
                        + len(cmds_dict.get("generated") or []))

                # Persist discovered attributes to the Zigbee cache so they appear in Discover tree
                try:
                    from modules.zigbee_cache import record_attribute_metadata
                    record_attribute_metadata(ieee, ep_id, cl_id, attrs_list)
                except Exception as e:
                    logger.warning(f"[{ieee}] Failed to cache attributes for {row_label}: {e}")

                results.append({
                    "ep": ep_id, "cluster": f"0x{cl_id:04X}",
                    "attrs": attrs, "cmds": cmds,
                })
                logger.info(f"[{ieee}] {row_label}: {attrs} attrs, {cmds} cmds")

            await asyncio.sleep(pace)

        logger.info(
            f"[{ieee}] Introspection done: {len(results)} OK, {len(errors)} errors"
        )

        return {
            "success": not errors,
            "ok_count": len(results),
            "error_count": len(errors),
            "results": results,
            "errors": errors,
        }

    # =====================================================================
    # CATCH-ALL  /{profile_id}  — declared LAST so static paths win
    # =====================================================================

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

    Per-cluster errors are isolated — one broken cluster doesn't blank the
    whole topology.
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
                "value":    _fallback_sanitise(v),
                "readable": True,
                "writable": False,
            }
        return out

    # Zigbee path — cache first, with per-row isolation
    cache_worked = False
    try:
        from modules.zigbee_cache import get_topology, get_cached_attributes
        topo = get_topology(ieee) or {}
        for ep in topo.get("endpoints", []) or []:
            try:
                ep_id = ep.get("id")                       # was "endpoint_id"
                if ep_id is None:
                    continue
                ep_out = out["endpoints"].setdefault(str(ep_id), {"clusters": {}})
                for direction in ("in", "out"):
                    for cl in ep.get(f"{direction}_clusters", []) or []:   # was "clusters"
                        try:
                            cl_id = cl.get("id")            # was "id_int"
                            if cl_id is None:
                                continue
                            cl_out = ep_out["clusters"].setdefault(
                                f"0x{cl_id:04X}",
                                {"attributes": {}, "commands": {},
                                 "name": cl.get("name"), "direction": direction},
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
                                    "value":    _fallback_sanitise(a.get("value")),
                                    "readable": a.get("readable"),
                                    "writable": a.get("writable"),
                                }
                        except Exception as e:
                            logger.debug(f"[{ieee}] cluster row skipped: {e}")
            except Exception as e:
                logger.debug(f"[{ieee}] endpoint row skipped: {e}")
        cache_worked = bool(out["endpoints"])
    except Exception as e:
        logger.debug(f"[{ieee}] zigbee_cache topology unavailable: {e}")

    if cache_worked:
        return out

    # Live fallback — walks zigpy_dev directly
    zdev = getattr(dev, "zigpy_dev", None)
    if not zdev:
        return out
    try:
        for ep_id, ep in zdev.endpoints.items():
            if ep_id == 0:
                continue
            try:
                ep_out = out["endpoints"].setdefault(str(ep_id), {"clusters": {}})
                for cl_id, cluster in (ep.in_clusters or {}).items():
                    try:
                        cl_out = ep_out["clusters"].setdefault(
                            f"0x{cl_id:04X}",
                            {"attributes": {}, "commands": {},
                             "name": getattr(cluster, "name", "")},
                        )
                        for aid, val in (getattr(cluster, "_attr_cache", {}) or {}).items():
                            try:
                                if hasattr(val, "value"):
                                    val = val.value
                                cl_out["attributes"][f"0x{aid:04X}"] = {
                                    "id":       aid,
                                    "name":     "",
                                    "type":     "",
                                    "value":    _fallback_sanitise(val),
                                    "readable": True,
                                    "writable": None,
                                }
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[{ieee}] live topology walk failed: {e}")

    return out