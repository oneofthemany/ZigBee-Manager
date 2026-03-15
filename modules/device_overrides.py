# modules/device_overrides.py
"""
Device Override Manager
Provides model-level and per-device attribute mappings for unsupported devices.
"""
import json
import os
import logging
from typing import Any, Dict, Optional, List

logger = logging.getLogger("modules.device_overrides")

OVERRIDES_FILE = "./data/device_overrides.json"

# Known manufacturer cluster metadata
# Used by GenericClusterHandler to make smarter decisions
MANUFACTURER_PROFILES = {
    # Aqara / Xiaomi / Opple
    0xFCC0: {
        "name": "Aqara Opple",
        "manufacturer_code": 0x115F,
        "requires_mfr_code": True,
    },
    0xFCB7: {
        "name": "Opple Extended",
        "manufacturer_code": 0x115F,
        "requires_mfr_code": True,
    },
    # Tuya
    0xEF00: {
        "name": "Tuya Private",
        "manufacturer_code": 0x1002,
        "requires_mfr_code": False,  # Tuya uses DP framing, not mfr code
    },
    0xE001: {
        "name": "Tuya Private 2",
        "manufacturer_code": 0x1002,
        "requires_mfr_code": False,
    },
    # Philips / Signify
    0xFC00: {
        "name": "Philips Private",
        "manufacturer_code": 0x100B,
        "requires_mfr_code": True,
    },
    0xFC03: {
        "name": "Philips Private 2",
        "manufacturer_code": 0x100B,
        "requires_mfr_code": True,
    },
    # IKEA
    0xFC7C: {
        "name": "IKEA Private",
        "manufacturer_code": 0x117C,
        "requires_mfr_code": True,
    },
    # Schneider / Legrand
    0xFC01: {
        "name": "Schneider/Legrand Private",
        "manufacturer_code": 0x105E,
        "requires_mfr_code": True,
    },
    # Sonoff
    0xFC11: {
        "name": "Sonoff Private",
        "manufacturer_code": 0x1286,
        "requires_mfr_code": True,
    },
}


def get_manufacturer_profile(cluster_id: int) -> Optional[Dict]:
    """Get manufacturer profile for a cluster if one exists."""
    return MANUFACTURER_PROFILES.get(cluster_id)


