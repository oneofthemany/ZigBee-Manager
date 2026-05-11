"""
Device Discovery Provider Mixin
Handles Home Assistant MQTT discovery configurations and payload formatting.
"""
import json
import asyncio
from typing import Dict, Any, List

class DeviceDiscoveryProviderMixin:

    def _publish_json_state(self, changed_data: Dict[str, Any], endpoint_id: int = None):
        """Helper to format and publish state in JSON format."""
        if not hasattr(self.service, 'mqtt') or not self.service.mqtt:
            return

        caps = self.capabilities

        payload = {
            'available': True,
            'linkquality': getattr(self.zigpy_dev, 'lqi', 0) or 0,
            'last_seen': self.last_seen,
        }

        is_light = caps.has_capability('light')
        is_cover = caps.has_capability('cover')
        is_switch = caps.has_capability('switch')
        is_motion = caps.has_capability('motion_sensor')
        is_contact = caps.has_capability('contact_sensor')

        if is_light:
            state_val = (changed_data.get(f'state_{endpoint_id}') or
                         changed_data.get('state') or
                         self.state.get(f'state_{endpoint_id}') or
                         self.state.get('state'))
            if state_val is not None:
                payload['state'] = state_val.upper() if isinstance(state_val, str) else ('ON' if state_val else 'OFF')

            if caps.has_capability('level_control'):
                bri = (changed_data.get(f'brightness_{endpoint_id}') or
                       changed_data.get('brightness') or
                       self.state.get(f'brightness_{endpoint_id}') or
                       self.state.get('brightness'))
                if bri is not None and isinstance(bri, (int, float)):
                    if bri <= 100 and bri > 1: bri = int(bri * 2.54)
                    payload['brightness'] = min(254, max(0, int(bri)))

            if caps.has_capability('color_control'):
                ct = (changed_data.get('color_temp_mireds') or
                      changed_data.get('color_temp') or
                      self.state.get('color_temp_mireds') or
                      self.state.get('color_temp'))
                if ct: payload['color_temp'] = int(ct)

        elif is_cover:
            position = (changed_data.get('position') or
                        changed_data.get('current_position') or
                        self.state.get('position', 0))
            payload['position'] = int(position) if position is not None else 0

            if payload['position'] == 0: payload['state'] = 'closed'
            else: payload['state'] = 'open'

        if is_contact:
            key = f'contact_{endpoint_id}' if endpoint_id is not None else 'contact'
            raw_contact = changed_data.get(key) if key in changed_data else self.state.get(key)
            if raw_contact is not None:
                ha_contact = not bool(raw_contact)
                payload[key] = ha_contact
                payload['state'] = 'ON' if ha_contact else 'OFF'

        if is_motion:
            occ_val = (changed_data.get('occupancy') or changed_data.get('motion') or changed_data.get('presence') or 
                       self.state.get('occupancy') or self.state.get('motion') or self.state.get('presence'))
            if occ_val is not None:
                payload['occupancy'] = bool(occ_val)
                payload.setdefault('state', 'ON' if occ_val else 'OFF')

        blocked_fields = {f'contact_{endpoint_id}', 'contact'}

        for key in list(changed_data.keys()) + list(self.state.keys()):
            if key in payload or key in blocked_fields: continue
            value = changed_data.get(key)
            if value is None: value = self.state.get(key)
            if value is not None: payload[key] = value

        if payload and len(payload) > 3:
            safe_name = self.service.friendly_names.get(self.ieee, self.ieee)
            topic = f"{self.service.mqtt.base_topic}/{safe_name}"
            asyncio.create_task(self.service.mqtt.publish(topic, json.dumps(payload)))

    def get_device_discovery_configs(self) -> List[Dict]:
        configs = []
        seen_handlers = set()

        device_info = {
            "identifiers": [self.ieee],
            "name": self.state.get("manufacturer", "Zigbee") + " " + self.state.get("model", "Device"),
            "model": self.state.get("model", "Unknown"),
            "manufacturer": self.state.get("manufacturer", "Unknown")
        }

        for handler in self.handlers.values():
            if handler in seen_handlers: continue
            seen_handlers.add(handler)

            if hasattr(handler, 'get_discovery_configs'):
                c = handler.get_discovery_configs()
                if c:
                    for config in c:
                        if "device" not in config: config["device"] = device_info
                        self._apply_json_schema(config)
                    configs.extend(c)

        configs.append({
            "component": "sensor",
            "object_id": "linkquality",
            "unique_id": f"{self.ieee}_linkquality",
            "device": device_info,
            "config": {
                "name": "Link Quality",
                "unit_of_measurement": "lqi",
                "value_template": "{{ value_json.lqi }}",
                "state_class": "measurement",
                "icon": "mdi:signal"
            }
        })
        return configs

    def _apply_json_schema(self, payload: Dict):
        component = payload.get('component')
        if component not in ['light', 'cover']: return
        config = payload.get('config', payload)

        if component == "light":
            if 'schema' not in config: config['schema'] = 'json'
            if config.get('schema') == 'json':
                keys_to_remove = ['payload_on', 'payload_off', 'value_template', 'brightness_state_topic', 'brightness_command_topic', 'brightness_value_template', 'brightness_command_template', 'color_temp_state_topic', 'color_temp_command_topic', 'color_temp_value_template', 'color_temp_command_template']
                for key in keys_to_remove: config.pop(key, None)
            if 'command_topic' not in config and 'state_topic' in config:
                config['command_topic'] = config['state_topic'] + "/set"

        elif component == "cover":
            if 'payload_open' not in config: config['payload_open'] = '{"command": "open"}'
            if 'payload_close' not in config: config['payload_close'] = '{"command": "close"}'
            if 'payload_stop' not in config: config['payload_stop'] = '{"command": "stop"}'
            if 'set_position_template' not in config: config['set_position_template'] = '{"command": "position", "value": {{ position }}}'
            if 'value_template' not in config: config['value_template'] = "{{ 'open' if value_json.is_open else 'closed' }}"
            if 'position_template' not in config: config['position_template'] = "{{ value_json.cover_position | default(value_json.position | default(0)) }}"
