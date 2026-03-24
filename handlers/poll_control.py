"""
Poll Control cluster handler (0x0020).

Sleepy end devices (battery sensors like Philips Hue SML, Aqara motion, etc.)
send periodic Check-in commands via this cluster. The coordinator MUST respond
with a Check-in Response or the device will eventually consider the parent lost
and stop communicating.

This is the root cause of Philips Hue motion sensors silently dropping off
the network after days/weeks — unanswered check-ins.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.poll_control")

# Poll Control Command IDs (Client -> Server, i.e. device -> coordinator)
CHECKIN_CMD = 0x00  # Check-in (device asks "are you still there?")

# Poll Control Command IDs (Server -> Client, i.e. coordinator -> device)
CHECKIN_RESPONSE_CMD = 0x00  # Check-in Response
FAST_POLL_STOP_CMD = 0x01
SET_LONG_POLL_CMD = 0x02
SET_SHORT_POLL_CMD = 0x03


@register_handler(0x0020)
class PollControlHandler(ClusterHandler):
    """
    Handles Poll Control cluster (0x0020).

    Critical for sleepy end devices:
    - Philips Hue SML motion sensors
    - Aqara motion/door sensors
    - Any battery-powered Zigbee end device

    Without responding to check-ins, devices silently leave the network.
    """
    CLUSTER_ID = 0x0020
    REPORT_CONFIG = []  # No reporting needed — this is command-driven

    # Poll Control Attributes
    ATTR_CHECKIN_INTERVAL = 0x0000      # How often device checks in (quarter-seconds)
    ATTR_LONG_POLL_INTERVAL = 0x0001    # Long poll interval (quarter-seconds)
    ATTR_SHORT_POLL_INTERVAL = 0x0002   # Short poll interval (quarter-seconds)
    ATTR_FAST_POLL_TIMEOUT = 0x0003     # Fast poll timeout (quarter-seconds)
    ATTR_CHECKIN_INTERVAL_MIN = 0x0004  # Minimum check-in interval
    ATTR_LONG_POLL_INTERVAL_MIN = 0x0005
    ATTR_FAST_POLL_TIMEOUT_MAX = 0x0006

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._checkin_count = 0
        logger.info(f"[{self.device.ieee}] Poll Control handler initialised")

    def cluster_command(self, tsn: int, command_id: int, args):
        """Handle Poll Control commands from the device."""
        super().cluster_command(tsn, command_id, args)

        if command_id == CHECKIN_CMD:
            self._handle_checkin(tsn)
        else:
            logger.debug(
                f"[{self.device.ieee}] Poll Control command 0x{command_id:02X}, args={args}"
            )

    def _handle_checkin(self, tsn: int):
        """
        Respond to a Check-in from a sleepy end device.

        The Check-in Response tells the device:
        - start_fast_polling: False (no need for fast polling)
        - fast_poll_timeout: 0 (not entering fast poll mode)

        This keeps the device on the network with minimal power consumption.
        """
        self._checkin_count += 1
        self.device.update_last_seen()

        logger.debug(
            f"[{self.device.ieee}] Poll Control Check-in #{self._checkin_count} — responding"
        )

        # Send Check-in Response (command 0x00, client-to-server direction)
        # Args: (start_fast_polling: bool, fast_poll_timeout: uint16)
        try:
            asyncio.create_task(self._send_checkin_response())
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to schedule check-in response: {e}")

    async def _send_checkin_response(self):
        """Send the actual Check-in Response to the device."""
        try:
            # zigpy's PollControl cluster has a checkin_response() client command
            result = await self.cluster.checkin_response(
                start_fast_polling=False,
                fast_poll_timeout=0
            )
            logger.debug(f"[{self.device.ieee}] Check-in response sent: {result}")
        except AttributeError:
            # Fallback: manually construct the response if method unavailable
            try:
                from zigpy.zcl.foundation import GeneralCommand
                result = await self.cluster.command(
                    CHECKIN_RESPONSE_CMD,
                    False,  # start_fast_polling
                    0,      # fast_poll_timeout
                    direction=True  # client-to-server
                )
                logger.debug(f"[{self.device.ieee}] Check-in response sent (fallback): {result}")
            except Exception as e:
                logger.warning(f"[{self.device.ieee}] Check-in response fallback failed: {e}")
        except asyncio.TimeoutError:
            # Device may have already gone back to sleep — this is normal
            logger.debug(f"[{self.device.ieee}] Check-in response timed out (device sleeping)")
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Check-in response failed: {e}")

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """Handle Poll Control attribute reports."""
        if hasattr(value, 'value'):
            value = value.value

        names = {
            self.ATTR_CHECKIN_INTERVAL: "checkin_interval",
            self.ATTR_LONG_POLL_INTERVAL: "long_poll_interval",
            self.ATTR_SHORT_POLL_INTERVAL: "short_poll_interval",
            self.ATTR_FAST_POLL_TIMEOUT: "fast_poll_timeout",
        }

        attr_name = names.get(attrid)
        if attr_name:
            # Values are in quarter-seconds
            seconds = value / 4 if value else 0
            logger.debug(
                f"[{self.device.ieee}] Poll Control {attr_name}: "
                f"{value} quarter-seconds ({seconds}s)"
            )

    async def configure(self):
        """
        Configure Poll Control — bind the cluster.
        We don't configure reporting; check-ins are command-driven.
        """
        try:
            async with asyncio.timeout(5.0):
                await self.cluster.bind()
            logger.info(f"[{self.device.ieee}] Poll Control cluster bound")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[{self.device.ieee}] Poll Control bind timed out")
            return False
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Poll Control bind failed: {e}")
            return False

    def get_discovery_configs(self) -> List[Dict]:
        """No HA discovery needed for Poll Control."""
        return []

    def get_pollable_attributes(self) -> Dict[int, str]:
        """Not pollable — check-ins are device-initiated."""
        return {}