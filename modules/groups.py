"""
Zigbee Groups Management Module
Based on ZHA and Zigbee2MQTT patterns for native Zigbee groups

Enhanced with Input/Output cluster awareness:
- input_clusters (Server) = Device RECEIVES commands = Actuators (controllable)
- output_clusters (Client) = Device SENDS commands = Sensors/Remotes (not controllable)

For groups, only devices with clusters as INPUT clusters are truly controllable.
"""

import logging
import json
from typing import Dict, List, Set, Optional, Any, Tuple
from dataclasses import dataclass
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


# Cluster IDs for group-controllable functionality
class ClusterId:
    BASIC = 0x0000
    POWER_CONFIG = 0x0001
    IDENTIFY = 0x0003
    GROUPS = 0x0004
    SCENES = 0x0005
    ON_OFF = 0x0006
    LEVEL_CONTROL = 0x0008
    DOOR_LOCK = 0x0101
    WINDOW_COVERING = 0x0102
    COLOR_CONTROL = 0x0300
    OCCUPANCY = 0x0406
    IAS_ZONE = 0x0500


@dataclass
class ClusterPresence:
    """Tracks where a cluster exists on a device"""
    cluster_id: int
    in_input: bool = False    # Server - device receives commands (controllable)
    in_output: bool = False   # Client - device sends commands (sensor/remote)
    endpoint_id: int = 0

    @property
    def is_controllable(self) -> bool:
        """Only INPUT clusters can receive group commands"""
        return self.in_input

    @property
    def is_sensor_only(self) -> bool:
        """OUTPUT only = sensor/remote, not controllable"""
        return self.in_output and not self.in_input


