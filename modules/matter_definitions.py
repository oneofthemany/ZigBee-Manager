"""
Matter Device Definitions — JSON-driven device mapping framework.
================================================================

Provides a definition system for Matter devices, similar to Zigbee quirks:
  - JSON definition files map vendor_id/product_id → endpoint roles & state mappings
  - DefinitionParser uses definitions to build meaningful state from raw attributes
  - Definitions can be created/edited via API and saved to config/matter_definitions/
  - Auto-detects matching definition by vendor_id + part_number (model)

Definition file structure:
{
  "vendor_id": 4476,
  "product_id": "E2490",
  "model": "BILRESA scroll wheel",
  "manufacturer": "IKEA of Sweden",
  "device_type": "Button",
  "endpoints": {
    "1": {"role": "button", "label": "Left Button", "group": "left", ...},
    ...
  },
  "state_mapping": {
    "left_button": {"ep": 1, "cluster": 59, "attr": 1, "transform": "position"},
    ...
  },
  "capabilities": ["button", "rotary", "battery"]
}
"""

import json
import logging
import os
import time
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("matter.definitions")

DEFINITIONS_DIR = os.environ.get(
    "ZMM_MATTER_DEFS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "config", "matter_definitions"),
)


# =============================================================================
# TAG SEMANTICS (Matter Descriptor cluster, TagList attribute)
# =============================================================================

# Semantic Tag namespace 0x0007 (Common/Position)
SEMANTIC_TAGS = {
    # Namespace 8 = "Position"
    (8, 0): "left",
    (8, 1): "right",
    (8, 2): "top",
    (8, 3): "bottom",
    (8, 4): "center",
    (8, 5): "row",
    (8, 6): "column",
    # Namespace 67 = "Button/Switch"  (common in IKEA devices)
    (67, 1): "position_1",
    (67, 2): "position_2",
    (67, 3): "position_3",
    (67, 4): "position_4",
    (67, 5): "long_press",
    (67, 6): "short_press",
}

# Switch cluster (59) feature map bits
SWITCH_FEATURES = {
    0: "latching_switch",
    1: "momentary_switch",
    2: "momentary_switch_release",
    3: "momentary_switch_long_press",
    4: "momentary_switch_multi_press",
}


# =============================================================================
# DEFINITION LOADER
# =============================================================================

