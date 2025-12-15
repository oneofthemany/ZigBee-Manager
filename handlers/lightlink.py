"""
LightLink cluster handler (0x1000)
Based on ZHA's lightlink.py implementation.
Handles commissioning groups for Ikea Tradfri and Philips Hue bulbs.
"""
import logging
import asyncio
from typing import Any, Dict, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.lightlink")


@register_handler(0x1000)
class LightLinkHandler(ClusterHandler):
    """
    LightLink cluster handler (0x1000).

    LightLink (now called Zigbee 3.0 Touchlink) is used for commissioning
    bulbs without a centralized coordinator. When a bulb pairs, it may
    create LightLink groups that need to be added to the coordinator.

    This handler automatically:
    1. Reads group identifiers from the bulb
    2. Adds the coordinator to those groups
    3. Ensures bulb can be controlled via group commands
    """
    CLUSTER_ID = 0x1000

    # LightLink doesn't need binding (it's a commissioning cluster)
    BIND = False

    async def configure(self):
        """
        Configure LightLink cluster - add coordinator to bulb's groups.
        This is critical for Ikea and Philips bulbs to work properly.
        """
        logger.info(f"[{self.device.ieee}] Configuring LightLink cluster...")

        # Get the coordinator device
        try:
            application = self.device.service.app
            coordinator_ieee = application.state.node_info.ieee
            coordinator = application.get_device(coordinator_ieee)
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Unable to locate coordinator: {e}")
            return False

        try:
            # Read group identifiers from the bulb
            logger.debug(f"[{self.device.ieee}] Reading LightLink group identifiers...")

            async with asyncio.timeout(10.0):
                response = await self.cluster.get_group_identifiers(0)

            logger.debug(f"[{self.device.ieee}] LightLink response: {response}")

            # Parse the response
            groups = []

            # Check if it's a default response (no groups)
            if hasattr(response, 'command_id'):
                # It's a default response - no groups configured
                logger.info(f"[{self.device.ieee}] No LightLink groups found, creating default group")
                groups = []
            elif hasattr(response, 'group_info_records'):
                # It's a proper response with group info
                groups = response.group_info_records
                logger.info(f"[{self.device.ieee}] Found {len(groups)} LightLink groups")
            else:
                logger.debug(f"[{self.device.ieee}] Unexpected response format: {type(response)}")

            # Add coordinator to groups
            if groups:
                for group_info in groups:
                    group_id = group_info.group_id
                    logger.info(f"[{self.device.ieee}] Adding coordinator to group 0x{group_id:04X}")
                    try:
                        await coordinator.add_to_group(group_id)
                        logger.info(f"[{self.device.ieee}] ✅ Added to group 0x{group_id:04X}")
                    except Exception as e:
                        logger.warning(f"[{self.device.ieee}] Failed to add to group 0x{group_id:04X}: {e}")
            else:
                # No groups found - create default LightLink group (0x0000)
                logger.info(f"[{self.device.ieee}] Creating default LightLink group 0x0000")
                try:
                    await coordinator.add_to_group(0x0000, name="Lightlink Group")
                    logger.info(f"[{self.device.ieee}] ✅ Created default LightLink group")
                except Exception as e:
                    logger.warning(f"[{self.device.ieee}] Failed to create default group: {e}")

            logger.info(f"[{self.device.ieee}] LightLink configuration complete")
            return True

        except asyncio.TimeoutError:
            logger.warning(f"[{self.device.ieee}] LightLink configuration timed out")
            return False
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] LightLink configuration failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """LightLink cluster doesn't typically report attributes."""
        logger.debug(f"[{self.device.ieee}] LightLink attribute 0x{attrid:04X} = {value}")

    def get_attr_name(self, attrid: int) -> str:
        return f"lightlink_attr_{attrid:04x}"
