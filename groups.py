"""
Zigbee Groups Management Module
Based on ZHA and Zigbee2MQTT patterns for native Zigbee groups

This module provides:
- Smart device grouping (only compatible devices)
- Common capability detection (brightness, color, etc.)
- Native Zigbee group creation
- Home Assistant MQTT discovery for groups
"""

import logging
import json
from typing import Dict, List, Set, Optional, Any
from pathlib import Path
import zigpy.types as t
import asyncio
import os

logger = logging.getLogger(__name__)
os.makedirs("groups", exist_ok=True)

# Groups storage file
GROUPS_FILE = Path("./groups/groups.json")


class DeviceCapability:
    """Device capabilities for smart grouping"""
    ON_OFF = "on_off"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    COLOR_XY = "color_xy"
    COLOR_HS = "color_hs"
    POSITION = "position"  # For covers
    LOCK = "lock"


class GroupManager:
    """
    Manages Zigbee groups with smart device compatibility
    """

    def __init__(self, zigbee_service):
        self.service = zigbee_service
        self.groups: Dict[int, Dict] = {}  # group_id -> group_info
        self.next_group_id = 1

        # Device type compatibility matrix
        self.compatible_types = {
            "light": ["Router"],  # Lights must be routers
            "switch": ["Router", "EndDevice"],
            "cover": ["Router", "EndDevice"],
            "lock": ["EndDevice"],
        }

        self.load_groups()

    def _get_friendly_name(self, device_or_ieee) -> str:
        """
        Get friendly name for a device or IEEE address

        Args:
            device_or_ieee: Either a device object or IEEE string

        Returns:
            Friendly name or IEEE if not found
        """
        # If it's a string (IEEE), use it directly
        if isinstance(device_or_ieee, str):
            ieee = device_or_ieee
        # If it's a device object, get its IEEE
        elif hasattr(device_or_ieee, 'ieee'):
            ieee = str(device_or_ieee.ieee)
        else:
            return "Unknown"

        # Get friendly name from service
        if hasattr(self.service, 'friendly_names'):
            return self.service.friendly_names.get(ieee, ieee)

        return ieee

    def load_groups(self):
        """Load groups from persistent storage"""
        try:
            if GROUPS_FILE.exists():
                with open(GROUPS_FILE, 'r') as f:
                    data = json.load(f)
                    self.groups = {int(k): v for k, v in data.get('groups', {}).items()}
                    self.next_group_id = data.get('next_id', 1)
                    logger.info(f"Loaded {len(self.groups)} groups from storage")

        except Exception as e:
            logger.error(f"Failed to load groups: {e}")
            self.groups = {}
            self.next_group_id = 1

    def save_groups(self):
        """Save groups to persistent storage"""
        try:
            GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(GROUPS_FILE, 'w') as f:
                json.dump({
                    'groups': self.groups,
                    'next_id': self.next_group_id
                }, f, indent=2)
            logger.info(f"Saved {len(self.groups)} groups to storage")
        except Exception as e:
            logger.error(f"Failed to save groups: {e}")


    def _get_group_id_by_name(self, name: str) -> Optional[int]:
        """Find group ID by name (case-insensitive, space-tolerant for MQTT topic resolution)"""
        # MQTT topics use a safe name format (lowercase, underscores)
        safe_name = name.replace('_', ' ').lower()

        for group_id, group in self.groups.items():
            current_safe_name = group['name'].replace(' ', ' ').lower()
            if current_safe_name == safe_name:
                return group_id
        return None

    async def handle_mqtt_group_command(self, group_name: str, data: Dict[str, Any]):
        """
        Wrapper to handle incoming MQTT group commands.
        Resolves group name (from topic) to ID and calls the control method.
        """
        # The MQTT topic contains the safe name (e.g., "living_room_lights")
        group_id = self._get_group_id_by_name(group_name)

        if group_id is None:
            logger.warning(f"MQTT Group Command received for unknown group name/ID: {group_name}")
            return {"error": f"Group name '{group_name}' not found"}

        logger.info(f"Handling MQTT command for group ID {group_id} ({group_name}): {data}")

        # The core logic is already in control_group
        return await self.control_group(group_id, data)

    def get_device_type(self, device) -> Optional[str]:
        """
        Determine device type from discovery configs or capabilities
        Returns: 'light', 'switch', 'cover', or 'lock'
        """
        # 1. Try discovery configs first (if available)
        if hasattr(device, 'get_discovery_configs'):
            configs = device.get_discovery_configs()
            for config in configs:
                component = config.get('component')
                if component in ['light', 'switch', 'cover', 'lock']:
                    return component

        # 2. Fallback: Smart detection based on capabilities
        caps = self.get_device_capabilities(device)

        # If it has brightness or color, it's definitely a light
        if DeviceCapability.BRIGHTNESS in caps or \
                DeviceCapability.COLOR_XY in caps or \
                DeviceCapability.COLOR_TEMP in caps:
            return "light"

        # Covers
        if DeviceCapability.POSITION in caps:
            return "cover"

        # Locks
        if DeviceCapability.LOCK in caps:
            return "lock"

        # On/Off devices could be switches or simple lights
        if DeviceCapability.ON_OFF in caps:
            # Check model name for hints
            model = getattr(device, 'model', '').lower() if hasattr(device, 'model') else ''
            if any(x in model for x in ['light', 'bulb', 'lamp', 'spot', 'led']):
                return "light"
            return "switch" # Default to switch if unknown

        return None

    def get_device_capabilities(self, device) -> Set[str]:
        """Detect what capabilities a device has"""
        capabilities = set()

        # Helper to check clusters in handlers OR raw endpoints
        def has_cluster(cluster_id):
            # Check handlers
            if hasattr(device, 'handlers'):
                if any(h.cluster_id == cluster_id for h in device.handlers.values()):
                    return True
            # Check raw endpoints
            if hasattr(device, 'zigpy_dev'):
                for ep in device.zigpy_dev.endpoints.values():
                    if hasattr(ep, 'in_clusters') and cluster_id in ep.in_clusters:
                        return True
            return False

        if has_cluster(0x0006): capabilities.add(DeviceCapability.ON_OFF)
        if has_cluster(0x0008): capabilities.add(DeviceCapability.BRIGHTNESS)
        if has_cluster(0x0300):
            capabilities.add(DeviceCapability.COLOR_TEMP)
            capabilities.add(DeviceCapability.COLOR_XY)
        if has_cluster(0x0102): capabilities.add(DeviceCapability.POSITION)
        if has_cluster(0x0101): capabilities.add(DeviceCapability.LOCK)

        return capabilities

    def _get_device_role(self, device) -> str:
        """Helper to safely get device role (Router/EndDevice/Coordinator)"""
        if hasattr(device, 'get_role'):
            return device.get_role()
        return getattr(device, 'type', 'Unknown')

    def are_devices_compatible(self, device1, device2) -> tuple[bool, str]:
        type1 = self.get_device_type(device1)
        type2 = self.get_device_type(device2)

        if not type1 or not type2:
            return False, "Unknown device type"

        if type1 != type2:
            # Allow mixing lights and switches
            if type1 in ['light', 'switch'] and type2 in ['light', 'switch']:
                pass # This is compatible
            else:
                return False, f"Different device types: {type1} vs {type2}"

        # Get Zigbee device roles (Router/EndDevice)
        zigbee_type1 = self._get_device_role(device1)
        zigbee_type2 = self._get_device_role(device2)

        # Check compatibility matrix
        # Use type1 if they are the same, otherwise default to switch rules for mixed
        check_type = type1 if type1 == type2 else "switch"
        allowed_types = self.compatible_types.get(check_type, [])

        # Warning only for role mismatch
        if zigbee_type1 not in allowed_types or zigbee_type2 not in allowed_types:
             pass

        return True, "Compatible"

    def get_common_capabilities(self, devices: List) -> Set[str]:
        """
        Find capabilities common to all devices in list
        """
        if not devices:
            return set()

        common = self.get_device_capabilities(devices[0])
        for device in devices[1:]:
            common &= self.get_device_capabilities(device)

        return common

    async def create_group(self, name: str, device_iees: List[str]) -> Dict:
        """
        Create a new Zigbee group

        Args:
            name: Human-readable group name
            device_iees: List of IEEE addresses to add

        Returns:
            Dict with group info or error
        """
        name = name.strip()

        # --- Check for duplicate name ---
        for group in self.groups.values():
            if group['name'].lower() == name.lower():
                return {"error": f"Group name '{name}' already exists"}

        # Validate we have at least 2 devices
        if len(device_iees) < 2:
            return {"error": "Groups require at least 2 devices"}

        # Get device objects
        devices = []
        for ieee in device_iees:
            device = self.service.devices.get(ieee)
            if not device:
                return {"error": f"Device {ieee} not found"}
            devices.append(device)

        # Check all devices are compatible
        base_device = devices[0]
        base_type = self.get_device_type(base_device)

        # Check compatibility
        for device in devices[1:]:
            compatible, reason = self.are_devices_compatible(base_device, device)
            if not compatible:
                return {"error": f"Device {device.ieee} incompatible: {reason}"}

        # Determine capabilities
        capabilities = self.get_common_capabilities(devices)
        if not capabilities:
            # Fallback: If no common capabilities detected but types match, assume On/Off
            if base_type in ['light', 'switch']:
                capabilities = {DeviceCapability.ON_OFF}
            else:
                return {"error": "Devices have no common capabilities"}

        # Allocate group ID
        group_id = self.next_group_id
        self.next_group_id += 1

        # Create group info
        group_info = {
            "id": group_id,
            "name": name,
            "type": base_type,
            "capabilities": list(capabilities),
            "members": device_iees,
            "created_at": None
        }

        # Add devices to Zigbee group
        try:
            await self._add_devices_to_zigbee_group(group_id, devices)
        except Exception as e:
            logger.error(f"Failed to create Zigbee group: {e}")
            return {"error": f"Failed to create Zigbee group: {str(e)}"}

        # Store group
        self.groups[group_id] = group_info
        self.save_groups()

        # Publish to Home Assistant
        await self._publish_group_discovery(group_id, group_info)

        await self.publish_group_initial_state(group_id)

        logger.info(f"Created group {group_id} '{name}' with {len(devices)} devices")

        return {"success": True, "group": group_info}

    async def _add_devices_to_zigbee_group(self, group_id: int, devices: List):
        """
        Add devices to native Zigbee group
        Uses Groups cluster (0x0004)
        """
        for device in devices:
            try:
                # Find the appropriate endpoint with Groups cluster
                endpoint = None

                # Check handlers first for cleaner lookups
                for handler in device.handlers.values():
                    if handler.cluster_id == 0x0004: # Groups Cluster
                        # We found a handler, get its endpoint
                        # handler keys are often (ep_id, cluster_id) or just cluster_id
                        # Let's rely on the handler's internal cluster object
                        await handler.cluster.add(group_id, f"Group {group_id}")
                        logger.info(f"Added {device.ieee} to Zigbee group {group_id} via Handler")
                        endpoint = True # Mark as done
                        break

                if endpoint:
                    continue

                # Fallback: Search raw endpoints if no handler wrapper exists
                for ep_id, ep in device.zigpy_dev.endpoints.items():
                    if ep_id == 0: continue
                    if 0x0004 in ep.in_clusters:
                        groups_cluster = ep.in_clusters[0x0004]
                        await groups_cluster.add(group_id, f"Group {group_id}")
                        logger.info(f"Added {device.ieee} to Zigbee group {group_id} via Raw Endpoint {ep_id}")
                        endpoint = True
                        break

                if not endpoint:
                    logger.warning(f"Device {device.ieee} has no Groups cluster")
                    continue

            except Exception as e:
                logger.error(f"Failed to add {device.ieee} to group {group_id}: {e}")
                # We raise to stop the process or continue?
                # Better to log and continue so partial groups can be fixed later
                pass

    async def remove_group(self, group_id: int) -> Dict:
        """Remove a group and clean up"""
        if group_id not in self.groups:
            return {"error": "Group not found"}

        group = self.groups[group_id]

        # Remove devices from Zigbee group
        for ieee in group['members']:
            device = self.service.devices.get(ieee)
            if device:
                try:
                    await self._remove_device_from_zigbee_group(group_id, device)
                except Exception as e:
                    logger.error(f"Failed to remove {ieee} from group: {e}")

        # Remove from storage
        del self.groups[group_id]
        self.save_groups()

        # Remove from Home Assistant
        await self._unpublish_group_discovery(group_id, group['name'])

        logger.info(f"Removed group {group_id}")
        return {"success": True}

    async def _remove_device_from_zigbee_group(self, group_id: int, device):
        """Remove device from Zigbee group"""
        # Try handlers first
        done = False
        for handler in device.handlers.values():
            if handler.cluster_id == 0x0004:
                await handler.cluster.remove(group_id)
                done = True
                break

        if done: return

        # Fallback to raw endpoints
        for ep_id, ep in device.zigpy_dev.endpoints.items():
            if ep_id == 0: continue
            if 0x0004 in ep.in_clusters:
                groups_cluster = ep.in_clusters[0x0004]
                await groups_cluster.remove(group_id)
                break

    async def add_device_to_group(self, group_id: int, ieee: str) -> Dict:
        """Add a device to existing group"""
        if group_id not in self.groups:
            return {"error": "Group not found"}

        group = self.groups[group_id]
        device = self.service.devices.get(ieee)

        if not device:
            return {"error": "Device not found"}

        if ieee in group['members']:
            return {"error": "Device already in group"}

        # Check compatibility with existing members
        if group['members']:
            existing_device = self.service.devices.get(group['members'][0])
            compatible, reason = self.are_devices_compatible(existing_device, device)
            if not compatible:
                return {"error": f"Device incompatible: {reason}"}

        # Add to Zigbee group
        try:
            await self._add_devices_to_zigbee_group(group_id, [device])
        except Exception as e:
            return {"error": f"Failed to add device: {str(e)}"}

        # Update group
        group['members'].append(ieee)

        # Recalculate common capabilities
        devices = [self.service.devices.get(m) for m in group['members']]
        group['capabilities'] = list(self.get_common_capabilities(devices))

        self.save_groups()

        # Update Home Assistant discovery
        await self._publish_group_discovery(group_id, group)

        return {"success": True, "group": group}

    async def remove_device_from_group(self, group_id: int, ieee: str) -> Dict:
        """Remove device from group"""
        if group_id not in self.groups:
            return {"error": "Group not found"}

        group = self.groups[group_id]

        if ieee not in group['members']:
            return {"error": "Device not in group"}

        # Remove from Zigbee group
        device = self.service.devices.get(ieee)
        if device:
            try:
                await self._remove_device_from_zigbee_group(group_id, device)
            except Exception as e:
                logger.error(f"Failed to remove device: {e}")

        # Update group
        group['members'].remove(ieee)

        # If less than 2 members, delete group
        if len(group['members']) < 2:
            return await self.remove_group(group_id)

        # Recalculate capabilities
        devices = [self.service.devices.get(m) for m in group['members']]
        group['capabilities'] = list(self.get_common_capabilities(devices))

        self.save_groups()
        await self._publish_group_discovery(group_id, group)

        return {"success": True, "group": group}


    async def _publish_group_state(self, group_id: int, state: Dict[str, Any]):
        """
        Args:
            group_id: The group ID
            state: Command that was executed (contains state, brightness, etc.)
        """
        if not hasattr(self.service, 'mqtt') or not self.service.mqtt:
            logger.warning(f"Cannot publish group {group_id} state - MQTT not available")
            return

        group = self.groups.get(group_id)
        if not group:
            logger.warning(f"Cannot publish state - group {group_id} not found")
            return

        base_topic = self.service.mqtt.base_topic
        safe_name = group['name'].replace(' ', '_').lower()
        topic = f"{base_topic}/group/{safe_name}"

        payload = {"available": True}

        if 'state' in state:
            payload["state"] = state["state"]
        elif 'brightness' in state:
            payload["state"] = "ON"

        if "brightness" in state:
            payload["brightness"] = int(state["brightness"])

        if "color_temp" in state:
            payload["color_temp"] = int(state["color_temp"])

        if "color" in state:
            payload["color"] = state["color"]

        try:
            await self.service.mqtt.client.publish(
                topic,
                json.dumps(payload),
                qos=1,
                retain=True
            )
            logger.info(f"📤 Published group {group_id} state: {payload}")
        except Exception as e:
            logger.error(f"Failed to publish group state: {e}")

    async def _read_group_state(self, group_id: int) -> Dict[str, Any]:
        """
        Read actual state from group member devices.
        """
        if group_id not in self.groups:
            return {}

        group = self.groups[group_id]

        # Try to read from first available device
        for ieee in group['members']:
            device = self.service.devices.get(ieee)
            if not device:
                continue

            zdev = getattr(device, 'zigpy_dev', None)
            if not zdev:
                continue

            state = {}

            try:
                # Find OnOff cluster
                for endpoint_id, endpoint in zdev.endpoints.items():
                    if endpoint_id == 0:
                        continue

                    # Read ON/OFF state
                    if 0x0006 in endpoint.in_clusters:
                        on_off_cluster = endpoint.in_clusters[0x0006]
                        result = await on_off_cluster.read_attributes([0x0000])  # OnOff attribute
                        if 0 in result[0]:
                            state['state'] = "ON" if result[0][0] else "OFF"

                    # Read brightness
                    if 0x0008 in endpoint.in_clusters:
                        level_cluster = endpoint.in_clusters[0x0008]
                        result = await level_cluster.read_attributes([0x0000])  # CurrentLevel
                        if 0 in result[0]:
                            state['brightness'] = result[0][0]

                    # Read color temp
                    if 0x0300 in endpoint.in_clusters:
                        color_cluster = endpoint.in_clusters[0x0300]
                        result = await color_cluster.read_attributes([0x0007])  # ColorTemperatureMireds
                        if 0 in result[0]:
                            state['color_temp'] = result[0][0]

                    # If we got some state, return it
                    if state:
                        logger.debug(f"Read state from {ieee}: {state}")
                        return state

            except Exception as e:
                logger.debug(f"Failed to read state from {ieee}: {e}")
                continue

        # If we couldn't read from any device, return default
        return {"state": "OFF", "available": True}


    async def publish_group_initial_state(self, group_id: int):
        """
        Publish initial group state after creation.
        Reads actual state from devices and publishes to MQTT.
        """
        state = await self._read_group_state(group_id)
        if state:
            await self._publish_group_state(group_id, state)


    async def control_group(self, group_id: int, command: Dict) -> Dict:
        """
        Control all devices in a group using Direct Cluster Commands.
        """
        if group_id not in self.groups:
            return {"error": "Group not found"}

        group = self.groups[group_id]
        results = []

        logger.info(f"🎮 Controlling group {group_id} '{group['name']}' - CMD: {command}")

        for ieee in group['members']:
            result = {"ieee": ieee, "success": False}
            try:
                device = self.service.devices.get(ieee)
                if not device:
                    result["error"] = "Device not found"
                    results.append(result)
                    continue

                zdev = getattr(device, 'zigpy_dev', None)
                if not zdev:
                    zdev = device if hasattr(device, 'endpoints') else None

                if not zdev:
                    result["error"] = "Invalid device object"
                    results.append(result)
                    continue

                def get_cluster(cluster_id):
                    for endpoint_id, endpoint in zdev.endpoints.items():
                        if endpoint_id == 0: continue
                        if cluster_id in endpoint.in_clusters:
                            return endpoint.in_clusters[cluster_id]
                    return None

                # 1. ON / OFF
                if 'state' in command:
                    on_off = get_cluster(0x0006)
                    level_ctrl = get_cluster(0x0008)
                    transition = command.get('transition')

                    if on_off:
                        state = command['state'].upper()
                        if state == 'ON':
                            await on_off.on()
                        else:
                            # OFF with transition via LevelControl if available
                            if transition and level_ctrl:
                                transition_time = int(transition * 10)
                                await level_ctrl.move_to_level_with_on_off(0, transition_time)
                                logger.info(f"[{ieee}] Group OFF with transition: {transition}s")
                            else:
                                await on_off.off()
                        result["success"] = True

                # 2. BRIGHTNESS
                if 'brightness' in command:
                    level_ctrl = get_cluster(0x0008)
                    if level_ctrl:
                        val = int(command['brightness'])
                        await level_ctrl.move_to_level_with_on_off(val, transition_time=10)
                        result["success"] = True

                # 3. COLOR TEMP
                if 'color_temp' in command:
                    color_ctrl = get_cluster(0x0300)
                    if color_ctrl:
                        mireds = int(command['color_temp'])
                        await color_ctrl.move_to_color_temperature(mireds, transition_time=10)
                        result["success"] = True

                # 4. COVERS
                if 'cover_state' in command:
                    cover_ctrl = get_cluster(0x0102)
                    if cover_ctrl:
                        action = command['cover_state'].upper()
                        if action == 'OPEN':
                            await cover_ctrl.up_open()
                        elif action == 'CLOSE':
                            await cover_ctrl.down_close()
                        elif action == 'STOP':
                            await cover_ctrl.stop()
                        result["success"] = True

                if 'position' in command:
                    cover_ctrl = get_cluster(0x0102)
                    if cover_ctrl:
                        pos = int(command['position'])
                        await cover_ctrl.go_to_lift_percentage(pos)
                        result["success"] = True

                results.append(result)

            except Exception as e:
                result["error"] = str(e)
                results.append(result)
                logger.error(f"Error controlling {ieee}: {e}")

        await self._publish_group_state(group_id, command)
        return {"success": True, "results": results}


    async def announce_groups(self):
        """
        Publish discovery for all groups.
        Called by Core after MQTT is connected.
        """
        logger.info(f"📢 Announcing {len(self.groups)} groups to Home Assistant...")
        for group_id, group_info in self.groups.items():
            await self._publish_group_discovery(group_id, group_info)


    async def _publish_group_discovery(self, group_id: int, group: Dict):
        """
        Publish group to Home Assistant via MQTT discovery
        """
        if not hasattr(self.service, 'mqtt') or not self.service.mqtt:
            return

        base_topic = self.service.mqtt.base_topic
        node_id = f"group_{group_id}"
        group_name = group['name']
        safe_name = group_name.replace(' ', '_').lower()
        component = group['type']

        config = {
            "name": group_name,
            "unique_id": node_id,
            "state_topic": f"{base_topic}/group/{safe_name}",
            "command_topic": f"{base_topic}/group/{safe_name}/set",
            "schema": "json",
            "optimistic": False,
            "device": {
                "identifiers": [node_id],
                "name": f"Zigbee Group: {group_name}",
                "model": f"{component.capitalize()} Group",
                "manufacturer": "Zigbee Group",
                "via_device": f"{base_topic}"
            },
            "availability": [
                {
                    "topic": f"{base_topic}/bridge/state",
                    "payload_available": "online",
                    "payload_not_available": "offline"
                }
            ]
        }

        # Add capability-specific config
        if DeviceCapability.BRIGHTNESS in group['capabilities']:
            config['brightness'] = True
            config['brightness_scale'] = 254
            config['supported_color_modes'] = ['brightness']

        if DeviceCapability.COLOR_TEMP in group['capabilities']:
            config['color_temp'] = True
            if 'supported_color_modes' not in config:
                config['supported_color_modes'] = []
            config['supported_color_modes'].append('color_temp')

        if DeviceCapability.COLOR_XY in group['capabilities']:
            if 'supported_color_modes' not in config:
                config['supported_color_modes'] = []
            config['supported_color_modes'].append('xy')

        # Publish discovery
        topic = f"homeassistant/{component}/{node_id}/{component}/config"
        await self.service.mqtt.client.publish(
            topic,
            json.dumps(config),
            retain=True,
            qos=1
        )

        logger.info(f"Published group {group_id} discovery to Home Assistant")

    async def _unpublish_group_discovery(self, group_id: int, group_name: str):
        """Remove group from Home Assistant"""
        if not hasattr(self.service, 'mqtt') or not self.service.mqtt:
            return

        node_id = f"group_{group_id}"
        # Send empty config to remove
        for component in ['light', 'switch', 'cover', 'lock']:
            topic = f"homeassistant/{component}/{node_id}/{component}/config"
            await self.service.mqtt.client.publish(topic, "", retain=True, qos=1)

    def get_all_groups(self) -> List[Dict]:
        """Get all groups with enriched device info"""
        result = []
        for group_id, group in self.groups.items():
            # Add device names
            enriched = group.copy()
            enriched['devices'] = []
            for ieee in group['members']:
                device = self.service.devices.get(ieee)
                if device:
                    enriched['devices'].append({
                        "ieee": ieee,
                        "name": self._get_friendly_name(ieee),
                        "model": device.model if hasattr(device, 'model') else 'Unknown'
                    })
            result.append(enriched)
        return result

    def get_compatible_devices_for(self, ieee: str) -> List[Dict]:
        """
        Get list of devices compatible with the given device for grouping
        """
        device = self.service.devices.get(ieee)
        if not device:
            return []

        compatible = []

        # Use safe role access
        my_role = self._get_device_role(device)

        for other_ieee, other_device in self.service.devices.items():
            if other_ieee == ieee:
                continue

            # Use safe role access
            other_role = self._get_device_role(other_device)
            if other_role == "Coordinator":
                continue

            is_compatible, reason = self.are_devices_compatible(device, other_device)
            if is_compatible:
                compatible.append({
                    "ieee": other_ieee,
                    "name": self._get_friendly_name(other_ieee),
                    "type": self.get_device_type(other_device),
                    "capabilities": list(self.get_device_capabilities(other_device))
                })

        return compatible