class DeviceOverrideManager:
    """
    Manages device definitions for unsupported/generic devices.

    Two levels of matching:
      - model_definitions: keyed by "model|manufacturer", applies to all matching devices
      - ieee_overrides: keyed by IEEE, per-device attribute mappings
    """

    def __init__(self):
        self._definitions: Dict[str, Dict] = {}   # "model|manufacturer" -> definition
        self._ieee_overrides: Dict[str, Dict] = {} # ieee -> {cluster_mappings, ...}
        self.load()

    def load(self):
        """Load overrides from JSON file."""
        if not os.path.exists(OVERRIDES_FILE):
            self._definitions = {}
            self._ieee_overrides = {}
            logger.info("No device_overrides.json found — starting empty")
            return

        try:
            with open(OVERRIDES_FILE, 'r') as f:
                data = json.load(f)
            self._definitions = data.get("definitions", {})
            self._ieee_overrides = data.get("ieee_overrides", {})
            logger.info(
                f"Loaded {len(self._definitions)} model definitions, "
                f"{len(self._ieee_overrides)} IEEE overrides"
            )
        except Exception as e:
            logger.error(f"Failed to load {OVERRIDES_FILE}: {e}")
            self._definitions = {}
            self._ieee_overrides = {}

    def save(self):
        """Persist overrides to JSON."""
        try:
            os.makedirs(os.path.dirname(OVERRIDES_FILE), exist_ok=True)
            with open(OVERRIDES_FILE, 'w') as f:
                json.dump({
                    "definitions": self._definitions,
                    "ieee_overrides": self._ieee_overrides
                }, f, indent=2)
            logger.info("Device overrides saved")
        except Exception as e:
            logger.error(f"Failed to save {OVERRIDES_FILE}: {e}")

    # ================================================================
    # LOOKUP
    # ================================================================

    def _make_key(self, model: str, manufacturer: str) -> str:
        """Build lookup key from model and manufacturer."""
        return f"{model.strip()}|{manufacturer.strip()}"

    def get_definition(self, model: str, manufacturer: str) -> Optional[Dict]:
        """Get model-level definition if one exists."""
        key = self._make_key(model, manufacturer)
        defn = self._definitions.get(key)
        if defn:
            return defn

        # Fallback: try model-only match (manufacturer wildcard)
        for k, v in self._definitions.items():
            stored_model = k.split("|")[0]
            if stored_model == model.strip():
                return v

        return None

    def get_ieee_override(self, ieee: str) -> Optional[Dict]:
        """Get per-device override if one exists."""
        return self._ieee_overrides.get(ieee)

    def get_attribute_mapping(self, ieee: str, model: str, manufacturer: str,
                              cluster_id: int, attr_id: int) -> Optional[Dict]:
        """
        Look up a friendly name + transform for a specific attribute.

        Checks IEEE-level first, then model-level.

        Returns dict with keys: name, scale, unit, device_class (all optional except name)
        or None if no mapping exists.
        """
        cluster_hex = f"0x{cluster_id:04X}"
        attr_hex = f"0x{attr_id:04X}"
        raw_key = f"cluster_{cluster_id:04x}_attr_{attr_id:04x}"

        # 1. IEEE-level (per-device)
        ieee_ovr = self._ieee_overrides.get(ieee)
        if ieee_ovr:
            mapping = ieee_ovr.get("cluster_mappings", {}).get(raw_key)
            if mapping:
                if isinstance(mapping, str):
                    return {"name": mapping}
                return mapping

        # 2. Model-level
        defn = self.get_definition(model, manufacturer)
        if defn:
            cluster_def = defn.get("clusters", {}).get(cluster_hex)
            if cluster_def:
                attr_def = cluster_def.get("attributes", {}).get(attr_hex)
                if attr_def:
                    return attr_def

        return None

    def get_command_mapping(self, ieee: str, model: str, manufacturer: str,
                            cluster_id: int, command_id: int) -> Optional[Dict]:
        """Look up friendly name for a cluster command."""
        cluster_hex = f"0x{cluster_id:04X}"
        cmd_hex = f"0x{command_id:02X}"

        defn = self.get_definition(model, manufacturer)
        if defn:
            cluster_def = defn.get("clusters", {}).get(cluster_hex)
            if cluster_def:
                return cluster_def.get("commands", {}).get(cmd_hex)

        return None

    # ================================================================
    # CRUD — MODEL DEFINITIONS
    # ================================================================

    def add_definition(self, model: str, manufacturer: str, definition: Dict) -> bool:
        """Add or update a model-level definition."""
        key = self._make_key(model, manufacturer)
        self._definitions[key] = definition
        self.save()
        logger.info(f"Added/updated definition for {key}")
        return True

    def remove_definition(self, model: str, manufacturer: str) -> bool:
        """Remove a model-level definition."""
        key = self._make_key(model, manufacturer)
        if key in self._definitions:
            del self._definitions[key]
            self.save()
            logger.info(f"Removed definition for {key}")
            return True
        return False

    def list_definitions(self) -> Dict:
        """Return all model definitions."""
        return self._definitions.copy()

    # ================================================================
    # CRUD — IEEE OVERRIDES
    # ================================================================

    def set_ieee_mapping(self, ieee: str, raw_key: str, friendly_name: str,
                         scale: float = 1, unit: str = "", device_class: str = "") -> bool:
        """Set a single attribute mapping for a specific device."""
        if ieee not in self._ieee_overrides:
            self._ieee_overrides[ieee] = {"cluster_mappings": {}}

        mapping = {"name": friendly_name}
        if scale != 1:
            mapping["scale"] = scale
        if unit:
            mapping["unit"] = unit
        if device_class:
            mapping["device_class"] = device_class

        self._ieee_overrides[ieee]["cluster_mappings"][raw_key] = mapping
        self.save()
        logger.info(f"[{ieee}] Mapped {raw_key} -> {friendly_name}")
        return True

    def remove_ieee_mapping(self, ieee: str, raw_key: str) -> bool:
        """Remove a single attribute mapping for a device."""
        if ieee in self._ieee_overrides:
            mappings = self._ieee_overrides[ieee].get("cluster_mappings", {})
            if raw_key in mappings:
                del mappings[raw_key]
                if not mappings:
                    del self._ieee_overrides[ieee]
                self.save()
                return True
        return False

    def get_ieee_mappings(self, ieee: str) -> Dict:
        """Get all mappings for a device."""
        return self._ieee_overrides.get(ieee, {}).get("cluster_mappings", {})

    def list_ieee_overrides(self) -> Dict:
        """Return all IEEE overrides."""
        return self._ieee_overrides.copy()


# Singleton
_manager: Optional[DeviceOverrideManager] = None

def get_override_manager() -> DeviceOverrideManager:
    global _manager
    if _manager is None:
        _manager = DeviceOverrideManager()
    return _manager