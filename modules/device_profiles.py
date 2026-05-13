# modules/device_profiles.py
"""
Device Profiles — unified Zigbee + Matter device modelling framework.
=====================================================================

This replaces (and is backwards-compatible with) the older split system of
``modules/device_overrides.py`` (Zigbee attribute renaming) and
``modules/matter_definitions.py`` (Matter endpoint mapping).

A *profile* is a JSON document describing a device model in protocol-agnostic
terms. The same schema covers Zigbee and Matter; only the ``protocol`` field
and the contents of ``endpoints[*].clusters`` change.

Schema (canonical, v1)
----------------------
::

    {
      "schema_version": 1,
      "id": "lumi.sensor_magnet.aq2",          # stable identifier
      "protocol": "zigbee",                     # "zigbee" | "matter"
      "match": {                                # how a device gets matched
        "model":        "lumi.sensor_magnet.aq2",
        "manufacturer": "LUMI",
        "vendor_id":    null,                   # matter only
        "product_id":   null                    # matter only
      },
      "device_type": "contact_sensor",          # see DEVICE_TYPES below
      "capabilities": ["contact", "battery"],
      "endpoints": {
        "1": {
          "role": "primary",                    # primary | controller | sensor | ...
          "label": "Sensor",
          "group": "",                          # used for button grouping
          "clusters": {
            "0x0500": {
              "attributes": {
                "0x0000": {
                  "name":         "contact",
                  "scale":        1,
                  "unit":         "",
                  "device_class": "door",
                  "invert":       true,
                  "value_map":    {"0": "closed", "1": "open"}
                }
              },
              "commands": {}
            }
          }
        }
      },
      "actions": [
        {
          "id":       "toggle",
          "label":    "Toggle",
          "ep":       1,
          "cluster":  "0x0006",
          "command":  "0x02",
          "args":     [],
          "writes":   []                        # alternative to command:
                                                # [{ep, cluster, attr, value, type}]
        }
      ],
      "reporting": [
        {
          "ep": 1, "cluster": "0x0402", "attr": "0x0000",
          "min": 60, "max": 300, "delta": 10
        }
      ],
      "ieee_overrides": {                       # legacy per-device mappings
        "00:11:22:...": {
          "cluster_mappings": {
            "cluster_0500_attr_0000": {"name": "contact"}
          }
        }
      },
      "meta": {
        "author":     "user@local",
        "source":     "user",                   # user | bundled | imported
        "created_at": 1700000000,
        "updated_at": 1700000000
      }
    }

A second file, ``ieee_overrides.json``, holds per-IEEE pinning to a profile
(``{ieee: profile_id}``) plus device-specific attribute mappings that haven't
been promoted to a profile yet.

Storage
-------
Profiles live as one JSON file per profile under ``data/device_profiles/``,
keyed by the ``id`` field. Bundled profiles (shipped with the app) live under
``data/community_profiles/`` and are read-only. User profiles override
bundled ones with the same id.

Lookup precedence (highest first)
---------------------------------
1. IEEE-pinned profile id (explicit user assignment)
2. User profile matching (protocol, model, manufacturer)
3. User profile matching (protocol, vendor_id, product_id) — Matter
4. Bundled profile matching (same priorities)
5. None (device runs on built-in handlers / generic fallback)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("modules.device_profiles")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR             = os.environ.get("ZMM_DATA_DIR", "./data")
USER_PROFILES_DIR    = os.path.join(DATA_DIR, "device_profiles")
BUNDLED_PROFILES_DIR = os.path.join(DATA_DIR, "community_profiles")
IEEE_OVERRIDES_FILE  = os.path.join(DATA_DIR, "ieee_overrides.json")

# Legacy files — read once at startup for migration
LEGACY_OVERRIDES_FILE       = os.path.join(DATA_DIR, "device_overrides.json")
LEGACY_MATTER_DEFS_DIR      = os.path.join(DATA_DIR, "..", "config", "matter_definitions")

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Canonical device types
# ---------------------------------------------------------------------------

DEVICE_TYPES: Dict[str, Dict[str, Any]] = {
    "contact_sensor":     {"label": "Contact sensor",     "capabilities": ["contact", "battery"]},
    "motion_sensor":      {"label": "Motion sensor",      "capabilities": ["motion", "battery"]},
    "temperature_sensor": {"label": "Temperature sensor", "capabilities": ["temperature", "battery"]},
    "humidity_sensor":    {"label": "Humidity sensor",    "capabilities": ["humidity", "battery"]},
    "leak_sensor":        {"label": "Leak sensor",        "capabilities": ["water_leak", "battery"]},
    "smoke_sensor":       {"label": "Smoke sensor",       "capabilities": ["smoke", "battery"]},
    "vibration_sensor":   {"label": "Vibration sensor",   "capabilities": ["vibration", "battery"]},
    "button":             {"label": "Button / remote",    "capabilities": ["button", "battery"]},
    "rotary":             {"label": "Rotary controller",  "capabilities": ["rotary", "battery"]},
    "switch":             {"label": "Switch / relay",     "capabilities": ["on_off"]},
    "dimmer":             {"label": "Dimmer",             "capabilities": ["on_off", "brightness"]},
    "light":              {"label": "Light",              "capabilities": ["on_off", "brightness", "color_temp"]},
    "color_light":        {"label": "Colour light",       "capabilities": ["on_off", "brightness", "color"]},
    "plug":               {"label": "Smart plug",         "capabilities": ["on_off", "power"]},
    "thermostat":         {"label": "Thermostat",         "capabilities": ["thermostat", "temperature"]},
    "trv":                {"label": "Radiator valve",     "capabilities": ["thermostat", "temperature", "battery"]},
    "blind":              {"label": "Blind / cover",      "capabilities": ["cover"]},
    "lock":               {"label": "Lock",               "capabilities": ["lock", "battery"]},
    "generic":            {"label": "Generic",            "capabilities": []},
}


# Mapping from a capability name to a Home Assistant MQTT discovery component.
# Used by the discovery generator. Multiple caps can map to the same component
# with different ``object_id`` values.
CAPABILITY_TO_HA = {
    "contact":     {"component": "binary_sensor"},
    "motion":      {"component": "binary_sensor"},
    "water_leak":  {"component": "binary_sensor"},
    "smoke":       {"component": "binary_sensor"},
    "vibration":   {"component": "binary_sensor"},
    "temperature": {"component": "sensor"},
    "humidity":    {"component": "sensor"},
    "battery":     {"component": "sensor"},
    "power":       {"component": "sensor"},
    "voltage":     {"component": "sensor"},
    "current":     {"component": "sensor"},
    "illuminance": {"component": "sensor"},
    "pressure":    {"component": "sensor"},
    "on_off":      {"component": "switch"},
    "brightness":  {"component": "light"},
    "color_temp":  {"component": "light"},
    "color":       {"component": "light"},
    "cover":       {"component": "cover"},
    "lock":        {"component": "lock"},
    "thermostat":  {"component": "climate"},
    "button":      {"component": "device_automation"},
    "rotary":      {"component": "device_automation"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^0x([0-9A-Fa-f]+)$")


def _to_int(v: Any) -> Optional[int]:
    """Accept '0x0006', '6', 6, or None and return int or None."""
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        m = _HEX_RE.match(s)
        if m:
            return int(m.group(1), 16)
        try:
            return int(s, 0)
        except ValueError:
            return None
    return None


def _safe_id(s: str) -> str:
    """Filename-safe profile id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_")


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Profile validation / normalisation
# ---------------------------------------------------------------------------

