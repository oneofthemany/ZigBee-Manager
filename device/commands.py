"""
Device Command Executor Mixin
Handles command routing, execution, and optimistic state updates.
"""
import logging
import asyncio
from typing import Dict, Any, List, Optional
from modules.error_handler import with_retries
from modules.packet_stats import packet_stats

logger = logging.getLogger("device.commands")

class DeviceCommandExecutorMixin:

    def get_control_commands(self) -> List[Dict[str, Any]]:
        """Get available control commands."""
        commands = []
        seen = set()
        for h in self.handlers.values():
            if h in seen: continue
            seen.add(h)

            eid = h.endpoint.endpoint_id
            if h.CLUSTER_ID == 0x0006:
                commands.extend([
                    {"command": "on", "label": "On", "endpoint_id": eid},
                    {"command": "off", "label": "Off", "endpoint_id": eid},
                    {"command": "toggle", "label": "Toggle", "endpoint_id": eid}
                ])
            elif h.CLUSTER_ID == 0x0008:
                commands.append({"command": "brightness", "label": "Brightness", "type": "slider", "min": 0, "max": 100, "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0300:
                commands.append({"command": "color_temp", "label": "Color Temp", "type": "slider", "min": 2000, "max": 6500, "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0201:
                commands.append({"command": "temperature", "label": "Temp Setpoint", "type": "number", "unit": "C", "endpoint_id": eid})
            elif h.CLUSTER_ID == 0x0102:
                commands.extend([
                    {"command": "open", "label": "Open", "endpoint_id": eid},
                    {"command": "close", "label": "Close", "endpoint_id": eid},
                    {"command": "stop", "label": "Stop", "endpoint_id": eid},
                    {"command": "position", "label": "Position", "type": "slider", "min": 0, "max": 100, "endpoint_id": eid}
                ])
        return commands

    @with_retries(max_retries=3, backoff_base=1.5, timeout=10.0)
    async def send_command(self, command: str, value=None, endpoint_id=None, data: Optional[Dict] = None):
        """Execute command on device."""
        try:
            packet_stats.record_tx(self.ieee)
            try:
                from modules.packet_flow import get_flow_analyzer
                get_flow_analyzer().record(self.ieee, None, direction="TX")
            except Exception: pass
            
            try:
                from modules.zigbee_debug import get_debugger
                _dbg = get_debugger()
                if _dbg: _dbg.capture_tx_command(ieee=self.ieee, command=command, value=value, endpoint_id=endpoint_id)
            except Exception: pass
            
            logger.info(f"[{self.ieee}] CMD: {command}={value} EP={endpoint_id}")
            command = command.lower()

            if not self.handlers:
                logger.debug(f"[{self.ieee}] Rejecting {command} — device still initialising")
                return False

            if value is not None and isinstance(value, str):
                if value.replace('.', '').replace('-', '').isdigit():
                    value = float(value) if '.' in value else int(value)

            def get_handler(cid):
                if endpoint_id:
                    if (endpoint_id, cid) in self.handlers: return self.handlers[(endpoint_id, cid)]
                return self.handlers.get(cid)

            has_cap = getattr(self.capabilities, 'has_capability', lambda x: False)
            optimistic_state = {}
            success = False

            if has_cap('light') or has_cap('switch'):
                if command in ['on', 'off', 'toggle']:
                    h = get_handler(0x0006)
                    if h:
                        if command == 'on':
                            await h.turn_on()
                            optimistic_state['state'] = 'ON'
                            optimistic_state['on'] = True
                        elif command == 'off':
                            transition = data.get('transition') if data else None
                            transition_time = int(transition * 10) if transition else None
                            await h.turn_off(transition_time=transition_time)
                            optimistic_state['state'] = 'OFF'
                            optimistic_state['on'] = False
                        else:
                            await h.toggle()
                            current = self.state.get('on', False)
                            optimistic_state['state'] = 'OFF' if current else 'ON'
                            optimistic_state['on'] = not current
                        success = True

                elif command == 'brightness' and value is not None:
                    h = get_handler(0x0008)
                    if h:
                        await h.set_brightness_pct(int(value))
                        optimistic_state['brightness'] = int(value * 2.54) if value <= 100 else int(value)
                        optimistic_state['level'] = int(value) if value <= 100 else int(value / 2.54)
                        if value > 0:
                            optimistic_state['state'] = 'ON'
                            optimistic_state['on'] = True
                        success = True

                elif command == 'color_temp' and value is not None:
                    h = get_handler(0x0300)
                    if h:
                        await h.set_color_temp_kelvin(int(value))
                        mireds = int(1000000 / value) if value > 0 else 250
                        optimistic_state['color_temp'] = mireds
                        optimistic_state['color_temp_mireds'] = mireds
                        success = True

                elif command == 'xy_color' and value is not None:
                    h = get_handler(0x0300)
                    if h:
                        x, y = value if isinstance(value, (list, tuple)) else (0.5, 0.5)
                        if isinstance(x, float) and x <= 1.0: x = int(x * 65535)
                        if isinstance(y, float) and y <= 1.0: y = int(y * 65535)
                        await h.set_xy_color(int(x), int(y))
                        optimistic_state['color_x'] = x
                        optimistic_state['color_y'] = y
                        optimistic_state['color_mode'] = 'xy'
                        success = True

                elif command == 'hs_color' and value is not None:
                    h = get_handler(0x0300)
                    if h:
                        hue, sat = value if isinstance(value, (list, tuple)) else (0, 100)
                        zcl_hue = int((hue / 360) * 254)
                        zcl_sat = int((sat / 100) * 254)
                        await h.set_hue_sat(zcl_hue, zcl_sat)
                        optimistic_state['hue'] = zcl_hue
                        optimistic_state['saturation'] = zcl_sat
                        optimistic_state['color_mode'] = 'hs'
                        success = True

            aqara_commands = ['window_detection', 'valve_detection', 'motor_calibration', 'child_lock', 'external_temp', 'sensor_type', 'system_mode', 'preset', 'away_preset_temperature', 'schedule']
            if not success and command in aqara_commands:
                h = get_handler(0xFCC0)
                if h and hasattr(h, 'process_command'):
                    h.process_command(command, value)
                    success = True
                    if command == 'motor_calibration': optimistic_state['calibration_status'] = 'in_progress'
                    elif command == 'system_mode':
                        sv = str(value).lower()
                        optimistic_state['system_mode'] = 'heat' if sv in ('1', 'heat', 'on', 'true') else 'off'
                    elif command == 'preset':
                        sv = str(value).lower()
                        if sv in ('manual', 'away', 'auto'): optimistic_state['preset'] = sv
                    elif command == 'external_temp':
                        try: optimistic_state['external_temperature'] = float(value)
                        except (TypeError, ValueError): pass
                    elif command == 'sensor_type':
                        sv = str(value).lower()
                        optimistic_state['sensor_type'] = 'external' if sv in ('1', 'external', 'true', 'on') else 'internal'
                    elif command == 'away_preset_temperature':
                        try: optimistic_state['away_preset_temperature'] = float(value)
                        except (TypeError, ValueError): pass
                    elif command == 'schedule': optimistic_state['schedule_enabled'] = bool(value)
                    else: optimistic_state[command] = bool(value)

            if not success and command in ['temperature', 'system_mode']:
                h = get_handler(0x0201)
                if h:
                    if hasattr(h, 'process_command'):
                        h.process_command(command, value)
                        success = True
                        if command == 'temperature':
                            optimistic_state['temperature_setpoint'] = float(value)
                            optimistic_state['occupied_heating_setpoint'] = float(value)
                            optimistic_state['heating_setpoint'] = float(value)
                            optimistic_state['target_temp'] = float(value)
                        elif command == 'system_mode':
                            mode = str(value).lower()
                            optimistic_state['system_mode'] = mode
                            if mode == 'off':
                                optimistic_state['running_state'] = 0
                                optimistic_state['hvac_action'] = 'off'
                    elif command == 'temperature':
                        await h.set_heating_setpoint(float(value))
                        optimistic_state['temperature_setpoint'] = float(value)
                        optimistic_state['occupied_heating_setpoint'] = float(value)
                        success = True

            elif not success and command == 'identify':
                h = get_handler(0x0003)
                if h:
                    await h.identify(5)
                    success = True

            if not success and (has_cap('cover') or command in ['open', 'close', 'stop', 'position']):
                h = get_handler(0x0102)
                if h:
                    if command == 'open':
                        await h.open()
                        optimistic_state['position'] = 100
                        optimistic_state['state'] = 'open'
                    elif command == 'close':
                        await h.close()
                        optimistic_state['position'] = 0
                        optimistic_state['state'] = 'closed'
                    elif command == 'stop':
                        await h.stop()
                    elif command == 'position' and value is not None:
                        await h.set_position(int(value))
                        optimistic_state['position'] = int(value)
                    success = True

            if success and optimistic_state:
                logger.info(f"[{self.ieee}] Optimistic update: {optimistic_state}")
                self.update_state(optimistic_state, endpoint_id=endpoint_id)

            if success and command in ['temperature', 'system_mode']:
                is_sleepy = False
                try:
                    nd = getattr(self.zigpy_dev, 'node_desc', None)
                    if nd:
                        is_end = int(nd.logical_type) == 2
                        rx_on_when_idle = bool(int(nd.mac_capability_flags) & 0x08)
                        is_sleepy = is_end and not rx_on_when_idle
                except Exception: pass
                
                if not is_sleepy:
                    async def _delayed_poll():
                        await asyncio.sleep(5)
                        try: await self.poll()
                        except Exception: pass
                    asyncio.create_task(_delayed_poll())

            return success

        except Exception as e:
            packet_stats.record_error(self.ieee)
            raise

    async def read_attribute_raw(self, ep_id: int, cluster_id: int, attr_name: str) -> Any:
        ep = self.zigpy_dev.endpoints.get(ep_id)
        if not ep: raise ValueError(f"EP {ep_id} not found")
        in_cl = getattr(ep, 'in_clusters', None) or {}
        out_cl = getattr(ep, 'out_clusters', None) or {}
        c = in_cl.get(cluster_id) or out_cl.get(cluster_id)
        if not c: raise ValueError(f"Cluster 0x{cluster_id:04x} not found")
        res = await c.read_attributes([attr_name])
        return res[0][attr_name] if res and attr_name in res[0] else None

    def handle_raw_message(self, cluster_id: int, message: bytes):
        h = self.handlers.get(cluster_id)
        if h and hasattr(h, 'handle_raw_data'): h.handle_raw_data(message)
