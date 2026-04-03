"""
Per-device polling scheduler.
Extracted from core.py - standalone class (not a mixin).
"""
import asyncio
import logging
from typing import Dict, Optional

logger = logging.getLogger("core.polling")


class PollingScheduler:
    """
    Per-device polling scheduler.
    Manages automatic polling of devices at configurable intervals.
    """

    def __init__(self, zigbee_service):
        self.service = zigbee_service
        self._tasks: Dict[str, asyncio.Task] = {}
        self._intervals: Dict[str, int] = {}  # ieee -> seconds
        self._running = False
        self._default_interval = 0  # 0 disables active polling by default

    def start(self):
        """Start the polling scheduler."""
        self._running = True
        logger.info("Polling scheduler started (Active polling disabled by default)")

    def stop(self):
        """Stop all polling tasks."""
        self._running = False
        for ieee, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
        logger.info("Polling scheduler stopped")

    def set_interval(self, ieee: str, interval: int):
        """Set polling interval for a device. interval=0 disables polling."""
        old = self._intervals.get(ieee, 0)
        self._intervals[ieee] = interval

        # Cancel existing task
        if ieee in self._tasks:
            self._tasks[ieee].cancel()
            del self._tasks[ieee]

        # Start new task if interval > 0
        if interval > 0 and self._running:
            self._tasks[ieee] = asyncio.create_task(self._poll_loop(ieee, interval))
            logger.info(f"[{ieee}] Polling enabled: {interval}s")
        elif old > 0:
            logger.info(f"[{ieee}] Polling disabled")

    async def _poll_loop(self, ieee: str, interval: int):
        """Polling loop for a single device."""
        while True:
            try:
                await asyncio.sleep(interval)

                if ieee not in self.service.devices:
                    break

                device = self.service.devices[ieee]

                # Check power source
                power_source = device.state.get('power_source', 'Unknown')
                is_battery = power_source in ('Battery', 'DC Source') or device.state.get('battery_percentage') is not None

                # Skip passive battery sensors
                is_sensor = any([
                    0x0406 in [h.CLUSTER_ID for h in device.handlers.values()],
                    0x0500 in [h.CLUSTER_ID for h in device.handlers.values()],
                    device.get_role() == "EndDevice" and not any([
                        0x0006 in ep.in_clusters for ep in device.zigpy_dev.endpoints.values()
                    ])
                ])

                if is_battery and is_sensor:
                    logger.debug(f"[{ieee}] Skipping poll - battery sensor")
                    continue

                # Skip covers during movement
                is_cover = 0x0102 in [h.CLUSTER_ID for h in device.handlers.values()]
                if is_cover:
                    cover_state = device.state.get('state', '').lower()
                    if cover_state in ['opening', 'closing']:
                        logger.debug(f"[{ieee}] Skipping poll - cover moving")
                        continue

                # Skip TRVs during active heating
                is_trv = 0x0201 in [h.CLUSTER_ID for h in device.handlers.values()]
                if is_trv and is_battery:
                    pi_heating_demand = device.state.get('pi_heating_demand', 0)
                    if pi_heating_demand > 0:
                        logger.debug(f"[{ieee}] Skipping poll - TRV actively heating")
                        continue

                if device.is_available():
                    logger.debug(f"[{ieee}] Auto-polling device")
                    try:
                        await device.poll()
                    except Exception as e:
                        logger.warning(f"[{ieee}] Poll failed: {e}")
                else:
                    logger.debug(f"[{ieee}] Skipping poll - device unavailable")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{ieee}] Polling error: {e}")
                await asyncio.sleep(30)

    async def _availability_watchdog_loop(self):
        """Periodically check for expired devices."""
        while True:
            await asyncio.sleep(60)
            for ieee, device in self.service.devices.items():
                device.check_availability_change()

    def enable_for_device(self, ieee: str, interval: Optional[int] = None):
        """Enable polling for a device with optional custom interval."""
        if interval is None:
            interval = self._default_interval
        self.set_interval(ieee, interval)

    def disable_for_device(self, ieee: str):
        """Disable polling for a device."""
        self.set_interval(ieee, 0)