class DefinitionStore:
    """Loads and manages Matter device definitions from JSON files."""

    def __init__(self, definitions_dir: str = None):
        self._dir = definitions_dir or DEFINITIONS_DIR
        self._definitions: Dict[str, dict] = {}  # key = "vendor_id:product_id"
        self._by_file: Dict[str, dict] = {}  # filename → definition
        self._load_all()

    def _load_all(self):
        """Load all definition files from the definitions directory."""
        os.makedirs(self._dir, exist_ok=True)
        self._definitions.clear()
        self._by_file.clear()

        for fname in os.listdir(self._dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path, "r") as f:
                    defn = json.load(f)
                key = self._make_key(defn)
                if key:
                    self._definitions[key] = defn
                    self._by_file[fname] = defn
                    logger.info(f"Loaded matter definition: {fname} → {key}")
            except Exception as e:
                logger.warning(f"Failed to load matter definition {fname}: {e}")

        logger.info(f"Matter definitions loaded: {len(self._definitions)} definitions")

    def _make_key(self, defn: dict) -> Optional[str]:
        """Create lookup key from definition."""
        vid = defn.get("vendor_id")
        pid = defn.get("product_id", "")
        if vid is None:
            return None
        return f"{vid}:{pid}"

    def reload(self):
        """Reload all definitions from disk."""
        self._load_all()

    def find(self, vendor_id: int, product_id: str) -> Optional[dict]:
        """Find a definition matching vendor_id and product_id (model/part number)."""
        # Exact match
        key = f"{vendor_id}:{product_id}"
        if key in self._definitions:
            return self._definitions[key]

        # Vendor-only fallback (empty product_id in definition)
        key_vendor = f"{vendor_id}:"
        if key_vendor in self._definitions:
            return self._definitions[key_vendor]

        return None

    def find_for_node(self, attributes: dict) -> Optional[dict]:
        """Find a definition matching a node's attributes."""
        # Extract vendor_id and part_number from BasicInformation cluster
        vendor_id = None
        part_number = ""
        for key, value in attributes.items():
            parts = key.split("/")
            if len(parts) != 3:
                continue
            ep, cluster, attr = parts
            if cluster == "40":  # BasicInformation
                if attr == "2":    # VendorID
                    vendor_id = value
                elif attr == "12":  # PartNumber
                    part_number = str(value) if value else ""
                elif attr == "3" and not part_number:  # ProductName fallback
                    part_number = str(value) if value else ""

        if vendor_id is None:
            return None

        return self.find(vendor_id, part_number)

    def list_definitions(self) -> List[dict]:
        """List all definitions with metadata."""
        result = []
        for fname, defn in self._by_file.items():
            result.append({
                "filename": fname,
                "vendor_id": defn.get("vendor_id"),
                "product_id": defn.get("product_id", ""),
                "model": defn.get("model", ""),
                "manufacturer": defn.get("manufacturer", ""),
                "device_type": defn.get("device_type", ""),
                "endpoint_count": len(defn.get("endpoints", {})),
                "state_count": len(defn.get("state_mapping", {})),
            })
        return result

    def save(self, defn: dict, filename: str = None) -> str:
        """Save a definition to disk. Returns filename."""
        if not filename:
            vid = defn.get("vendor_id", 0)
            pid = defn.get("product_id", "unknown").lower().replace(" ", "_")
            model = defn.get("model", "device").lower().replace(" ", "_")
            filename = f"{defn.get('manufacturer', 'unknown').lower().replace(' ', '_')}_{pid}_{model}.json"

        # Sanitise filename
        filename = "".join(c for c in filename if c.isalnum() or c in "._-").strip()
        if not filename.endswith(".json"):
            filename += ".json"

        path = os.path.join(self._dir, filename)
        os.makedirs(self._dir, exist_ok=True)

        with open(path, "w") as f:
            json.dump(defn, f, indent=2, default=str)

        # Reload
        key = self._make_key(defn)
        if key:
            self._definitions[key] = defn
            self._by_file[filename] = defn

        logger.info(f"Saved matter definition: {filename}")
        return filename

    def delete(self, filename: str) -> bool:
        """Delete a definition file."""
        path = os.path.join(self._dir, filename)
        if os.path.exists(path):
            defn = self._by_file.pop(filename, None)
            if defn:
                key = self._make_key(defn)
                self._definitions.pop(key, None)
            os.remove(path)
            logger.info(f"Deleted matter definition: {filename}")
            return True
        return False


# =============================================================================
# ENDPOINT SCANNER — auto-generates definition drafts from raw attributes
# =============================================================================