class GroupManager:
    """
    Manages Zigbee groups with smart device compatibility
    Enhanced with input/output cluster awareness
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
        """Get friendly name for a device or IEEE address"""
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
        """Handle incoming MQTT group commands"""
        group_id = self._get_group_id_by_name(group_name)

        if group_id is None:
            logger.warning(f"MQTT Group Command received for unknown group name/ID: {group_name}")
            return {"error": f"Group name '{group_name}' not found"}

        logger.info(f"Handling MQTT command for group ID {group_id} ({group_name}): {data}")

        # The core logic is already in control_group
        return await self.control_group(group_id, data)

    # =========================================================================
    # Cluster Analysis with Input/Output Awareness
    # =========================================================================

    def _analyze_device_clusters(self, device) -> Dict[int, ClusterPresence]:
        """
        Analyze all clusters on a device, tracking input vs output presence.

        Returns dict of cluster_id -> ClusterPresence
        """
        clusters: Dict[int, ClusterPresence] = {}

        # Get zigpy device
        zdev = getattr(device, 'zigpy_dev', None)
        if not zdev:
            return clusters

        for ep_id, endpoint in zdev.endpoints.items():
            if ep_id == 0:  # Skip ZDO endpoint
                continue

            # Check INPUT clusters (Server - controllable)
            if hasattr(endpoint, 'in_clusters'):
                for cluster_id in endpoint.in_clusters:
                    if cluster_id not in clusters:
                        clusters[cluster_id] = ClusterPresence(cluster_id=cluster_id)
                    clusters[cluster_id].in_input = True
                    clusters[cluster_id].endpoint_id = ep_id

            # Check OUTPUT clusters (Client - sensor/remote)
            if hasattr(endpoint, 'out_clusters'):
                for cluster_id in endpoint.out_clusters:
                    if cluster_id not in clusters:
                        clusters[cluster_id] = ClusterPresence(cluster_id=cluster_id)
                    clusters[cluster_id].in_output = True
                    if clusters[cluster_id].endpoint_id == 0:
                        clusters[cluster_id].endpoint_id = ep_id

        return clusters

    def _is_actuator(self, device) -> bool:
        """
        Determine if device is an actuator (can receive control commands).

        An actuator has relevant control clusters as INPUT clusters.
        A sensor/remote has them only as OUTPUT clusters.
        """
        clusters = self._analyze_device_clusters(device)

        # Control clusters that matter for groups
        control_cluster_ids = {
            ClusterId.ON_OFF,
            ClusterId.LEVEL_CONTROL,
            ClusterId.COLOR_CONTROL,
            ClusterId.WINDOW_COVERING,
            ClusterId.DOOR_LOCK,
        }

        for cluster_id in control_cluster_ids:
            if cluster_id in clusters and clusters[cluster_id].is_controllable:
                return True

        return False

    def _get_controllable_clusters(self, device) -> Set[int]:
        """Get only the clusters that are INPUT (controllable via groups)"""
        clusters = self._analyze_device_clusters(device)
        return {cid for cid, presence in clusters.items() if presence.is_controllable}

    def _get_sensor_only_clusters(self, device) -> Set[int]:
        """Get clusters that are OUTPUT only (sensor/reporting)"""
        clusters = self._analyze_device_clusters(device)
        return {cid for cid, presence in clusters.items() if presence.is_sensor_only}

    def get_device_type(self, device) -> Optional[str]:
        """
        Determine device type from discovery configs or capabilities.
        Enhanced: Only considers INPUT clusters for type determination.
        """
        # 1. Try discovery configs first
        if hasattr(device, 'get_discovery_configs'):
            configs = device.get_discovery_configs()
            for config in configs:
                component = config.get('component')
                if component in ['light', 'switch', 'cover', 'lock']:
                    return component

        # 2. Smart detection based on CONTROLLABLE capabilities only
        caps = self.get_device_capabilities(device)

        # Must be an actuator to be a controllable device type
        if not self._is_actuator(device):
            return None  # Sensors/remotes don't get a controllable type

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
        """
        Detect controllable capabilities (INPUT clusters only).
        """
        capabilities = set()
        controllable = self._get_controllable_clusters(device)

        if ClusterId.ON_OFF in controllable:
            capabilities.add(DeviceCapability.ON_OFF)
        if ClusterId.LEVEL_CONTROL in controllable:
            capabilities.add(DeviceCapability.BRIGHTNESS)
        if ClusterId.COLOR_CONTROL in controllable:
            capabilities.add(DeviceCapability.COLOR_TEMP)
            capabilities.add(DeviceCapability.COLOR_XY)
        if ClusterId.WINDOW_COVERING in controllable:
            capabilities.add(DeviceCapability.POSITION)
        if ClusterId.DOOR_LOCK in controllable:
            capabilities.add(DeviceCapability.LOCK)

        return capabilities

    def _get_device_role(self, device) -> str:
        """Helper to safely get device role (Router/EndDevice/Coordinator)"""
        if hasattr(device, 'get_role'):
            return device.get_role()
        return getattr(device, 'type', 'Unknown')

    def are_devices_compatible(self, device1, device2) -> Tuple[bool, str]:
        """
        Check if two devices are compatible for grouping.
        Enhanced: Rejects devices that aren't actuators.
        """
        # First check: both must be actuators
        if not self._is_actuator(device1):
            return False, "First device is not controllable (sensor/remote only)"
        if not self._is_actuator(device2):
            return False, "Second device is not controllable (sensor/remote only)"

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
        """Find capabilities common to all devices"""
        if not devices:
            return set()

        common = self.get_device_capabilities(devices[0])
        for device in devices[1:]:
            common &= self.get_device_capabilities(device)

        return common

    # =========================================================================
    # Smart Device Selection with Relevance Scoring
    # =========================================================================

    def get_device_group_info(self, device) -> Dict[str, Any]:
        """
        Get detailed grouping info for a device including actuator status.
        """
        ieee = str(device.ieee) if hasattr(device, 'ieee') else str(device)
        clusters = self._analyze_device_clusters(device)

        # Find sensor-only clusters (potential confusion sources)
        sensor_only = []
        for cid, presence in clusters.items():
            if presence.is_sensor_only and cid in [ClusterId.ON_OFF, ClusterId.LEVEL_CONTROL]:
                sensor_only.append(cid)

        is_actuator = self._is_actuator(device)
        device_type = self.get_device_type(device)
        capabilities = list(self.get_device_capabilities(device))

        # Calculate relevance score (0-100)
        relevance = 0
        if is_actuator:
            relevance = 70 + min(30, len(capabilities) * 10)
        elif sensor_only:
            relevance = 10  # Very low - shouldn't be in control groups

        notes = []
        if not is_actuator:
            notes.append("Not controllable - sensor or remote only")
        if sensor_only:
            cluster_names = {ClusterId.ON_OFF: "On/Off", ClusterId.LEVEL_CONTROL: "Level"}
            for cid in sensor_only:
                name = cluster_names.get(cid, hex(cid))
                notes.append(f"{name} is OUTPUT only (reports, cannot receive commands)")

        # Check for Groups cluster support
        if ClusterId.GROUPS not in clusters or not clusters[ClusterId.GROUPS].in_input:
            notes.append("May not support native Zigbee groups (no Groups input cluster)")

        return {
            "ieee": ieee,
            "name": self._get_friendly_name(ieee),
            "type": device_type,
            "is_actuator": is_actuator,
            "capabilities": capabilities,
            "relevance_score": relevance,
            "notes": notes,
            "model": getattr(device, 'model', 'Unknown') if hasattr(device, 'model') else 'Unknown'
        }

    def get_compatible_devices_for(self, ieee: str) -> List[Dict]:
        """
        Get devices compatible with given device for grouping.
        Enhanced: Includes relevance scoring and excludes non-actuators.
        """
        device = self.service.devices.get(ieee)
        if not device:
            return []

        # Source device must be an actuator
        if not self._is_actuator(device):
            logger.info(f"Device {ieee} is not an actuator, cannot form groups")
            return []

        compatible = []
        my_role = self._get_device_role(device)

        for other_ieee, other_device in self.service.devices.items():
            if other_ieee == ieee:
                continue

            other_role = self._get_device_role(other_device)
            if other_role == "Coordinator":
                continue

            # Key check: other device must be an actuator
            if not self._is_actuator(other_device):
                continue

            is_compatible, reason = self.are_devices_compatible(device, other_device)
            if is_compatible:
                info = self.get_device_group_info(other_device)
                compatible.append(info)

        # Sort by relevance score descending
        compatible.sort(key=lambda x: x['relevance_score'], reverse=True)
        return compatible

    def get_all_groupable_devices(self, group_type: Optional[str] = None) -> Dict[str, List[Dict]]:
        """
        Get all devices that can be added to groups, categorized.

        Args:
            group_type: Optional filter ('light', 'switch', 'cover', 'lock')

        Returns:
            {
                "recommended": [...],  # Actuators matching type
                "other_actuators": [...],  # Actuators of other types
                "not_recommended": [...]  # Non-actuators (for reference)
            }
        """
        recommended = []
        other_actuators = []
        not_recommended = []

        for ieee, device in self.service.devices.items():
            role = self._get_device_role(device)
            if role == "Coordinator":
                continue

            info = self.get_device_group_info(device)

            if not info['is_actuator']:
                not_recommended.append(info)
            elif group_type and info['type'] == group_type:
                recommended.append(info)
            elif group_type and info['type'] in ['light', 'switch'] and group_type in ['light', 'switch']:
                # Allow light/switch mixing
                recommended.append(info)
            elif group_type:
                other_actuators.append(info)
            else:
                recommended.append(info)

        # Sort each list by relevance
        recommended.sort(key=lambda x: x['relevance_score'], reverse=True)
        other_actuators.sort(key=lambda x: x['relevance_score'], reverse=True)
        not_recommended.sort(key=lambda x: x['relevance_score'], reverse=True)

        return {
            "recommended": recommended,
            "other_actuators": other_actuators,
            "not_recommended": not_recommended
        }

    # =========================================================================
    # Group Creation and Management (unchanged logic, uses enhanced detection)
    # =========================================================================

    async def create_group(self, name: str, device_iees: List[str]) -> Dict:
        """Create a new Zigbee group"""
        name = name.strip()

        for group in self.groups.values():
            if group['name'].lower() == name.lower():
                return {"error": f"Group name '{name}' already exists"}

        if len(device_iees) < 2:
            return {"error": "Groups require at least 2 devices"}

        devices = []
        for ieee in device_iees:
            device = self.service.devices.get(ieee)
            if not device:
                return {"error": f"Device {ieee} not found"}

            # Check if device is an actuator
            if not self._is_actuator(device):
                return {"error": f"Device {self._get_friendly_name(ieee)} cannot be controlled (sensor/remote only)"}

            devices.append(device)

        base_device = devices[0]
        for device in devices[1:]:
            compatible, reason = self.are_devices_compatible(base_device, device)
            if not compatible:
                return {"error": f"Device {device.ieee} incompatible: {reason}"}

        capabilities = self.get_common_capabilities(devices)
        if not capabilities:
            base_type = self.get_device_type(base_device)
            if base_type in ['light', 'switch']:
                capabilities = {DeviceCapability.ON_OFF}
            else:
                return {"error": "Devices have no common capabilities"}

        # Determine group type based on capabilities (not just first device)
        group_type = self._determine_group_type(capabilities, devices)

        group_id = self.next_group_id
        self.next_group_id += 1

        group_info = {
            "id": group_id,
            "name": name,
            "type": group_type,
            "capabilities": list(capabilities),
            "members": device_iees,
            "created_at": None
        }

        try:
            await self._add_devices_to_zigbee_group(group_id, devices)
        except Exception as e:
            logger.error(f"Failed to create Zigbee group: {e}")
            return {"error": f"Failed to create Zigbee group: {str(e)}"}

        self.groups[group_id] = group_info
        self.save_groups()
        await self._publish_group_discovery(group_id, group_info)

        return {"success": True, "group": group_info}

    def _determine_group_type(self, capabilities: Set[str], devices: List) -> str:
        """Determine group type based on common capabilities"""
        if (DeviceCapability.BRIGHTNESS in capabilities or
                DeviceCapability.COLOR_XY in capabilities or
                DeviceCapability.COLOR_TEMP in capabilities):
            return "light"

        # Check if majority are lights
        device_types = [self.get_device_type(d) for d in devices]
        light_count = device_types.count("light")

        if light_count > len(devices) / 2:
            return "light"

        # Position capability = cover
        if DeviceCapability.POSITION in capabilities:
            return "cover"

        # Lock capability = lock
        if DeviceCapability.LOCK in capabilities:
            return "lock"

        # Default to switch for on/off only
        return "switch"

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

        #Check if device is an actuator
        if not self._is_actuator(device):
            return {"error": f"Device cannot be controlled (sensor/remote only)"}

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

        if "color_mode" in state:
            payload["color_mode"] = state["color_mode"]

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
        """Read actual state from group member devices"""
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

                    # Read color mode
                    if 0x0300 in endpoint.in_clusters:
                        color_cluster = endpoint.in_clusters[0x0300]
                        result = await color_cluster.read_attributes([0x0007, 0x0008])  # ColorTemp + ColorMode
                        if 7 in result[0]:
                            state['color_temp'] = result[0][7]
                        if 8 in result[0]:
                            mode_val = result[0][8]
                            if hasattr(mode_val, 'value'):
                                mode_val = mode_val.value
                            mode_map = {0: 'hs', 1: 'xy', 2: 'color_temp'}
                            state['color_mode'] = mode_map.get(mode_val, 'color_temp')

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
        """Publish initial group state after creation"""
        state = await self._read_group_state(group_id)
        if state:
            await self._publish_group_state(group_id, state)


    async def control_group(self, group_id: int, command: Dict) -> Dict:
        """Control all devices in a group using Direct Cluster Commands"""
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
                        await color_ctrl.move_to_color_temp(mireds, transition_time=10)
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

                # 5. HS COLOR (from frontend color picker via HS values)
                if 'hs_color' in command:
                    color_ctrl = get_cluster(0x0300)
                    if color_ctrl:
                        hs = command['hs_color']
                        hue, sat = hs if isinstance(hs, (list, tuple)) else (0, 100)
                        zcl_hue = int((hue / 360) * 254)
                        zcl_sat = int((sat / 100) * 254)
                        await color_ctrl.move_to_hue_and_saturation(zcl_hue, zcl_sat, transition_time=10)
                        result["success"] = True

                results.append(result)

            except Exception as e:
                result["error"] = str(e)
                results.append(result)
                logger.error(f"Error controlling {ieee}: {e}")

        await self._publish_group_state(group_id, command)
        return {"success": True, "results": results}


    async def announce_groups(self):
        """Publish discovery for all groups"""
        logger.info(f"📢 Announcing {len(self.groups)} groups to Home Assistant...")
        for group_id, group_info in self.groups.items():
            await self._publish_group_discovery(group_id, group_info)


    async def _publish_group_discovery(self, group_id: int, group: Dict):
        """Publish group to Home Assistant via MQTT discovery"""
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
        color_modes = []

        if DeviceCapability.BRIGHTNESS in group['capabilities']:
            config['brightness'] = True
            config['brightness_scale'] = 254
            color_modes.append('brightness')

        if DeviceCapability.COLOR_TEMP in group['capabilities']:
            color_modes.append('color_temp')

        if DeviceCapability.COLOR_XY in group['capabilities']:
            color_modes.append('xy')

        if DeviceCapability.COLOR_HS in group['capabilities']:
            color_modes.append('hs')

        if color_modes:
            config['supported_color_modes'] = color_modes
            config['color_mode'] = True

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