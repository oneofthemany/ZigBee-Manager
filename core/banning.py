"""
Device banning mixin for ZigbeeService.
"""
import asyncio
import logging
import zigpy.types

logger = logging.getLogger("core.banning")


class BanningMixin:
    """Device ban/unban methods."""

    def ban_device(self, ieee: str, reason: str = None) -> dict:
        """Ban a device by IEEE address."""
        ieee = str(ieee).lower()
        success = self.ban_manager.ban(ieee, reason)

        if success and ieee in self.devices:
            device = self.devices[ieee]
            asyncio.create_task(self._kick_banned_device(device.zigpy_dev))

        return {
            "success": success, "ieee": ieee,
            "message": f"Device {ieee} has been banned" if success else f"Device {ieee} was already banned"
        }

    def unban_device(self, ieee: str) -> dict:
        """Remove a device from the ban list."""
        ieee = str(ieee).lower()
        success = self.ban_manager.unban(ieee)
        return {
            "success": success, "ieee": ieee,
            "message": f"Device {ieee} has been unbanned" if success else f"Device {ieee} was not banned"
        }

    def get_banned_devices(self) -> list:
        """Get list of all banned IEEE addresses."""
        return self.ban_manager.get_banned_list()

    def is_device_banned(self, ieee: str) -> bool:
        """Check if a device is banned."""
        return self.ban_manager.is_banned(ieee)

    async def _kick_banned_device(self, device):
        """Send leave request to a banned device and remove from zigpy."""
        ieee = str(device.ieee)
        try:
            logger.info(f"[{ieee}] Sending leave request to banned device...")
            await device.zdo.leave()
            logger.info(f"[{ieee}] Leave request sent successfully")
        except Exception as e:
            logger.warning(f"[{ieee}] Leave request failed (device may have already left): {e}")

        try:
            z_ieee = zigpy.types.EUI64.convert(ieee)
            if z_ieee in self.app.devices:
                await self.app.remove(z_ieee)
                logger.info(f"[{ieee}] Removed from zigpy device list")
        except Exception as e:
            logger.debug(f"[{ieee}] Could not remove from zigpy: {e}")