def scan_endpoints(attributes: dict) -> dict:
    """
    Scan a Matter node's raw attributes and build a structured endpoint map.
    Used by the frontend to show the endpoint explorer and assist definition creation.
    """
    endpoints = {}

    for key, value in attributes.items():
        parts = key.split("/")
        if len(parts) != 3:
            continue
        try:
            ep = int(parts[0])
            cluster = int(parts[1])
            attr = int(parts[2])
        except ValueError:
            continue

        if ep not in endpoints:
            endpoints[ep] = {
                "endpoint_id": ep,
                "clusters": {},
                "device_types": [],
                "tags": [],
                "role": "unknown",
                "label": f"Endpoint {ep}",
            }

        ep_data = endpoints[ep]
        if cluster not in ep_data["clusters"]:
            from matter_parsers import BaseMatterParser
            ep_data["clusters"][cluster] = {
                "cluster_id": cluster,
                "cluster_name": BaseMatterParser.CLUSTER_NAMES.get(cluster, f"Cluster {cluster}"),
                "attributes": {},
            }

        ep_data["clusters"][cluster]["attributes"][attr] = value

    # Post-process: extract device types, tags, roles
    for ep_id, ep_data in endpoints.items():
        # Device types from Descriptor cluster (29)
        descriptor = ep_data["clusters"].get(29, {}).get("attributes", {})
        type_list = descriptor.get(0, [])
        if isinstance(type_list, list):
            for entry in type_list:
                if isinstance(entry, dict):
                    dt = entry.get("0", entry.get(0, 0))
                    from matter_parsers import MATTER_DEVICE_TYPES
                    ep_data["device_types"].append({
                        "type_id": dt,
                        "type_name": MATTER_DEVICE_TYPES.get(dt, f"Type {dt}"),
                    })

        # Tags from Descriptor TagList (attr 4)
        tag_list = descriptor.get(4, [])
        if isinstance(tag_list, list):
            for tag_entry in tag_list:
                if isinstance(tag_entry, dict):
                    ns = tag_entry.get("1", tag_entry.get(1))
                    tag_val = tag_entry.get("2", tag_entry.get(2))
                    tag_label = tag_entry.get("3", tag_entry.get(3, ""))
                    semantic = SEMANTIC_TAGS.get((ns, tag_val), f"ns{ns}_tag{tag_val}")
                    ep_data["tags"].append({
                        "namespace": ns,
                        "tag": tag_val,
                        "label": str(tag_label) if tag_label else "",
                        "semantic": semantic,
                    })

        # Auto-detect role from clusters and features
        switch_cluster = ep_data["clusters"].get(59)
        if switch_cluster:
            switch_attrs = switch_cluster.get("attributes", {})
            feature_map = switch_attrs.get(65532, 0)
            positions = switch_attrs.get(0, 2)

            # Decode feature bits
            features = []
            for bit, name in SWITCH_FEATURES.items():
                if feature_map & (1 << bit):
                    features.append(name)

            # Determine role from features and tags
            tag_labels = [t.get("label", "").lower() for t in ep_data["tags"]]
            if "rotary" in tag_labels:
                ep_data["role"] = "rotary"
                ep_data["label"] = f"Rotary (EP{ep_id})"
            elif "button" in tag_labels or any(f in features for f in
                                               ["momentary_switch", "momentary_switch_long_press"]):
                ep_data["role"] = "button"
                ep_data["label"] = f"Button (EP{ep_id})"
            elif "latching_switch" in features:
                ep_data["role"] = "toggle"
                ep_data["label"] = f"Toggle (EP{ep_id})"
            else:
                ep_data["role"] = "switch"
                ep_data["label"] = f"Switch (EP{ep_id})"

            ep_data["switch_info"] = {
                "positions": positions,
                "current_position": switch_attrs.get(1, 0),
                "multi_press_max": switch_attrs.get(2, 0),
                "feature_map": feature_map,
                "features": features,
            }

            # Enrich label with position tag
            for tag in ep_data["tags"]:
                if tag["namespace"] in (8, 67) and tag.get("label"):
                    ep_data["label"] = f"{ep_data['role'].capitalize()} '{tag['label']}' (EP{ep_id})"
                    break

        # Infrastructure endpoints
        if ep_id == 0:
            ep_data["role"] = "root"
            ep_data["label"] = "Root Node (EP0)"

    # Sort by endpoint ID
    return {
        "endpoints": [endpoints[ep] for ep in sorted(endpoints.keys())],
        "endpoint_count": len(endpoints),
    }


