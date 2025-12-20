"""
Window Covering cluster handler for Zigbee devices.
Handles: Blinds, Shutters, Curtains, Shades (Cluster 0x0102)
"""
import logging
from typing import Any, Dict, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.blinds")


@register_handler(0x0102)
class WindowCoveringHandler(ClusterHandler):
    """
    Handles Window Covering cluster (0x0102).
    """
    CLUSTER_ID = 0x0102

    # Configure reporting: Attribute, Min Interval, Max Interval, Change amount
    REPORT_CONFIG = [
        ("current_position_lift_percentage", 1, 300, 1),  # Report 1% changes
        ("current_position_tilt_percentage", 1, 300, 1),
    ]

    # Attribute IDs
    ATTR_COVERING_TYPE = 0x0000
    ATTR_PHYSICAL_CLOSED_LIMIT_LIFT = 0x0001
    ATTR_PHYSICAL_CLOSED_LIMIT_TILT = 0x0002
    ATTR_CURRENT_POSITION_LIFT = 0x0003
    ATTR_CURRENT_POSITION_TILT = 0x0004
    ATTR_NUMBER_OF_ACTUATIONS_LIFT = 0x0005
    ATTR_NUMBER_OF_ACTUATIONS_TILT = 0x0006
    ATTR_CONFIG_STATUS = 0x0007
    ATTR_CURRENT_POSITION_LIFT_PCT = 0x0008
    ATTR_CURRENT_POSITION_TILT_PCT = 0x0009
    ATTR_INSTALLED_OPEN_LIMIT_LIFT = 0x0010
    ATTR_INSTALLED_CLOSED_LIMIT_LIFT = 0x0011
    ATTR_INSTALLED_OPEN_LIMIT_TILT = 0x0012
    ATTR_INSTALLED_CLOSED_LIMIT_TILT = 0x0013
    ATTR_MODE = 0x0017

    COVERING_TYPES = {
        0x00: "rollershade",
        0x01: "rollershade_2_motor",
        0x02: "rollershade_exterior",
        0x03: "rollershade_exterior_2_motor",
        0x04: "drapery",
        0x05: "awning",
        0x06: "shutter",
        0x07: "tilt_blind_tilt_only",
        0x08: "tilt_blind_lift_and_tilt",
        0x09: "projector_screen",
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None: return

        # Handle wrapped values if they come from zigpy
        if hasattr(value, 'value'):
            value = value.value

        if attrid == self.ATTR_CURRENT_POSITION_LIFT_PCT:
            # Zigbee: 0=Open, 100=Closed
            # UI/HA: 100=Open, 0=Closed
            # We invert it here for the UI
            position = 100 - value if isinstance(value, (int, float)) else None

            self.device.update_state({
                "position": position,
                "cover_position": position,
                "is_closed": value == 100,
                "is_open": value == 0
            })
            logger.debug(f"[{self.device.ieee}] Cover position: {position}% (Raw: {value})")

        elif attrid == self.ATTR_CURRENT_POSITION_TILT_PCT:
            tilt = value
            self.device.update_state({"tilt_position": tilt})

        elif attrid == self.ATTR_COVERING_TYPE:
            cover_type = self.COVERING_TYPES.get(value, f"unknown_{value}")
            self.device.update_state({"cover_type": cover_type})

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_CURRENT_POSITION_LIFT_PCT: "position",
            self.ATTR_CURRENT_POSITION_TILT_PCT: "tilt_position",
        }

    # --- COMMANDS ---

    async def open(self):
        """Open the cover."""
        await self.cluster.up_open()
        logger.info(f"[{self.device.ieee}] Opening cover")
        # Optimistic update
        self.device.update_state({"is_closed": False})

    async def close(self):
        """Close the cover."""
        await self.cluster.down_close()
        logger.info(f"[{self.device.ieee}] Closing cover")
        # Optimistic update
        self.device.update_state({"is_closed": True})

    async def stop(self):
        """Stop cover movement."""
        await self.cluster.stop()
        logger.info(f"[{self.device.ieee}] Stopping cover")

    async def set_position(self, position: int):
        """Set cover position (0=closed, 100=open)."""
        # API/UI sends 0-100 where 100 is Open.
        # Zigbee expects 0-100 where 0 is Open.
        # So we invert: 100 - UI_Value = Zigbee_Value
        zigbee_value = 100 - int(position)

        await self.cluster.go_to_lift_percentage(zigbee_value)
        logger.info(f"[{self.device.ieee}] Set cover position to {position}% (Zigbee: {zigbee_value})")

    async def set_tilt(self, tilt: int):
        """Set tilt position (0-100)."""
        await self.cluster.go_to_tilt_percentage(int(tilt))
        logger.info(f"[{self.device.ieee}] Set tilt to {tilt}%")

    # --- HA DISCOVERY ---
    def get_discovery_configs(self):
        """Generate Home Assistant discovery configs."""
        base_topic = self.device.service.mqtt.base_topic
        return [{
            "component": "cover",
            "object_id": "cover",
            "config": {
                "name": "Window Cover",
                "device_class": "shutter",
                "command_topic": "CMD_TOPIC_PLACEHOLDER",
                "position_topic": f"{base_topic}/{self.device.service.get_safe_name(self.device.ieee)}",
                "position_template": "{{ value_json.position }}",
                "set_position_template": "{ \"command\": \"position\", \"value\": {{ position }} }",
                "payload_open": "{ \"command\": \"open\" }",
                "payload_close": "{ \"command\": \"close\" }",
                "payload_stop": "{ \"command\": \"stop\" }",
                "position_open": 100,
                "position_closed": 0
            }
        }]
