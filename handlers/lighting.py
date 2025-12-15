"""
Lighting cluster handlers for Zigbee Home Automation.
Handles: Color Control, Ballast clusters for bulbs and LED strips.

Based on ZHA's lighting.py architecture for proper bulb support.
"""
import logging
from typing import Any, Dict, List, Optional
import asyncio

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.lighting")


# ============================================================
# LEVEL CONTROL CLUSTER (0x0008)
# Moved here from general.py because it's lighting related
# ============================================================
# NOTE: Registration DISABLED - using general.py version to avoid conflicts
# This handler provides lighting-specific level control features
# @register_handler(0x0008)
class LevelControlHandler_Lighting(ClusterHandler):
    CLUSTER_ID = 0x0008
    REPORT_CONFIG = [("current_level", 1, 300, 5)]
    ATTR_CURRENT_LEVEL = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_CURRENT_LEVEL:
            if value is not None and value != 0xFF:
                self._update_level(value)

    def _update_level(self, level):
        pct = round((level / 254) * 100)
        ep_id = self.endpoint.endpoint_id
        updates = {
            f"brightness_{ep_id}": pct,
            f"level_{ep_id}": level
        }
        if ep_id == 1:
            updates["brightness"] = pct
            updates["level"] = level
        self.device.update_state(updates)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_CURRENT_LEVEL: "brightness"}

    # --- OPTIMISTIC UPDATES ADDED HERE ---
    async def set_level(self, level: int, transition_time: int = 10):
        await self.cluster.move_to_level(level, transition_time)
        self._update_level(level) # Optimistic

    async def set_brightness_pct(self, percent: int, transition_time: int = 10):
        level = round((percent / 100) * 254)
        await self.set_level(level, transition_time)


    def get_discovery_configs(self) -> List[Dict]:
        # Suppress separate Number entity for brightness,
        # because OnOffHandler now handles it as part of the Light entity.
        return []

# ============================================================
# BALLAST CLUSTER (0x0301)
# Used by some lighting fixtures for ballast control
# ============================================================
@register_handler(0x0301)
class BallastClusterHandler(ClusterHandler):
    """
    Handles Ballast Configuration cluster (0x0301).
    Used by some dimmable lights for ballast control.
    """
    CLUSTER_ID = 0x0301

    ATTR_PHYSICAL_MIN_LEVEL = 0x0000
    ATTR_PHYSICAL_MAX_LEVEL = 0x0001
    ATTR_BALLAST_STATUS = 0x0002
    ATTR_MIN_LEVEL = 0x0010
    ATTR_MAX_LEVEL = 0x0011
    ATTR_POWER_ON_LEVEL = 0x0012
    ATTR_POWER_ON_FADE_TIME = 0x0013
    ATTR_INTRINSIC_BALLAST_FACTOR = 0x0014
    ATTR_BALLAST_FACTOR_ADJUSTMENT = 0x0015

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        """Handle ballast attribute updates."""
        if attrid == self.ATTR_BALLAST_STATUS:
            self.device.update_state({"ballast_status": value})
        elif attrid == self.ATTR_MIN_LEVEL:
            self.device.update_state({"ballast_min_level": value})
        elif attrid == self.ATTR_MAX_LEVEL:
            self.device.update_state({"ballast_max_level": value})