def generate_definition_draft(attributes: dict) -> dict:
    """
    Auto-generate a definition draft from raw attributes.
    The user can then refine it via the UI.
    """
    from matter_parsers import BaseMatterParser, BasicInfoAttrs

    base = BaseMatterParser()
    vendor_name = base.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_NAME, "Unknown")
    vendor_id = base.find_attr(attributes, 40, BasicInfoAttrs.VENDOR_ID, 0)
    product_name = base.find_attr(attributes, 40, BasicInfoAttrs.PRODUCT_NAME, "")
    part_number = base.find_attr(attributes, 40, BasicInfoAttrs.PART_NUMBER, "")
    device_type = base.get_device_type(attributes)

    scan = scan_endpoints(attributes)

    # Build endpoint map
    endpoint_map = {}
    state_mapping = {}
    capabilities = set(["matter"])

    for ep_info in scan["endpoints"]:
        ep_id = ep_info["endpoint_id"]
        if ep_id == 0:
            continue  # Skip root

        role = ep_info.get("role", "unknown")
        label = ep_info.get("label", f"EP{ep_id}")

        # Find group from tags
        group = ""
        for tag in ep_info.get("tags", []):
            if tag.get("label"):
                group = tag["label"]
                break

        endpoint_map[str(ep_id)] = {
            "role": role,
            "label": label,
            "group": group,
        }

        if ep_info.get("switch_info"):
            endpoint_map[str(ep_id)]["switch_info"] = ep_info["switch_info"]

        # Auto-generate state mappings
        switch_info = ep_info.get("switch_info")
        if switch_info:
            safe_label = label.lower().replace(" ", "_").replace("'", "")
            safe_label = "".join(c for c in safe_label if c.isalnum() or c == "_")

            if role == "rotary":
                state_key = f"{group}_rotary" if group else f"ep{ep_id}_rotary"
                state_mapping[state_key] = {
                    "ep": ep_id, "cluster": 59, "attr": 1,
                    "type": "position", "description": f"{label} position",
                }
                capabilities.add("rotary")
            elif role in ("button", "toggle"):
                state_key = f"{group}_button" if group else f"ep{ep_id}_button"
                state_mapping[state_key] = {
                    "ep": ep_id, "cluster": 59, "attr": 1,
                    "type": "position", "description": f"{label} position",
                }
                capabilities.add("button")

    # Battery
    bat = base.find_attr(attributes, 47, 12)
    if bat is not None:
        state_mapping["battery"] = {
            "ep": 0, "cluster": 47, "attr": 12,
            "type": "battery", "description": "Battery percentage",
        }
        capabilities.add("battery")

    return {
        "vendor_id": vendor_id,
        "product_id": part_number or product_name,
        "model": product_name,
        "manufacturer": vendor_name,
        "device_type": device_type,
        "endpoints": endpoint_map,
        "state_mapping": state_mapping,
        "capabilities": sorted(list(capabilities)),
        "_draft": True,
        "_generated_at": time.time(),
    }


# =============================================================================
# DEFINITION-BASED PARSER
# =============================================================================

