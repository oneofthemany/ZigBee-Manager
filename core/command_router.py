"""
Command Router Mixin
Handles routing of API and MQTT commands to the appropriate device.
"""
import logging
import re
import time
import traceback
from typing import Dict, Any, Optional

logger = logging.getLogger("core.command_router")

class CommandRouterMixin:

    async def send_command(self, ieee: str, command: str, value=None, endpoint_id=None):
        if ieee not in self.devices:
            return {"success": False, "error": "Device not found"}
        try:
            device = self.devices[ieee]
            device._last_write_ts = time.time()
            result = await device.send_command(command, value, endpoint_id)
            if result is False:
                if not getattr(device, 'handlers', None):
                    return {"success": False, "error": "Device initialising"}
                return {"success": False, "error": "Device rejected command"}
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"[{ieee}] Command failed: {e}")
            if type(e).__name__ == "NcpFailure":
                if hasattr(self, 'resilience'):
                    await self.resilience.handle_ncp_failure(e)
            return {"success": False, "error": str(e)}

    async def handle_mqtt_command(self, device_identifier: str, data: Dict[str, Any], component: Optional[str] = None, object_id: Optional[str] = None):
        if not getattr(self, '_accepting_commands', True):
            logger.warning(f"Ignoring command during startup: {device_identifier}")
            return

        ieee = self._resolve_device_identifier(device_identifier)
        if not ieee or ieee not in self.devices:
            logger.warning(f"MQTT command for unknown device: {device_identifier}")
            return

        device = self.devices[ieee]
        logger.info(f"[{ieee}] MQTT command: {data}")

        try:
            endpoint = None
            if object_id:
                match = re.search(r'_(\d+)$', object_id)
                endpoint = int(match.group(1)) if match else None

            if endpoint is None and device.capabilities.has_capability('light'):
                for ep_id in device.zigpy_dev.endpoints:
                    if ep_id == 0: continue
                    ep = device.zigpy_dev.endpoints[ep_id]
                    if 0x0008 in ep.in_clusters or 0x0006 in ep.in_clusters:
                        endpoint = ep_id
                        break

            optimistic_state = {}
            state = data.get('state')
            brightness = data.get('brightness')
            color_temp = data.get('color_temp')
            color = data.get('color')

            if state:
                cmd = 'on' if str(state).upper() == 'ON' else 'off'
                result = await device.send_command(cmd, endpoint_id=endpoint, data=data)
                if result:
                    optimistic_state['state'] = state.upper() if isinstance(state, str) else ('ON' if state else 'OFF')
                    optimistic_state['on'] = (cmd == 'on')

            if brightness is not None:
                pct = int(brightness / 2.54)
                result = await device.send_command('brightness', pct, endpoint_id=endpoint)
                if result:
                    optimistic_state['brightness'] = int(brightness)
                    optimistic_state['level'] = pct
                    if brightness > 0: optimistic_state['state'] = 'ON'; optimistic_state['on'] = True

            if color_temp is not None:
                try:
                    kelvin = int(1000000 / color_temp)
                    result = await device.send_command('color_temp', kelvin, endpoint_id=endpoint)
                    if result: optimistic_state['color_temp'] = int(color_temp)
                except ZeroDivisionError: pass

            if color and 'x' in color and 'y' in color:
                result = await device.send_command('xy_color', (color['x'], color['y']), endpoint_id=endpoint)
                if result: optimistic_state['color'] = color

            position = data.get('position')
            tilt = data.get('tilt')
            if position is not None: await device.send_command('position', position, endpoint_id=endpoint)
            if tilt is not None: await device.send_command('tilt', tilt, endpoint_id=endpoint)

            temperature = data.get('temperature')
            if temperature is not None: await device.send_command('temperature', temperature, endpoint_id=endpoint)

            mode = data.get('mode') or data.get('preset_mode')
            if mode is not None: await device.send_command('mode', mode, endpoint_id=endpoint)

            if optimistic_state:
                self.handle_device_update(device, optimistic_state, endpoint_id=endpoint)

        except Exception as e:
            logger.error(f"[{ieee}] MQTT command failed: {e}")
            traceback.print_exc()
            if type(e).__name__ == "NcpFailure" and hasattr(self, 'resilience'):
                await self.resilience.handle_ncp_failure(e)