# ============================================================
# COLOR CONTROL CLUSTER (0x0300)
# Main handler for RGB/RGBW/Color Temperature bulbs
# ============================================================
@register_handler(0x0300)
class ColorClusterHandler(ClusterHandler):
    """
    Handles Color Control cluster (0x0300).
    """
    CLUSTER_ID = 0x0300

    # Report configuration - report color changes
    REPORT_CONFIG = [
        ("current_x", 1, 300, 100),  # X coordinate
        ("current_y", 1, 300, 100),  # Y coordinate
        ("color_temperature", 1, 300, 10),  # Color temp in mireds
        ("current_hue", 1, 300, 5),  # Hue
        ("current_saturation", 1, 300, 5),  # Saturation
    ]

    # Attribute IDs (ZCL Spec 5.2.2.2)
    ATTR_CURRENT_HUE = 0x0000
    ATTR_CURRENT_SAT = 0x0001
    ATTR_REMAINING_TIME = 0x0002
    ATTR_CURRENT_X = 0x0003
    ATTR_CURRENT_Y = 0x0004
    ATTR_DRIFT_COMPENSATION = 0x0005
    ATTR_COMPENSATION_TEXT = 0x0006
    ATTR_COLOR_TEMP = 0x0007
    ATTR_COLOR_MODE = 0x0008
    ATTR_OPTIONS = 0x000F
    ATTR_ENHANCED_CURRENT_HUE = 0x4000
    ATTR_ENHANCED_COLOR_MODE = 0x4001
    ATTR_COLOR_LOOP_ACTIVE = 0x4002
    ATTR_COLOR_LOOP_DIRECTION = 0x4003
    ATTR_COLOR_LOOP_TIME = 0x4004
    ATTR_COLOR_LOOP_START_HUE = 0x4005
    ATTR_COLOR_LOOP_STORED_HUE = 0x4006
    ATTR_COLOR_CAPABILITIES = 0x400A
    ATTR_COLOR_TEMP_PHYSICAL_MIN = 0x400B
    ATTR_COLOR_TEMP_PHYSICAL_MAX = 0x400C
    ATTR_COUPLE_COLOR_TEMP_TO_LEVEL = 0x4010
    ATTR_STARTUP_COLOR_TEMP = 0x4010

    # Color capabilities bitmap (ZCL Spec 5.2.2.2.13)
    COLOR_CAP_HUE_SAT = 0x0001  # Supports hue/saturation
    COLOR_CAP_ENHANCED_HUE = 0x0002  # Supports enhanced hue (16-bit)
    COLOR_CAP_COLOR_LOOP = 0x0004  # Supports color loop
    COLOR_CAP_XY = 0x0008  # Supports XY color
    COLOR_CAP_COLOR_TEMP = 0x0010  # Supports color temperature

    # Default limits (ZCL Spec 5.2.2.2.11-12)
    MIN_MIREDS = 153  # ~6500K (cool white)
    MAX_MIREDS = 500  # ~2000K (warm white)

    # Color modes (ZCL Spec 5.2.2.2.8)
    COLOR_MODE_HUE_SAT = 0x00
    COLOR_MODE_XY = 0x01
    COLOR_MODE_TEMP = 0x02

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._color_capabilities = None
        self._min_mireds = self.MIN_MIREDS
        self._max_mireds = self.MAX_MIREDS
        self._current_color_mode = None

    async def configure(self):
        """
        Configure color cluster with ZHA approach.

        Critical for bulb pairing:
        1. Read color capabilities FIRST (before binding)
        2. Read physical min/max color temp limits
        3. Then bind cluster to coordinator
        4. Finally configure attribute reporting
        """
        try:
            logger.info(f"[{self.device.ieee}] Configuring Color cluster...")

            # STEP 1: Read capabilities and limits BEFORE binding
            # This is CRITICAL for proper bulb support
            attrs_to_read = [
                self.ATTR_COLOR_CAPABILITIES,
                self.ATTR_COLOR_TEMP_PHYSICAL_MIN,
                self.ATTR_COLOR_TEMP_PHYSICAL_MAX,
                self.ATTR_COLOR_MODE,
            ]

            try:
                async with asyncio.timeout(5.0):
                    result = await self.cluster.read_attributes(attrs_to_read)

                if result and result[0]:
                    data = result[0]

                    # Parse color capabilities
                    if self.ATTR_COLOR_CAPABILITIES in data:
                        caps = data[self.ATTR_COLOR_CAPABILITIES]
                        if hasattr(caps, 'value'):
                            caps = caps.value
                        self._color_capabilities = caps

                        # Log supported features
                        features = []
                        if caps & self.COLOR_CAP_HUE_SAT:
                            features.append("Hue/Sat")
                        if caps & self.COLOR_CAP_XY:
                            features.append("XY")
                        if caps & self.COLOR_CAP_COLOR_TEMP:
                            features.append("Color Temp")
                        if caps & self.COLOR_CAP_COLOR_LOOP:
                            features.append("Color Loop")

                        logger.info(f"[{self.device.ieee}] Color capabilities: {', '.join(features)}")

                    # Parse color temp range
                    if self.ATTR_COLOR_TEMP_PHYSICAL_MIN in data:
                        min_val = data[self.ATTR_COLOR_TEMP_PHYSICAL_MIN]
                        if hasattr(min_val, 'value'):
                            min_val = min_val.value
                        if min_val > 0:
                            self._min_mireds = min_val
                            kelvin_max = round(1000000 / min_val)
                            logger.info(f"[{self.device.ieee}] Min color temp: {min_val} mireds (~{kelvin_max}K)")

                    if self.ATTR_COLOR_TEMP_PHYSICAL_MAX in data:
                        max_val = data[self.ATTR_COLOR_TEMP_PHYSICAL_MAX]
                        if hasattr(max_val, 'value'):
                            max_val = max_val.value
                        if max_val > 0:
                            self._max_mireds = max_val
                            kelvin_min = round(1000000 / max_val)
                            logger.info(f"[{self.device.ieee}] Max color temp: {max_val} mireds (~{kelvin_min}K)")

                    # Parse current color mode
                    if self.ATTR_COLOR_MODE in data:
                        mode = data[self.ATTR_COLOR_MODE]
                        if hasattr(mode, 'value'):
                            mode = mode.value
                        self._current_color_mode = mode
                        logger.debug(f"[{self.device.ieee}] Current color mode: {mode}")

            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[{self.device.ieee}] Failed to read color attributes: {e}")
                # Continue anyway - some bulbs don't respond to reads until after binding

            # STEP 2: Now do standard binding and configure reporting
            await super().configure()

            logger.info(f"[{self.device.ieee}] âœ… Color cluster configured successfully")
            return True

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Color cluster configuration failed: {e}")
            return False

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        """Handle color attribute updates."""
        if hasattr(value, 'value'):
            value = value.value

        updates = {}

        # Hue & Saturation
        if attrid == self.ATTR_CURRENT_HUE:
            updates["hue"] = value
            updates["color_hue"] = value

        elif attrid == self.ATTR_CURRENT_SAT:
            updates["saturation"] = value
            updates["color_saturation"] = value

        # XY Color
        elif attrid == self.ATTR_CURRENT_X:
            updates["color_x"] = value

        elif attrid == self.ATTR_CURRENT_Y:
            updates["color_y"] = value

        # Color Temperature
        elif attrid == self.ATTR_COLOR_TEMP:
            updates["color_temp"] = value
            updates["color_temperature_mireds"] = value
            # Calculate and store Kelvin too
            if value and value > 0:
                kelvin = round(1000000 / value)
                updates["color_temperature"] = kelvin
                updates["color_temp_kelvin"] = kelvin
                logger.debug(f"[{self.device.ieee}] Color temp: {value} mireds = {kelvin}K")

        # Color Mode
        elif attrid == self.ATTR_COLOR_MODE:
            mode_names = {
                self.COLOR_MODE_HUE_SAT: "hs",
                self.COLOR_MODE_XY: "xy",
                self.COLOR_MODE_TEMP: "color_temp"
            }
            mode_name = mode_names.get(value, str(value))
            updates["color_mode"] = mode_name
            self._current_color_mode = value
            logger.debug(f"[{self.device.ieee}] Color mode: {mode_name}")

        # Color Loop
        elif attrid == self.ATTR_COLOR_LOOP_ACTIVE:
            updates["color_loop_active"] = bool(value)

        if updates:
            self.device.update_state(updates)

    # ========================================================================
    # PROPERTIES - Following ZHA API
    # ========================================================================

    @property
    def color_capabilities(self) -> Optional[int]:
        """Return ZCL color capabilities bitmap."""
        return self._color_capabilities

    @property
    def color_mode(self) -> Optional[int]:
        """Return cached value of the color_mode attribute."""
        return self._current_color_mode

    @property
    def xy_supported(self) -> bool:
        """Return True if XY color is supported."""
        return (
                self._color_capabilities is not None
                and (self._color_capabilities & self.COLOR_CAP_XY) != 0
        )

    @property
    def color_temp_supported(self) -> bool:
        """Return True if color temperature is supported."""
        return (
                self._color_capabilities is not None
                and (self._color_capabilities & self.COLOR_CAP_COLOR_TEMP) != 0
        )

    @property
    def hue_sat_supported(self) -> bool:
        """Return True if hue/saturation is supported."""
        return (
                self._color_capabilities is not None
                and (self._color_capabilities & self.COLOR_CAP_HUE_SAT) != 0
        )

    @property
    def color_loop_supported(self) -> bool:
        """Return True if color loop is supported."""
        return (
                self._color_capabilities is not None
                and (self._color_capabilities & self.COLOR_CAP_COLOR_LOOP) != 0
        )

    @property
    def min_mireds(self) -> int:
        """Return the coldest color_temp that this bulb supports."""
        return self._min_mireds

    @property
    def max_mireds(self) -> int:
        """Return the warmest color_temp that this bulb supports."""
        return self._max_mireds

    # ========================================================================
    # COMMAND METHODS
    # ========================================================================

    async def set_color_temp_kelvin(self, kelvin: int, transition_time: int = 10):
        """
        Set color temperature in Kelvin.

        Args:
            kelvin: Color temperature in Kelvin (2000-6500)
            transition_time: Transition time in 1/10th seconds (default 1 second)
        """
        # Convert Kelvin to mireds: mireds = 1,000,000 / kelvin
        mireds = round(1000000 / kelvin)

        # Clamp to supported range
        mireds = max(self._min_mireds, min(self._max_mireds, mireds))

        await self.cluster.move_to_color_temp(mireds, transition_time)
        logger.info(f"[{self.device.ieee}] Set color temp to {kelvin}K ({mireds} mireds)")

        # Optimistic update
        self.device.update_state({
            "color_temperature": kelvin,
            "color_temp_kelvin": kelvin,
            "color_temperature_mireds": mireds,
            "color_temp": mireds
        })

    async def set_hue_sat(self, hue: int, saturation: int, transition_time: int = 10):
        """
        Set color using hue and saturation.

        Args:
            hue: Hue value (0-254)
            saturation: Saturation value (0-254)
            transition_time: Transition time in 1/10th seconds
        """
        await self.cluster.move_to_hue_and_saturation(hue, saturation, transition_time)
        logger.info(f"[{self.device.ieee}] Set color to H:{hue} S:{saturation}")

        # Optimistic update
        self.device.update_state({
            "hue": hue,
            "color_hue": hue,
            "saturation": saturation,
            "color_saturation": saturation
        })

    async def set_xy_color(self, x: int, y: int, transition_time: int = 10):
        """
        Set color using XY coordinates.

        Args:
            x: X coordinate (0-65535)
            y: Y coordinate (0-65535)
            transition_time: Transition time in 1/10th seconds
        """
        await self.cluster.move_to_color(x, y, transition_time)
        logger.info(f"[{self.device.ieee}] Set color to X:{x} Y:{y}")

        # Optimistic update
        self.device.update_state({
            "color_x": x,
            "color_y": y
        })

    async def color_loop_set(self, update_flags: int, action: int, direction: int,
                             time: int = 0, start_hue: int = 0):
        """
        Control color loop effect.

        Args:
            update_flags: Bitmap of fields to update
            action: 0=deactivate, 1=activate from start_hue, 2=activate from current hue
            direction: 0=decrement, 1=increment
            time: Time for one loop (in seconds, 0=use default)
            start_hue: Starting hue (only used if action=1)
        """
        await self.cluster.color_loop_set(
            update_flags, action, direction, time, start_hue
        )
        logger.info(f"[{self.device.ieee}] Color loop: action={action}, direction={direction}")

    # ========================================================================
    # CONFIGURATION & DISCOVERY
    # ========================================================================

    def get_pollable_attributes(self) -> Dict[int, str]:
        """Return attributes that can be polled."""
        attrs = {
            self.ATTR_COLOR_MODE: "color_mode",
        }

        # Add attributes based on capabilities
        if self.color_temp_supported:
            attrs[self.ATTR_COLOR_TEMP] = "color_temperature"

        if self.xy_supported:
            attrs[self.ATTR_CURRENT_X] = "color_x"
            attrs[self.ATTR_CURRENT_Y] = "color_y"

        if self.hue_sat_supported:
            attrs[self.ATTR_CURRENT_HUE] = "hue"
            attrs[self.ATTR_CURRENT_SAT] = "saturation"

        return attrs

    def get_configuration_options(self) -> List[Dict]:
        """Expose configuration options to UI."""
        options = []

        if self.color_temp_supported:
            options.append({
                "name": "startup_color_temp",
                "label": "Startup Color Temperature (mireds)",
                "type": "number",
                "min": self._min_mireds,
                "max": self._max_mireds,
                "description": f"Color temp on power-up ({self._min_mireds}-{self._max_mireds} mireds)",
                "attribute_id": self.ATTR_STARTUP_COLOR_TEMP
            })

        return options

    def get_discovery_configs(self) -> List[Dict]:
        """Generate Home Assistant discovery configs."""
        # Color bulbs are usually discovered via Light entity in Home Assistant
        # which combines On/Off, Level, and Color clusters
        # We'll return an empty list since HA discovers lights automatically
        # via the zigbee integration's light platform
        return []

    def get_attr_name(self, attrid: int) -> str:
        """Get human-readable attribute name."""
        names = {
            self.ATTR_CURRENT_HUE: "hue",
            self.ATTR_CURRENT_SAT: "saturation",
            self.ATTR_CURRENT_X: "color_x",
            self.ATTR_CURRENT_Y: "color_y",
            self.ATTR_COLOR_TEMP: "color_temperature",
            self.ATTR_COLOR_MODE: "color_mode",
        }
        return names.get(attrid, super().get_attr_name(attrid))