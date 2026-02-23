"""
modules/network_init.py

Auto-generation of Zigbee network credentials and channel selection.
Called at startup when config values are absent/placeholder.
"""

import os
import random
import logging
import yaml

logger = logging.getLogger(__name__)

# Zigbee 2.4 GHz channels - only non-overlapping with Wi-Fi 1/6/11 preferred
# Channels 11-26 are valid Zigbee channels (802.15.4)
ZIGBEE_CHANNELS = list(range(11, 27))

# Channels least likely to conflict with typical Wi-Fi deployments
# WiFi ch1=2412MHz(ZB11-12), WiFi ch6=2437MHz(ZB16-17), WiFi ch11=2462MHz(ZB21-22)
PREFERRED_CHANNELS = [15, 20, 25, 26, 24, 19, 14, 13]

# Placeholder patterns that indicate a value hasn't been configured
PLACEHOLDER_PATTERNS = [
    "etc.", "example", "xxx", "your_", "change_me", "placeholder"
]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def generate_pan_id() -> str:
    """Random 16-bit PAN ID as 4-char uppercase hex."""
    return f"{random.randint(0x0001, 0xFFFE):04X}"


def generate_extended_pan_id() -> list:
    """Random 8-byte extended PAN ID as list of ints."""
    return [random.randint(0, 255) for _ in range(8)]


def generate_network_key() -> list:
    """Random 128-bit network key as list of 16 ints."""
    return [random.randint(0, 255) for _ in range(16)]


def select_best_channel(energy_results: dict) -> int:
    """
    Pick the channel with lowest energy from scan results.
    Falls back through PREFERRED_CHANNELS if scan unavailable.

    Args:
        energy_results: {channel: energy_level} where energy is 0-255 (higher = noisier)

    Returns:
        Best channel (int)
    """
    if not energy_results:
        return PREFERRED_CHANNELS[0]  # Default: channel 15

    # Filter to valid Zigbee channels only
    valid = {ch: e for ch, e in energy_results.items() if ch in ZIGBEE_CHANNELS}
    if not valid:
        return PREFERRED_CHANNELS[0]

    # Sort by energy (ascending), then by preference order
    def score(ch):
        energy = valid.get(ch, 255)
        pref_idx = PREFERRED_CHANNELS.index(ch) if ch in PREFERRED_CHANNELS else 99
        return (energy, pref_idx)

    best = min(valid.keys(), key=score)
    logger.info(f"Auto channel selection: {best} (energy={valid[best]})")
    return best


# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------

def _is_placeholder(value) -> bool:
    """Return True if value looks like an unfilled placeholder."""
    if value is None:
        return True
    if isinstance(value, list):
        # e.g. [12, 139, 142, "etc."]  or  []
        if not value:
            return True
        return any(isinstance(v, str) and any(p in str(v).lower() for p in PLACEHOLDER_PATTERNS) for v in value)
    if isinstance(value, str):
        if not value.strip():
            return True
        return any(p in value.lower() for p in PLACEHOLDER_PATTERNS)
    return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ensure_network_credentials(config_path: str = "./config/config.yaml") -> dict:
    """
    Load config, auto-fill any missing/placeholder Zigbee network credentials,
    write back if changed, and return the (possibly updated) config dict.

    Returns:
        Updated config dict
    """
    if not os.path.exists(config_path):
        logger.warning(f"Config not found at {config_path}")
        return {}

    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    zigbee = config.setdefault("zigbee", {})
    changed = False

    # --- Channel ---
    if _is_placeholder(zigbee.get("channel")) or zigbee.get("channel") == 0:
        old = zigbee.get("channel")
        zigbee["channel"] = PREFERRED_CHANNELS[0]  # Will be replaced after scan
        logger.info(f"Auto-set channel: {old} -> {zigbee['channel']}")
        changed = True

    # --- PAN ID ---
    if _is_placeholder(zigbee.get("pan_id")):
        zigbee["pan_id"] = generate_pan_id()
        logger.info(f"Auto-generated PAN ID: {zigbee['pan_id']}")
        changed = True

    # --- Extended PAN ID ---
    if _is_placeholder(zigbee.get("extended_pan_id")):
        zigbee["extended_pan_id"] = generate_extended_pan_id()
        logger.info(f"Auto-generated extended PAN ID")
        changed = True

    # --- Network Key ---
    if _is_placeholder(zigbee.get("network_key")):
        zigbee["network_key"] = generate_network_key()
        logger.info(f"Auto-generated network key")
        changed = True

    if changed:
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        logger.info("Config updated with auto-generated network credentials")

    return config