class DeviceCapabilities:
    # ... existing code ...

    # ========================================================================
    # COMPREHENSIVE CLUSTER CONFIGURATION MATRIX
    # ========================================================================

    # Clusters that NEVER support configuration (system/infrastructure)
    NEVER_CONFIGURABLE = {
        0x0000,  # Basic (read-only device info)
        0x0003,  # Identify (UI feedback only)
        0x0004,  # Groups (binding target, not source)
        0x0005,  # Scenes (binding target, not source)
        0x0007,  # OnOff Configuration (config storage, not reporting)
        0x0009,  # Alarms (event-driven)
        0x000A,  # Time (usually OUTPUT only)
        0x0013,  # Multistate Output (command-driven)
        0x0019,  # OTA (firmware updates)
        0x0020,  # Poll Control (deprecated)
        0x0021,  # Green Power (proxy/commissioning only)
        0x0100,  # Shade Configuration (settings, not state)
        0x0101,  # Door Lock (command-driven)
        0x0204,  # Thermostat UI Config (settings, not state)
        0x0301,  # Ballast Configuration (settings, not state)
        0x0401,  # Illuminance Level Sensing (thresholds, not measurement)
        0x0501,  # IAS ACE (command interface)
        0x0502,  # IAS WD (warning device commands)
        0x0B05,  # Diagnostics (read-only stats)
        0x1000,  # Touchlink/LightLink (commissioning)
        # Manufacturer-specific that are typically not configurable
        0xFC00,  # Philips (usually commands/config)
        0xFC11,  # Sonoff (settings storage)
    }

    # Clusters configurable ONLY if in INPUT clusters
    CONFIGURABLE_INPUT_ONLY = {
        # Power & Energy
        0x0001: "Power Configuration",        # Battery voltage/percentage
        0x0702: "Metering",                   # Energy consumption
        0x0B04: "Electrical Measurement",     # Voltage/current/power

        # Environmental Sensors
        0x0002: "Device Temperature",         # Internal temp
        0x0400: "Illuminance Measurement",    # Light level
        0x0402: "Temperature Measurement",    # Ambient temp
        0x0403: "Pressure Measurement",       # Barometric pressure
        0x0404: "Flow Measurement",           # Air/water flow
        0x0405: "Relative Humidity",          # Humidity %
        0x0406: "Occupancy Sensing",          # Motion/presence
        0x0407: "Leaf Wetness",               # Agriculture
        0x0408: "Soil Moisture",              # Agriculture
        0x040D: "CO2 Measurement",            # Air quality
        0x042A: "PM25 Measurement",           # Air quality

        # Actuator State (configurable for feedback)
        0x0006: "OnOff",                      # Switch state (bind only usually)
        0x0008: "Level Control",              # Dimmer position
        0x0102: "Window Covering",            # Blind position
        0x0201: "Thermostat",                 # HVAC state
        0x0202: "Fan Control",                # Fan speed/mode
        0x0203: "Dehumidification Control",   # Dehumidifier
        0x0300: "Color Control",              # Light color/temp

        # Security
        0x0500: "IAS Zone",                   # Alarm state

        # Inputs (sensor-like)
        0x000C: "Analog Input",               # Generic analog value
        0x000F: "Binary Input",               # Generic binary state
        0x0012: "Multistate Input",           # Multi-value sensor (buttons)
    }

    # Manufacturer-specific clusters (configurable if in INPUT)
    MANUFACTURER_SPECIFIC_CONFIGURABLE = {
        0xEF00: "Tuya Manufacturer",          # Tuya DP tunneling
        0xFCC0: "Aqara Manufacturer",         # Aqara extensions
    }

    # Clusters that use OUTPUT for binding (not for state reporting)
    BINDING_OUTPUT_CLUSTERS = {
        0x0006: "OnOff",           # Buttons/sensors bind to lights
        0x0008: "Level Control",   # Dimmers bind to lights
        0x0300: "Color Control",   # Color remotes bind to lights
        0x0004: "Groups",          # Group commands
        0x0005: "Scenes",          # Scene recall
    }

    def __init__(self, zha_device):
        self.device = zha_device
        self.zigpy_dev = zha_device.zigpy_dev
        self._capabilities: Set[str] = set()
        self._cluster_ids: Set[int] = set()
        self._configurable_endpoints: Dict[int, Dict[str, Any]] = {}  # {ep_id: {...}}
        self._detect_capabilities()

    def _detect_capabilities(self):
        """Smart Capability Detection with comprehensive cluster analysis."""
        self._capabilities.clear()
        self._cluster_ids.clear()
        self._configurable_endpoints.clear()

        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        # --- PHASE 1: Comprehensive Endpoint Analysis ---
        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue  # Skip ZDO

            ep_info = {
                'configurable_clusters': set(),
                'input_clusters': set(),
                'output_clusters': set(),
                'role': 'unknown',  # 'actuator', 'sensor', 'controller', 'mixed'
            }

            # Analyze INPUT clusters
            for cluster in ep.in_clusters.values():
                cid = cluster.cluster_id
                self._cluster_ids.add(cid)
                ep_info['input_clusters'].add(cid)

                # Determine if configurable
                if cid in self.NEVER_CONFIGURABLE:
                    continue  # Skip entirely

                if cid in self.CONFIGURABLE_INPUT_ONLY:
                    ep_info['configurable_clusters'].add(cid)

                if cid in self.MANUFACTURER_SPECIFIC_CONFIGURABLE:
                    ep_info['configurable_clusters'].add(cid)

            # Analyze OUTPUT clusters (for role detection)
            for cluster in ep.out_clusters.values():
                cid = cluster.cluster_id
                self._cluster_ids.add(cid)
                ep_info['output_clusters'].add(cid)

            # Determine endpoint role
            has_actuator_inputs = bool(ep_info['input_clusters'] & {0x0006, 0x0008, 0x0102, 0x0201, 0x0300})
            has_sensor_inputs = bool(ep_info['input_clusters'] & {0x0400, 0x0402, 0x0405, 0x0406, 0x0500})
            has_control_outputs = bool(ep_info['output_clusters'] & self.BINDING_OUTPUT_CLUSTERS)

            if has_actuator_inputs and not has_control_outputs:
                ep_info['role'] = 'actuator'  # Light/switch that gets controlled
            elif has_sensor_inputs and not has_actuator_inputs:
                ep_info['role'] = 'sensor'  # Pure sensor
            elif has_control_outputs and not has_actuator_inputs:
                ep_info['role'] = 'controller'  # Button/remote that controls others
            elif has_actuator_inputs and has_sensor_inputs:
                ep_info['role'] = 'mixed'  # E.g., thermostat (controls + senses)
            else:
                ep_info['role'] = 'passive'  # Minimal functionality

            # Store endpoint info
            self._configurable_endpoints[ep_id] = ep_info

            LOGGER.debug(
                f"[{self.device.ieee}] EP{ep_id} role={ep_info['role']}, "
                f"configurable={len(ep_info['configurable_clusters'])}, "
                f"in={len(ep_info['input_clusters'])}, out={len(ep_info['output_clusters'])}"
            )

        # --- PHASE 2: Standard Capability Detection (existing code) ---
        # ... keep existing capability detection logic ...

        # --- PHASE 3: Multi-Endpoint Device Quirks ---
        total_endpoints = len([e for e in self.zigpy_dev.endpoints if e > 0])

        if total_endpoints > 1:
            self._capabilities.add('multi_endpoint')

            # Detect multi-socket/switch patterns
            actuator_endpoints = [
                ep_id for ep_id, info in self._configurable_endpoints.items()
                if info['role'] in ('actuator', 'mixed')
            ]
            if len(actuator_endpoints) > 1:
                self._capabilities.add('multi_switch')
                LOGGER.info(f"[{self.device.ieee}] Multi-switch device detected: "
                            f"EPs {actuator_endpoints}")

        # Philips Motion Sensor quirk
        if ("philips" in manufacturer or "signify" in manufacturer) and "sml" in model:
            # EP1 is controller (OUTPUT only), EP2 is sensor
            if 1 in self._configurable_endpoints:
                # Force EP1 as non-configurable
                self._configurable_endpoints[1]['configurable_clusters'].clear()
                self._configurable_endpoints[1]['role'] = 'controller'
                LOGGER.info(f"[{self.device.ieee}] Applied Philips SML quirk: EP1=controller (skip config)")

    def is_endpoint_configurable(self, endpoint_id: int) -> bool:
        """Check if an endpoint has any configurable clusters."""
        if endpoint_id not in self._configurable_endpoints:
            return False
        return bool(self._configurable_endpoints[endpoint_id]['configurable_clusters'])

    def is_cluster_configurable(self, cluster_id: int, endpoint_id: int) -> bool:
        """Check if a specific cluster on an endpoint is configurable."""
        if endpoint_id not in self._configurable_endpoints:
            return False
        return cluster_id in self._configurable_endpoints[endpoint_id]['configurable_clusters']

    def get_endpoint_role(self, endpoint_id: int) -> str:
        """Get the role of an endpoint."""
        return self._configurable_endpoints.get(endpoint_id, {}).get('role', 'unknown')

    def get_configurable_clusters(self, endpoint_id: Optional[int] = None) -> Set[int]:
        """Get configurable cluster IDs for an endpoint or all endpoints."""
        if endpoint_id is not None:
            return self._configurable_endpoints.get(endpoint_id, {}).get('configurable_clusters', set())

        # All configurable clusters across all endpoints
        all_clusters = set()
        for ep_info in self._configurable_endpoints.values():
            all_clusters.update(ep_info['configurable_clusters'])
        return all_clusters

    def get_configuration_info(self) -> Dict[str, Any]:
        """Get detailed configuration capability info for API/debugging."""
        return {
            "endpoints": {
                ep_id: {
                    "role": info['role'],
                    "configurable": [f"0x{c:04x}" for c in info['configurable_clusters']],
                    "input_count": len(info['input_clusters']),
                    "output_count": len(info['output_clusters']),
                }
                for ep_id, info in self._configurable_endpoints.items()
            },
            "total_configurable_clusters": len(self.get_configurable_clusters()),
            "is_multi_endpoint": self.has_capability('multi_endpoint'),
        }