class DefinitionParser:
    """
    Parser driven by a JSON device definition.
    Plugs into the matter_parsers framework — same interface as BaseMatterParser.
    """

    def __init__(self, definition: dict):
        self._def = definition
        self.device_type = definition.get("device_type", "Matter")
        self._previous_positions = {}

    def find_attr(self, attributes: dict, cluster: int, attr: int,
                  default=None, endpoint: int = None):
        eps = [endpoint] if endpoint is not None else [0, 1, 2]
        for ep in eps:
            key = f"{ep}/{cluster}/{attr}"
            if key in attributes:
                return attributes[key]
        return default

    def get_manufacturer(self, attributes: dict) -> str:
        return self._def.get("manufacturer", "Unknown")

    def get_model(self, attributes: dict) -> str:
        return self._def.get("product_id", self._def.get("model", "Unknown"))

    def get_friendly_name(self, attributes: dict) -> str:
        label = self.find_attr(attributes, 40, 5, "")
        if label:
            return str(label)
        return self._def.get("model", "Matter Device")

    def parse_basic_info(self, attributes: dict) -> dict:
        return {
            "vendor_name": self.find_attr(attributes, 40, 1, self._def.get("manufacturer", "")),
            "vendor_id": self.find_attr(attributes, 40, 2, self._def.get("vendor_id", 0)),
            "product_name": self.find_attr(attributes, 40, 3, self._def.get("model", "")),
            "product_id": self.find_attr(attributes, 40, 4, 0),
            "node_label": self.find_attr(attributes, 40, 5, ""),
            "part_number": self.find_attr(attributes, 40, 12, self._def.get("product_id", "")),
            "hardware_version": self.find_attr(attributes, 40, 8, ""),
            "software_version": self.find_attr(attributes, 40, 10, ""),
            "serial_number": self.find_attr(attributes, 40, 15, ""),
            "location": self.find_attr(attributes, 40, 6, ""),
            "definition": self._def.get("product_id", ""),
        }

    def build_state(self, attributes: dict, node_id: int, available: bool) -> dict:
        state = {
            "protocol": "matter",
            "available": available,
            "node_id": node_id,
            "definition": self._def.get("product_id", ""),
        }

        for state_key, mapping in self._def.get("state_mapping", {}).items():
            ep = mapping.get("ep", 0)
            cluster = mapping.get("cluster", 0)
            attr = mapping.get("attr", 0)
            transform = mapping.get("type", "raw")
            key = f"{ep}/{cluster}/{attr}"

            value = attributes.get(key)
            if value is None:
                continue

            if transform == "battery":
                state[state_key] = value // 2 if isinstance(value, int) else value
            elif transform == "position":
                state[state_key] = value
                # Detect rotary changes
                prev = self._previous_positions.get(f"{ep}_{state_key}")
                if prev is not None and value != prev:
                    direction = "cw" if value > prev else "ccw"
                    steps = abs(value - prev)
                    state["last_action"] = f"{state_key}_{direction}_{steps}"
                    state["last_action_source"] = state_key
                    state["last_action_time"] = time.time()
                self._previous_positions[f"{ep}_{state_key}"] = value
            elif transform == "boolean":
                state[state_key] = bool(value)
            elif transform == "on_off":
                state[state_key] = "ON" if value else "OFF"
            elif transform == "temperature":
                state[state_key] = round(value / 100.0, 1) if isinstance(value, (int, float)) else value
            elif transform == "percentage":
                state[state_key] = round(value / 2) if isinstance(value, int) else value
            else:
                state[state_key] = value

        return state

    def get_commands(self, attributes: dict) -> List[dict]:
        """Get commands — definition can override."""
        commands = self._def.get("commands", [])
        if commands:
            return commands

        # Fallback: identify on each endpoint with Identify cluster
        result = []
        for ep_str, ep_info in self._def.get("endpoints", {}).items():
            ep = int(ep_str)
            if ep == 0:
                continue
            result.append({
                "command": "identify",
                "label": f"Identify {ep_info.get('label', f'EP{ep}')}",
                "endpoint_id": ep,
                "cluster_id": 3,
            })
        return result[:3]  # Limit to 3 for UI

    def get_capabilities(self, attributes: dict) -> List[str]:
        caps = self._def.get("capabilities", ["matter"])
        if "matter" not in caps:
            caps = ["matter"] + caps
        return caps

    def get_device_type(self, attributes: dict) -> str:
        return self._def.get("device_type", "Matter")

    def get_all_endpoints(self, attributes: dict) -> List[int]:
        eps = set()
        for key in attributes:
            parts = key.split("/")
            if len(parts) == 3:
                try:
                    eps.add(int(parts[0]))
                except ValueError:
                    pass
        return sorted(eps)

    def get_clusters_for_endpoint(self, attributes: dict, ep: int) -> List[int]:
        clusters = set()
        prefix = f"{ep}/"
        for key in attributes:
            if key.startswith(prefix):
                parts = key.split("/")
                if len(parts) == 3:
                    try:
                        clusters.add(int(parts[1]))
                    except ValueError:
                        pass
        return sorted(clusters)

    def parse_event(self, event_name: str, endpoint_id: int,
                    cluster_id: int, event_data: dict) -> str:
        """Map events using endpoint labels from definition."""
        ep_info = self._def.get("endpoints", {}).get(str(endpoint_id), {})
        role = ep_info.get("role", "button")
        group = ep_info.get("group", "")
        label = ep_info.get("label", f"ep{endpoint_id}")

        # Use group name as prefix if available
        prefix = group if group else f"ep{endpoint_id}"

        # Standard switch event mapping
        action = event_name.lower().replace(" ", "_")
        event_map = {
            "initialpress": "press", "initial_press": "press",
            "longpress": "hold", "long_press": "hold",
            "shortrelease": "single", "short_release": "single",
            "longrelease": "release", "long_release": "release",
            "multipressongoing": "multi_press",
            "multipresscomplete": "multi", "multi_press_complete": "multi",
        }
        action = event_map.get(action, action)

        if "multi" in action:
            count = event_data.get("totalNumberOfPressesCounted",
                                   event_data.get("total_number_of_presses_counted", 0))
            if count == 2:
                action = "double"
            elif count == 3:
                action = "triple"

        return f"{prefix}_{role}_{action}"


# =============================================================================
# SINGLETON STORE
# =============================================================================

_store: Optional[DefinitionStore] = None


def get_definition_store() -> DefinitionStore:
    """Get or create the singleton DefinitionStore."""
    global _store
    if _store is None:
        _store = DefinitionStore()
    return _store