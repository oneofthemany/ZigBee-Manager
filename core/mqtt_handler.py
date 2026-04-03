"""
MQTT handler mixin for ZigbeeService.
Handles device announcements, republishing, and MQTT command routing.
"""
import asyncio
import json
import logging
from modules.json_helpers import prepare_for_json, sanitise_device_state

logger = logging.getLogger("core.mqtt")


class MQTTHandlerMixin:
    """MQTT announcement and command handling methods."""

    async def republish_all_devices(self):
        """
        Called when Home Assistant restarts (Birth Message).
        Republishes Discovery Config and Current State for ALL devices.
        """
        logger.info("✅ Home Assistant is ONLINE! Republishing all devices...")
        await self._emit("log", {"level": "INFO", "message": "HA Online - Republishing all devices", "ieee": None})
        await self._emit("ha_status", {"status": "online"})

        await asyncio.sleep(2)

        for ieee, device in self.devices.items():
            try:
                await self.announce_device(ieee)
                if device.state:
                    self.handle_device_update(device, device.state, full_state=device.state, qos=1)
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Failed to republish {ieee}: {e}")

        logger.info("✅ All devices republished to Home Assistant")

    async def handle_bridge_status_change(self, status: str):
        """
        Handle bridge/gateway status changes.
        Notifies frontend of MQTT bridge status (online/offline).
        """
        await self._emit("bridge_status", {"status": status})
        logger.info(f"Bridge status changed: {status}")

    async def announce_all_devices(self):
        """
        Announce ALL devices to Home Assistant on startup.
        Called after Zigbee network has fully started and MQTT is connected.
        """
        if not self.mqtt:
            logger.warning("Cannot announce devices - MQTT not available")
            return

        await asyncio.sleep(1)
        logger.info(f"📢 Announcing {len(self.devices)} devices to Home Assistant...")

        announced = 0
        failed = 0

        for ieee in list(self.devices.keys()):
            try:
                await self.announce_device(ieee)
                announced += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"[{ieee}] Failed to announce: {e}")
                failed += 1

        if hasattr(self, 'group_manager'):
            logger.info("📢 Announcing Groups...")
            await self.group_manager.announce_groups()

        logger.info(f"✅ Device & Group announcement complete: {announced} devices successful")

        # Grace period for HA to sync state before accepting commands
        logger.info("⏳ Startup grace period (20s) - ignoring commands...")
        await asyncio.sleep(20)
        self._accepting_commands = True
        logger.info("✅ Now accepting MQTT commands")

        await self._emit("log", {
            "level": "INFO",
            "message": f"Announced {announced} devices to Home Assistant",
            "ieee": None
        })