def normalise_profile(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make a profile dict canonical. Idempotent. Never raises — invalid bits
    are dropped with a debug log so the rest of the app keeps working.
    """
    out: Dict[str, Any] = {}
    out["schema_version"] = int(p.get("schema_version") or SCHEMA_VERSION)

    pid = p.get("id") or p.get("profile_id") or ""
    out["id"] = _safe_id(pid) if pid else ""

    protocol = (p.get("protocol") or "zigbee").lower()
    if protocol not in ("zigbee", "matter"):
        protocol = "zigbee"
    out["protocol"] = protocol

    # match
    match_in = p.get("match") or {}
    out["match"] = {
        "model":        str(match_in.get("model") or p.get("model") or "").strip(),
        "manufacturer": str(match_in.get("manufacturer") or p.get("manufacturer") or "").strip(),
        "vendor_id":    _to_int(match_in.get("vendor_id") or p.get("vendor_id")),
        "product_id":   match_in.get("product_id") or p.get("product_id") or None,
    }

    # device type
    dt = p.get("device_type") or "generic"
    if dt not in DEVICE_TYPES:
        logger.debug(f"Unknown device_type {dt!r} in profile {out['id']!r}, defaulting to generic")
        dt = "generic"
    out["device_type"] = dt

    # capabilities (free-form list of strings; defaults from device_type)
    caps = p.get("capabilities")
    if not isinstance(caps, list) or not caps:
        caps = list(DEVICE_TYPES[dt]["capabilities"])
    out["capabilities"] = sorted({str(c) for c in caps if c})

    # endpoints
    eps_out: Dict[str, Dict[str, Any]] = {}
    for ep_key, ep_val in (p.get("endpoints") or {}).items():
        if not isinstance(ep_val, dict):
            continue
        try:
            ep_id = int(ep_key)
        except (TypeError, ValueError):
            continue
        clusters_out: Dict[str, Dict[str, Any]] = {}
        for cl_key, cl_val in (ep_val.get("clusters") or {}).items():
            cl_id = _to_int(cl_key)
            if cl_id is None or not isinstance(cl_val, dict):
                continue
            cluster_norm: Dict[str, Any] = {"attributes": {}, "commands": {}}
            for a_key, a_val in (cl_val.get("attributes") or {}).items():
                a_id = _to_int(a_key)
                if a_id is None:
                    continue
                if isinstance(a_val, str):
                    a_val = {"name": a_val}
                if not isinstance(a_val, dict):
                    continue
                cluster_norm["attributes"][f"0x{a_id:04X}"] = _normalise_attr_mapping(a_val)
            for c_key, c_val in (cl_val.get("commands") or {}).items():
                c_id = _to_int(c_key)
                if c_id is None or not isinstance(c_val, dict):
                    continue
                cluster_norm["commands"][f"0x{c_id:02X}"] = {
                    "name":    str(c_val.get("name") or ""),
                    "args":    list(c_val.get("args") or []),
                    "expose":  bool(c_val.get("expose", False)),
                }
            clusters_out[f"0x{cl_id:04X}"] = cluster_norm
        eps_out[str(ep_id)] = {
            "role":     str(ep_val.get("role") or "primary"),
            "label":    str(ep_val.get("label") or ""),
            "group":    str(ep_val.get("group") or ""),
            "clusters": clusters_out,
        }
    out["endpoints"] = eps_out

    # actions
    actions_out: List[Dict[str, Any]] = []
    for a in (p.get("actions") or []):
        if not isinstance(a, dict):
            continue
        ep = _to_int(a.get("ep"))
        cl = _to_int(a.get("cluster"))
        cmd = _to_int(a.get("command"))
        writes = a.get("writes") or []
        if ep is None or cl is None:
            continue
        if cmd is None and not writes:
            continue
        norm_writes: List[Dict[str, Any]] = []
        for w in writes:
            if not isinstance(w, dict):
                continue
            w_ep = _to_int(w.get("ep")) if "ep" in w else ep
            w_cl = _to_int(w.get("cluster")) if "cluster" in w else cl
            w_attr = _to_int(w.get("attr"))
            if w_ep is None or w_cl is None or w_attr is None:
                continue
            norm_writes.append({
                "ep":      w_ep,
                "cluster": f"0x{w_cl:04X}",
                "attr":    f"0x{w_attr:04X}",
                "value":   w.get("value"),
                "type":    w.get("type") or None,
            })
        actions_out.append({
            "id":      str(a.get("id") or f"action_{len(actions_out)}"),
            "label":   str(a.get("label") or a.get("id") or "Action"),
            "ep":      ep,
            "cluster": f"0x{cl:04X}",
            "command": f"0x{cmd:02X}" if cmd is not None else None,
            "args":    list(a.get("args") or []),
            "writes":  norm_writes,
            "atomic":  bool(a.get("atomic", len(norm_writes) > 1)),
        })
    out["actions"] = actions_out

    # reporting
    reporting_out: List[Dict[str, Any]] = []
    for r in (p.get("reporting") or []):
        if not isinstance(r, dict):
            continue
        ep = _to_int(r.get("ep"))
        cl = _to_int(r.get("cluster"))
        at = _to_int(r.get("attr"))
        if ep is None or cl is None or at is None:
            continue
        reporting_out.append({
            "ep":      ep,
            "cluster": f"0x{cl:04X}",
            "attr":    f"0x{at:04X}",
            "min":     int(r.get("min") or 30),
            "max":     int(r.get("max") or 600),
            "delta":   r.get("delta", 1),
        })
    out["reporting"] = reporting_out

    # ieee_overrides — legacy per-device mappings carried inside the profile
    # are unusual but supported for migration paths. Most live in the separate
    # ieee_overrides.json file.
    out["ieee_overrides"] = p.get("ieee_overrides") or {}

    meta = p.get("meta") or {}
    out["meta"] = {
        "author":     str(meta.get("author") or ""),
        "source":     str(meta.get("source") or "user"),
        "created_at": int(meta.get("created_at") or _now()),
        "updated_at": _now(),
    }
    return out


def _normalise_attr_mapping(a: Dict[str, Any]) -> Dict[str, Any]:
    out = {"name": str(a.get("name") or "")}
    if "scale" in a:        out["scale"]        = a["scale"]
    if "unit" in a:         out["unit"]         = str(a.get("unit") or "")
    if "device_class" in a: out["device_class"] = str(a.get("device_class") or "")
    if "invert" in a:       out["invert"]       = bool(a.get("invert"))
    if "value_map" in a:    out["value_map"]    = dict(a.get("value_map") or {})
    if "state_topic" in a:  out["state_topic"]  = str(a.get("state_topic") or "")
    if "capability" in a:   out["capability"]   = str(a.get("capability") or "")
    return out


# ---------------------------------------------------------------------------
# Profile store
# ---------------------------------------------------------------------------

class ProfileStore:
    """
    File-backed registry of device profiles.

    Thread-safe (one RLock guards all mutating operations).
    """

    def __init__(
            self,
            user_dir: str = USER_PROFILES_DIR,
            bundled_dir: str = BUNDLED_PROFILES_DIR,
            ieee_overrides_file: str = IEEE_OVERRIDES_FILE,
    ):
        self._user_dir = user_dir
        self._bundled_dir = bundled_dir
        self._ieee_file = ieee_overrides_file
        self._lock = threading.RLock()

        # In-memory caches
        self._profiles_user: Dict[str, Dict[str, Any]] = {}
        self._profiles_bundled: Dict[str, Dict[str, Any]] = {}
        self._ieee_pins: Dict[str, str] = {}            # ieee → profile_id
        self._ieee_mappings: Dict[str, Dict[str, Any]] = {}  # ieee → {cluster_mappings: {...}}

        os.makedirs(self._user_dir, exist_ok=True)
        os.makedirs(self._bundled_dir, exist_ok=True)

        self._load_all()
        self._maybe_migrate_legacy()

    # -----------------------------------------------------------------
    # Disk I/O
    # -----------------------------------------------------------------

    def _load_all(self):
        with self._lock:
            self._profiles_user    = self._load_dir(self._user_dir,    "user")
            self._profiles_bundled = self._load_dir(self._bundled_dir, "bundled")
            self._load_ieee_overrides()
        logger.info(
            f"ProfileStore loaded: {len(self._profiles_user)} user, "
            f"{len(self._profiles_bundled)} bundled, "
            f"{len(self._ieee_pins)} IEEE pins, "
            f"{len(self._ieee_mappings)} IEEE mappings"
        )

    def _load_dir(self, path: str, source: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        if not os.path.isdir(path):
            return out
        for name in os.listdir(path):
            if not name.endswith(".json"):
                continue
            full = os.path.join(path, name)
            try:
                with open(full, "r") as f:
                    raw = json.load(f)
                norm = normalise_profile(raw)
                norm["meta"]["source"] = source
                if not norm["id"]:
                    norm["id"] = _safe_id(os.path.splitext(name)[0])
                out[norm["id"]] = norm
            except Exception as e:
                logger.warning(f"Failed to load profile {full}: {e}")
        return out

    def _load_ieee_overrides(self):
        self._ieee_pins = {}
        self._ieee_mappings = {}
        if not os.path.exists(self._ieee_file):
            return
        try:
            with open(self._ieee_file, "r") as f:
                data = json.load(f)
            self._ieee_pins     = dict(data.get("pins") or {})
            self._ieee_mappings = dict(data.get("mappings") or {})
        except Exception as e:
            logger.error(f"Failed to load {self._ieee_file}: {e}")

    def _save_ieee_overrides(self):
        try:
            os.makedirs(os.path.dirname(self._ieee_file), exist_ok=True)
            tmp = self._ieee_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "pins":     self._ieee_pins,
                    "mappings": self._ieee_mappings,
                }, f, indent=2)
            os.replace(tmp, self._ieee_file)
        except Exception as e:
            logger.error(f"Failed to save {self._ieee_file}: {e}")

    def _save_profile(self, profile: Dict[str, Any]):
        pid = profile["id"]
        path = os.path.join(self._user_dir, f"{pid}.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(profile, f, indent=2, sort_keys=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Failed to save profile {pid}: {e}")

    # -----------------------------------------------------------------
    # Legacy migration
    # -----------------------------------------------------------------

    def _maybe_migrate_legacy(self):
        """
        Convert old data/device_overrides.json into new-style profiles +
        ieee_overrides.json the first time the new store loads. Safe to
        run repeatedly — only writes files that don't already exist.
        """
        if not os.path.exists(LEGACY_OVERRIDES_FILE):
            return
        try:
            with open(LEGACY_OVERRIDES_FILE, "r") as f:
                legacy = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read legacy overrides: {e}")
            return

        legacy_defs = legacy.get("definitions") or {}
        legacy_ieee = legacy.get("ieee_overrides") or {}

        migrated_profiles = 0
        with self._lock:
            for key, defn in legacy_defs.items():
                # key was "model|manufacturer"
                parts = (key or "").split("|", 1)
                model = parts[0] if parts else ""
                manuf = parts[1] if len(parts) > 1 else ""
                pid = _safe_id(model) or _safe_id(key)
                if not pid or pid in self._profiles_user:
                    continue
                p = normalise_profile({
                    "id":           pid,
                    "protocol":     "zigbee",
                    "match":        {"model": model, "manufacturer": manuf},
                    "device_type":  "generic",
                    "endpoints":    {"1": {"clusters": defn.get("clusters") or {}}},
                    "meta":         {"source": "user", "author": "migrated"},
                })
                self._profiles_user[pid] = p
                self._save_profile(p)
                migrated_profiles += 1

            for ieee, payload in legacy_ieee.items():
                if ieee in self._ieee_mappings:
                    continue
                cm = payload.get("cluster_mappings") if isinstance(payload, dict) else None
                if cm:
                    self._ieee_mappings[ieee] = {"cluster_mappings": cm}

            if migrated_profiles or legacy_ieee:
                self._save_ieee_overrides()

        if migrated_profiles:
            logger.info(f"Migrated {migrated_profiles} legacy device_overrides definitions")

        # Migrate matter_definitions/*.json one-shot
        try:
            if os.path.isdir(LEGACY_MATTER_DEFS_DIR):
                for name in os.listdir(LEGACY_MATTER_DEFS_DIR):
                    if not name.endswith(".json"):
                        continue
                    full = os.path.join(LEGACY_MATTER_DEFS_DIR, name)
                    try:
                        with open(full, "r") as f:
                            raw = json.load(f)
                    except Exception:
                        continue
                    pid = _safe_id(raw.get("product_id") or raw.get("model") or os.path.splitext(name)[0])
                    if not pid or pid in self._profiles_user:
                        continue
                    p = normalise_profile({
                        "id":           pid,
                        "protocol":     "matter",
                        "match":        {
                            "vendor_id":    raw.get("vendor_id"),
                            "product_id":   raw.get("product_id"),
                            "model":        raw.get("model"),
                            "manufacturer": raw.get("manufacturer"),
                        },
                        "device_type":  raw.get("device_type") or "generic",
                        "endpoints":    raw.get("endpoints") or {},
                        "capabilities": raw.get("capabilities") or [],
                        "meta":         {"source": "user", "author": "migrated"},
                    })
                    with self._lock:
                        self._profiles_user[pid] = p
                        self._save_profile(p)
        except Exception as e:
            logger.debug(f"Matter legacy migration skipped: {e}")

    # -----------------------------------------------------------------
    # Lookup
    # -----------------------------------------------------------------

    def list_profiles(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            if source in (None, "user"):
                out.extend(self._profiles_user.values())
            if source in (None, "bundled"):
                # Don't return bundled profile when a user profile of same id exists
                for pid, p in self._profiles_bundled.items():
                    if pid not in self._profiles_user:
                        out.append(p)
            return [dict(p) for p in out]

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        if not profile_id:
            return None
        with self._lock:
            return dict(self._profiles_user.get(profile_id)
                        or self._profiles_bundled.get(profile_id)
                        or {}) or None

    def get_profile_for_device(
            self,
            *,
            ieee: str = "",
            protocol: str = "zigbee",
            model: str = "",
            manufacturer: str = "",
            vendor_id: Optional[int] = None,
            product_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve which profile (if any) applies to a device. Lookup order:

        1. IEEE pin
        2. user profile (exact match)
        3. bundled profile (exact match)
        4. fuzzy match (model only)
        """
        with self._lock:
            # 1. Pin
            pid = self._ieee_pins.get(ieee)
            if pid:
                p = self._profiles_user.get(pid) or self._profiles_bundled.get(pid)
                if p:
                    return dict(p)

            for table in (self._profiles_user, self._profiles_bundled):
                # 2/3. Exact match
                for p in table.values():
                    if p["protocol"] != protocol:
                        continue
                    m = p["match"]
                    if protocol == "zigbee":
                        if (model and m["model"] == model
                                and (not m["manufacturer"] or m["manufacturer"] == manufacturer)):
                            return dict(p)
                    else:  # matter
                        if (vendor_id is not None and m["vendor_id"] == vendor_id
                                and product_id and m["product_id"] == product_id):
                            return dict(p)
                # 4. Model-only fuzzy fallback (Zigbee)
                if protocol == "zigbee" and model:
                    for p in table.values():
                        if p["protocol"] == "zigbee" and p["match"]["model"] == model:
                            return dict(p)
        return None

    # -----------------------------------------------------------------
    # CRUD — profiles
    # -----------------------------------------------------------------

    def upsert_profile(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        norm = normalise_profile(raw)
        if not norm["id"]:
            # Derive id from match data
            base = (norm["match"]["model"]
                    or (str(norm["match"].get("product_id") or "") if norm["protocol"] == "matter" else "")
                    or "profile")
            norm["id"] = _safe_id(base)
        # User profiles can never be sourced as "bundled"
        if norm["meta"]["source"] == "bundled":
            norm["meta"]["source"] = "user"
        with self._lock:
            self._profiles_user[norm["id"]] = norm
            self._save_profile(norm)
        logger.info(f"Profile saved: {norm['id']} ({norm['protocol']})")
        return norm

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            if profile_id not in self._profiles_user:
                return False
            del self._profiles_user[profile_id]
            path = os.path.join(self._user_dir, f"{profile_id}.json")
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.warning(f"Could not delete {path}: {e}")
            # Drop any pins that referenced it
            stale = [i for i, pid in self._ieee_pins.items() if pid == profile_id]
            for i in stale:
                del self._ieee_pins[i]
            if stale:
                self._save_ieee_overrides()
        return True

    # -----------------------------------------------------------------
    # IEEE pins + per-device mappings
    # -----------------------------------------------------------------

    def pin_ieee(self, ieee: str, profile_id: str) -> bool:
        with self._lock:
            if profile_id not in self._profiles_user and profile_id not in self._profiles_bundled:
                return False
            self._ieee_pins[ieee] = profile_id
            self._save_ieee_overrides()
        return True

    def unpin_ieee(self, ieee: str) -> bool:
        with self._lock:
            if ieee in self._ieee_pins:
                del self._ieee_pins[ieee]
                self._save_ieee_overrides()
                return True
        return False

    def get_ieee_pin(self, ieee: str) -> Optional[str]:
        with self._lock:
            return self._ieee_pins.get(ieee)

    def set_ieee_mapping(
            self, ieee: str, raw_key: str, friendly_name: str,
            scale: float = 1, unit: str = "", device_class: str = "",
            invert: bool = False,
    ) -> bool:
        with self._lock:
            if ieee not in self._ieee_mappings:
                self._ieee_mappings[ieee] = {"cluster_mappings": {}}
            mapping: Dict[str, Any] = {"name": friendly_name}
            if scale != 1:        mapping["scale"]        = scale
            if unit:              mapping["unit"]         = unit
            if device_class:      mapping["device_class"] = device_class
            if invert:            mapping["invert"]       = True
            self._ieee_mappings[ieee]["cluster_mappings"][raw_key] = mapping
            self._save_ieee_overrides()
        return True

    def remove_ieee_mapping(self, ieee: str, raw_key: str) -> bool:
        with self._lock:
            if ieee in self._ieee_mappings:
                cm = self._ieee_mappings[ieee].get("cluster_mappings", {})
                if raw_key in cm:
                    del cm[raw_key]
                    if not cm:
                        del self._ieee_mappings[ieee]
                    self._save_ieee_overrides()
                    return True
        return False

    def get_ieee_mappings(self, ieee: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._ieee_mappings.get(ieee, {}).get("cluster_mappings", {}))

    def list_ieee_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pins":     dict(self._ieee_pins),
                "mappings": dict(self._ieee_mappings),
            }

    # -----------------------------------------------------------------
    # Attribute lookup (used by handlers / discovery / state formatter)
    # -----------------------------------------------------------------

    def get_attribute_mapping(
            self, *, ieee: str, model: str, manufacturer: str,
            cluster_id: int, attr_id: int,
            protocol: str = "zigbee",
            vendor_id: Optional[int] = None, product_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the friendly mapping dict for an attribute, checking the
        IEEE-specific mappings first, then any matching profile.
        """
        raw_key = f"cluster_{cluster_id:04x}_attr_{attr_id:04x}"
        cluster_hex = f"0x{cluster_id:04X}"
        attr_hex    = f"0x{attr_id:04X}"

        with self._lock:
            # 1. IEEE-level
            ieee_cm = self._ieee_mappings.get(ieee, {}).get("cluster_mappings", {})
            m = ieee_cm.get(raw_key)
            if m:
                return dict(m) if isinstance(m, dict) else {"name": str(m)}

        # 2. Profile
        p = self.get_profile_for_device(
            ieee=ieee, protocol=protocol, model=model, manufacturer=manufacturer,
            vendor_id=vendor_id, product_id=product_id,
        )
        if not p:
            return None
        for ep in p["endpoints"].values():
            cl = ep.get("clusters", {}).get(cluster_hex)
            if not cl:
                continue
            attr = cl.get("attributes", {}).get(attr_hex)
            if attr:
                return dict(attr)
        return None


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_store: Optional[ProfileStore] = None


def get_profile_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore()
    return _store


# ---------------------------------------------------------------------------
# Backwards-compatible facade for old call sites
# ---------------------------------------------------------------------------
# The old DeviceOverrideManager API is preserved so existing imports of
# ``from modules.device_overrides import get_override_manager`` keep working
# while the codebase transitions to the new module.

class _LegacyOverrideManagerShim:
    """Adapter that exposes the old API on top of ProfileStore."""

    def __init__(self, store: ProfileStore):
        self._s = store

    def get_attribute_mapping(self, ieee, model, manufacturer, cluster_id, attr_id):
        return self._s.get_attribute_mapping(
            ieee=ieee, model=model, manufacturer=manufacturer,
            cluster_id=cluster_id, attr_id=attr_id,
        )

    def get_command_mapping(self, ieee, model, manufacturer, cluster_id, command_id):
        p = self._s.get_profile_for_device(
            ieee=ieee, protocol="zigbee", model=model, manufacturer=manufacturer,
        )
        if not p:
            return None
        cluster_hex = f"0x{cluster_id:04X}"
        cmd_hex     = f"0x{command_id:02X}"
        for ep in p["endpoints"].values():
            cl = ep.get("clusters", {}).get(cluster_hex)
            if cl and cmd_hex in cl.get("commands", {}):
                return dict(cl["commands"][cmd_hex])
        return None

    def get_definition(self, model, manufacturer):
        return self._s.get_profile_for_device(
            protocol="zigbee", model=model, manufacturer=manufacturer,
        )

    def get_ieee_override(self, ieee):
        if ieee in self._s.list_ieee_state()["mappings"]:
            return self._s.list_ieee_state()["mappings"][ieee]
        return None

    def add_definition(self, model, manufacturer, definition):
        return self._s.upsert_profile({
            "protocol":  "zigbee",
            "match":     {"model": model, "manufacturer": manufacturer},
            "endpoints": {"1": {"clusters": definition.get("clusters") or {}}},
        })

    def remove_definition(self, model, manufacturer):
        for p in self._s.list_profiles("user"):
            if (p["protocol"] == "zigbee"
                    and p["match"]["model"] == model
                    and (not manufacturer or p["match"]["manufacturer"] == manufacturer)):
                return self._s.delete_profile(p["id"])
        return False

    def list_definitions(self):
        return {
            f"{p['match']['model']}|{p['match']['manufacturer']}": p
            for p in self._s.list_profiles()
            if p["protocol"] == "zigbee"
        }

    def set_ieee_mapping(self, ieee, raw_key, friendly_name, scale=1, unit="", device_class=""):
        return self._s.set_ieee_mapping(
            ieee=ieee, raw_key=raw_key, friendly_name=friendly_name,
            scale=scale, unit=unit, device_class=device_class,
        )

    def remove_ieee_mapping(self, ieee, raw_key):
        return self._s.remove_ieee_mapping(ieee, raw_key)

    def get_ieee_mappings(self, ieee):
        return self._s.get_ieee_mappings(ieee)

    def list_ieee_overrides(self):
        return self._s.list_ieee_state()["mappings"]


def get_legacy_override_manager() -> _LegacyOverrideManagerShim:
    """For modules that still import the old API."""
    return _LegacyOverrideManagerShim(get_profile_store())