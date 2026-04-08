"""
Rotary Binding Manager — maps Matter rotary dial positions to device commands.
===============================================================================

Translates rotary position changes (0–N) into proportional commands on
target devices. For example:

    BILRESA left dial (18 positions) → Living Room Light brightness (0–254)
    Position 0  → brightness 0
    Position 9  → brightness 127
    Position 18 → brightness 254

Bindings are stored in the device's matter definition JSON under
"rotary_bindings".

Usage:
    manager = RotaryBindingManager()
    manager.set_dispatchers(zigbee_send_command, matter_send_command)
    manager.load_bindings(definition_store)

    # Called from matter_bridge.py on rotary events:
    await manager.on_rotary_event("matter_6", endpoint_id=3, position=12, max_positions=18)
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("matter.rotary_bindings")


class RotaryBinding:
    """A single rotary → target device binding."""

    def __init__(self, data: dict):
        self.source_ieee = data.get("source_ieee", "")
        self.source_ep = data.get("source_ep", 0)
        self.rotary_key = data.get("rotary_key", "")
        self.max_positions = data.get("max_positions", 18)

        # Step mode (paired directional EPs)
        self.mode = data.get("mode", "position")  # "step" or "position"
        self.cw_ep = data.get("cw_ep", 0)
        self.ccw_ep = data.get("ccw_ep", 0)
        self.step_size = data.get("step_size", 25)

        # Target
        self.target_ieee = data.get("target_ieee", "")
        self.target_command = data.get("target_command", "brightness")
        self.target_endpoint = data.get("target_endpoint")
        self.target_min = data.get("target_min", 0)
        self.target_max = data.get("target_max", 254)

        # Behaviour
        self.enabled = data.get("enabled", True)
        self.wrap = data.get("wrap", False)
        self.invert = data.get("invert", False)
        self.description = data.get("description", "")

        # Tracking
        self.last_sent = 0
        self.last_position = None
        self.last_value = None

    def interpolate(self, position: int) -> Any:
        """Convert rotary position to target value using linear interpolation."""
        if self.max_positions <= 0:
            return self.target_min

        pos = position
        if self.invert:
            pos = self.max_positions - position

        # Clamp
        pos = max(0, min(pos, self.max_positions))

        # Linear interpolation
        ratio = pos / self.max_positions
        value = self.target_min + ratio * (self.target_max - self.target_min)

        # Round to int for most commands, keep float for temperature etc.
        if self.target_command in ("brightness", "level", "position", "volume"):
            return int(round(value))
        elif self.target_command in ("color_temp", "color_temp_kelvin"):
            return int(round(value))
        elif self.target_command in ("heating_setpoint", "cooling_setpoint", "temperature"):
            return round(value, 1)
        return round(value, 2)

    def to_dict(self) -> dict:
        return {
            "source_ieee": self.source_ieee,
            "source_ep": self.source_ep,
            "rotary_key": self.rotary_key,
            "max_positions": self.max_positions,
            "mode": self.mode,
            "cw_ep": self.cw_ep,
            "ccw_ep": self.ccw_ep,
            "step_size": self.step_size,
            "target_ieee": self.target_ieee,
            "target_command": self.target_command,
            "target_endpoint": self.target_endpoint,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "enabled": self.enabled,
            "wrap": self.wrap,
            "invert": self.invert,
            "description": self.description,
        }


# Known command → default ranges
COMMAND_DEFAULTS = {
    "brightness":        {"min": 0, "max": 100, "label": "Brightness %"},
    "level":             {"min": 0, "max": 100, "label": "Level %"},
    "color_temp":        {"min": 153, "max": 500, "label": "Color Temp (mireds)"},
    "color_temp_kelvin": {"min": 2000, "max": 6500, "label": "Color Temp (K)"},
    "position":          {"min": 0, "max": 100, "label": "Position %"},
    "volume":            {"min": 0, "max": 100, "label": "Volume %"},
    "heating_setpoint":  {"min": 5, "max": 30, "label": "Heat Setpoint °C"},
    "cooling_setpoint":  {"min": 16, "max": 32, "label": "Cool Setpoint °C"},
    "fan_speed":         {"min": 0, "max": 100, "label": "Fan Speed %"},
}


class RotaryBindingManager:
    """
    Manages all rotary → target device bindings.

    Loads bindings from matter definition files and dispatches
    commands when rotary positions change.
    """

    def __init__(self):
        # Key = "source_ieee:endpoint_id" → list of bindings
        self._bindings: Dict[str, List[RotaryBinding]] = {}
        self._all_bindings: List[RotaryBinding] = []

        # Command dispatchers (set by main.py during startup)
        self._zigbee_send: Optional[Callable] = None
        self._matter_send: Optional[Callable] = None

        # Throttle: min ms between commands to same target
        self._throttle_ms = 100
        self._last_dispatch: Dict[str, float] = {}

        # Stats
        self._stats = {
            "events_received": 0,
            "commands_sent": 0,
            "errors": 0,
        }

    def set_dispatchers(self, zigbee_send=None, matter_send=None):
        """
        Set command dispatcher functions.

        Args:
            zigbee_send: async fn(ieee, command, value, endpoint_id) → dict
            matter_send: async fn(node_id, command, value) → dict
        """
        self._zigbee_send = zigbee_send
        self._matter_send = matter_send
        logger.info(f"Rotary binding dispatchers set: "
                    f"zigbee={'yes' if zigbee_send else 'no'}, "
                    f"matter={'yes' if matter_send else 'no'}")

    def load_from_definitions(self, definition_store):
        """Load rotary bindings from all matter definitions."""
        self._bindings.clear()
        self._all_bindings.clear()

        for fname, defn in definition_store._by_file.items():
            rotary_bindings = defn.get("rotary_bindings", {})
            if not rotary_bindings:
                continue

            source_vid = defn.get("vendor_id", 0)
            source_pid = defn.get("product_id", "")

            for rotary_key, binding_data in rotary_bindings.items():
                target = binding_data.get("target")
                if not target:
                    continue  # Unbound

                # Find source_ieee from definition endpoints
                ep = self._find_ep_for_rotary(defn, rotary_key)
                if not ep:
                    continue

                source_ieee = binding_data.get("source_ieee", "")
                max_positions = binding_data.get("positions", 18)

                b = RotaryBinding({
                    "source_ieee": source_ieee,
                    "source_ep": binding_data.get("cw_ep", ep),
                    "rotary_key": rotary_key,
                    "max_positions": binding_data.get("positions", 18),
                    "mode": binding_data.get("mode", "position"),
                    "cw_ep": binding_data.get("cw_ep", 0),
                    "ccw_ep": binding_data.get("ccw_ep", 0),
                    "step_size": binding_data.get("step_size", 25),
                    "target_ieee": target.get("ieee", ""),
                    "target_command": target.get("command", "brightness"),
                    "target_endpoint": target.get("endpoint"),
                    "target_min": target.get("min", 0),
                    "target_max": target.get("max", 254),
                    "enabled": binding_data.get("enabled", True),
                    "wrap": target.get("wrap", False),
                    "invert": target.get("invert", False),
                    "description": binding_data.get("description", ""),
                })

                key = f"{source_ieee}:{ep}"
                if key not in self._bindings:
                    self._bindings[key] = []
                self._bindings[key].append(b)
                self._all_bindings.append(b)

        logger.info(f"Loaded {len(self._all_bindings)} rotary binding(s)")

    def _find_ep_for_rotary(self, defn: dict, rotary_key: str) -> Optional[int]:
        """Find the endpoint number for a rotary key from state_mapping."""
        state_mapping = defn.get("state_mapping", {})
        mapping = state_mapping.get(rotary_key)
        if mapping:
            return mapping.get("ep")

        # Try rotary_bindings directly
        rb = defn.get("rotary_bindings", {}).get(rotary_key, {})
        return rb.get("ep")


    async def on_rotary_event(
            self,
            source_ieee: str,
            endpoint_id: int,
            position: int = None,
            max_positions: int = None,
            rotary_key: str = None,
            steps: int = 1,
    ):
        """
        Handle a rotary event.

        Two modes:
          - "step" mode (IKEA paired EPs): cw_ep fires → increment, ccw_ep fires → decrement
          - "position" mode (absolute encoder): position 0-N maps to min-max
        """
        self._stats["events_received"] += 1

        # Find matching binding by checking both cw_ep and ccw_ep
        matching_bindings = []
        direction = None

        for binding in self._all_bindings:
            if binding.source_ieee != source_ieee:
                continue
            if not binding.enabled:
                continue

            mode = binding.mode

            if mode == "step":
                if binding.cw_ep == endpoint_id:
                    direction = "cw"
                    matching_bindings.append(binding)
                elif binding.ccw_ep == endpoint_id:
                    direction = "ccw"
                    matching_bindings.append(binding)
            else:
                # Position mode — match by source_ep
                key = f"{source_ieee}:{endpoint_id}"
                if key == f"{binding.source_ieee}:{binding.source_ep}":
                    matching_bindings.append(binding)

        if not matching_bindings:
            return

        for binding in matching_bindings:
            # Throttle
            now = time.monotonic()
            throttle_key = f"{binding.target_ieee}:{binding.target_command}"
            last = self._last_dispatch.get(throttle_key, 0)
            if (now - last) * 1000 < self._throttle_ms:
                continue
            self._last_dispatch[throttle_key] = now

            if binding.mode == "step":
                # Step mode: increment/decrement current value
                current = binding.last_value
                if current is None:
                    current = (binding.target_min + binding.target_max) / 2

                step = binding.step_size * steps
                if direction == "ccw":
                    step = -step
                if binding.invert:
                    step = -step

                new_value = current + step
                # Clamp
                new_value = max(binding.target_min, min(new_value, binding.target_max))
                # Round for integer commands
                if binding.target_command in ("brightness", "level", "position", "volume", "color_temp"):
                    new_value = int(round(new_value))

                binding.last_value = new_value
                binding.last_sent = time.time()

                logger.info(
                    f"Rotary step: {source_ieee} EP{endpoint_id} {direction} "
                    f"→ {binding.target_ieee} {binding.target_command}={new_value} "
                    f"(step={step})"
                )

                await self._dispatch(binding, new_value)

            else:
                # Position mode (absolute encoder)
                if position is None:
                    continue
                value = binding.interpolate(position)
                binding.last_value = value
                binding.last_sent = time.time()
            await self._dispatch(binding, value)

    async def _dispatch(self, binding: RotaryBinding, value: Any):
        """Send command to the target device."""
        target_ieee = binding.target_ieee
        command = binding.target_command
        endpoint_id = binding.target_endpoint

        try:
            if target_ieee.startswith("matter_"):
                # Matter target
                if self._matter_send:
                    node_id = int(target_ieee.replace("matter_", ""))
                    result = await self._matter_send(node_id, command, value)
                    if isinstance(result, dict) and not result.get("success", True):
                        logger.warning(f"Rotary → Matter command failed: {result.get('error')}")
                        self._stats["errors"] += 1
                    else:
                        self._stats["commands_sent"] += 1
                else:
                    logger.warning("No Matter dispatcher registered")

            elif target_ieee.startswith("group:"):
                # Group target — dispatch via zigbee group command
                if self._zigbee_send:
                    result = await self._zigbee_send(target_ieee, command, value, endpoint_id)
                    self._stats["commands_sent"] += 1
                else:
                    logger.warning("No Zigbee dispatcher registered")

            else:
                # Zigbee target
                if self._zigbee_send:
                    result = await self._zigbee_send(target_ieee, command, value, endpoint_id)
                    if isinstance(result, dict) and not result.get("success", True):
                        logger.warning(f"Rotary → Zigbee command failed: {result.get('error')}")
                        self._stats["errors"] += 1
                    else:
                        self._stats["commands_sent"] += 1
                else:
                    logger.warning("No Zigbee dispatcher registered")

        except Exception as e:
            logger.error(f"Rotary dispatch error: {e}")
            self._stats["errors"] += 1

    # ── Binding CRUD ──────────────────────────────────────────────────

    def add_binding(self, source_ieee: str, rotary_key: str, ep: int,
                    max_positions: int, target: dict,
                    mode: str = "step", cw_ep: int = 0, ccw_ep: int = 0,
                    step_size: int = 25) -> dict:
        binding = RotaryBinding({
            "source_ieee": source_ieee,
            "source_ep": ep,
            "rotary_key": rotary_key,
            "max_positions": max_positions,
            "mode": mode,
            "cw_ep": cw_ep,
            "ccw_ep": ccw_ep,
            "step_size": step_size,
            "target_ieee": target.get("ieee", ""),
            "target_command": target.get("command", "brightness"),
            "target_endpoint": target.get("endpoint"),
            "target_min": target.get("min", 0),
            "target_max": target.get("max", 254),
            "enabled": True,
            "wrap": target.get("wrap", False),
            "invert": target.get("invert", False),
            "description": target.get("description", ""),
        })

        key = f"{source_ieee}:{ep}"

        # Replace existing binding for same rotary key
        if key in self._bindings:
            self._bindings[key] = [b for b in self._bindings[key]
                                   if b.rotary_key != rotary_key]
        else:
            self._bindings[key] = []

        self._bindings[key].append(binding)

        # Update all_bindings
        self._all_bindings = [b for b in self._all_bindings
                              if not (b.source_ieee == source_ieee
                                      and b.rotary_key == rotary_key)]
        self._all_bindings.append(binding)

        logger.info(f"Rotary binding added: {rotary_key} → {target.get('ieee')} {target.get('command')}")
        return {"success": True, "binding": binding.to_dict()}

    def remove_binding(self, source_ieee: str, rotary_key: str) -> dict:
        """Remove a rotary binding."""
        removed = False
        for key, bindings in self._bindings.items():
            before = len(bindings)
            self._bindings[key] = [b for b in bindings
                                   if b.rotary_key != rotary_key
                                   or b.source_ieee != source_ieee]
            if len(self._bindings[key]) < before:
                removed = True

        self._all_bindings = [b for b in self._all_bindings
                              if not (b.source_ieee == source_ieee
                                      and b.rotary_key == rotary_key)]

        return {"success": removed}

    def get_bindings(self, source_ieee: str = None) -> List[dict]:
        """Get all bindings, optionally filtered by source device."""
        bindings = self._all_bindings
        if source_ieee:
            bindings = [b for b in bindings if b.source_ieee == source_ieee]
        return [b.to_dict() for b in bindings]

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "total_bindings": len(self._all_bindings),
            "active_bindings": len([b for b in self._all_bindings if b.enabled]),
        }

    def save_to_definition(self, definition_store, source_ieee: str):
        """Save current bindings back to the definition file."""
        device_bindings = [b for b in self._all_bindings if b.source_ieee == source_ieee]
        if not device_bindings:
            return False

        # Find definition by iterating all and checking rotary_key match
        # OR by source_ieee in any rotary_binding
        defn = None
        fname = None
        for f, d in definition_store._by_file.items():
            # Check if any existing rotary_binding matches
            rb = d.get("rotary_bindings", {})
            for rk, rv in rb.items():
                if rv.get("source_ieee") == source_ieee:
                    defn = d
                    fname = f
                    break

            # Also check by rotary_key names matching binding keys
            if not defn:
                binding_keys = {b.rotary_key for b in device_bindings}
                sm_rotary_keys = {k for k in d.get("state_mapping", {})
                                  if "rotary" in k}
                rb_keys = set(rb.keys())
                if binding_keys & (rb_keys | sm_rotary_keys):
                    defn = d
                    fname = f

            if defn:
                break

        if not defn or not fname:
            logger.warning(f"No definition found for {source_ieee}")
            return False

        # Ensure rotary_bindings section exists
        if "rotary_bindings" not in defn:
            defn["rotary_bindings"] = {}

        for b in device_bindings:
            defn["rotary_bindings"][b.rotary_key] = {
                "mode": b.mode,
                "cw_ep": b.cw_ep,
                "ccw_ep": b.ccw_ep,
                "step_size": b.step_size,
                "positions": b.max_positions,
                "description": b.description,
                "source_ieee": b.source_ieee,
                "ep": b.source_ep,
                "enabled": b.enabled,
                "target": {
                    "ieee": b.target_ieee,
                    "command": b.target_command,
                    "endpoint": b.target_endpoint,
                    "min": b.target_min,
                    "max": b.target_max,
                    "wrap": b.wrap,
                    "invert": b.invert,
                },
            }

        definition_store.save(defn, fname)
        logger.info(f"Saved {len(device_bindings)} rotary binding(s) to {fname}")
        return True


# ── Singleton ─────────────────────────────────────────────────────────

_manager: Optional[RotaryBindingManager] = None


def get_rotary_binding_manager() -> RotaryBindingManager:
    global _manager
    if _manager is None:
        _manager = RotaryBindingManager()
    return _manager