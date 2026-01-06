def get_discovery_configs(self) -> List[Dict]:
    ep = self.endpoint.endpoint_id

    # ===== STEP 1: Check if this is a sensor endpoint FIRST =====
    is_contact_sensor = self._is_contact_sensor()
    has_only_sensor_clusters = len(self.endpoint.in_clusters) <= 4 and 0x0500 in self.endpoint.in_clusters

    # Contact sensors get binary_sensor discovery regardless of OnOff direction
    if is_contact_sensor or has_only_sensor_clusters:
        return [{
            "component": "binary_sensor",
            "object_id": f"contact_{ep}",
            "config": {
                "name": f"Contact Sensor {ep}",
                "device_class": "door",
                "value_template": f"{{{{ value_json.contact_{ep} }}}}",
                "payload_on": True,
                "payload_off": False
            }
        }]
    # ===== END STEP 1 =====

    # ===== STEP 2: Check OnOff direction for NON-SENSOR endpoints =====
    has_onoff_input = 0x0006 in self.endpoint.in_clusters
    has_onoff_output = 0x0006 in self.endpoint.out_clusters

    # If OnOff is OUTPUT-only and NOT a sensor, this is a controller (skip)
    if has_onoff_output and not has_onoff_input:
        logger.debug(f"[{self.device.ieee}] EP{ep} has OnOff in OUTPUT only - controller endpoint, skipping")
        return []

    # If no OnOff in INPUT at all, nothing to control
    if not has_onoff_input:
        logger.debug(f"[{self.device.ieee}] EP{ep} has no OnOff in INPUT - skipping")
        return []
    # ===== END STEP 2 =====

    # ===== STEP 3: Detect capabilities (INPUT clusters only) =====
    has_lightlink = 0x1000 in self.endpoint.in_clusters
    has_opple = 0xFCC0 in self.endpoint.in_clusters
    has_color = 0x0300 in self.endpoint.in_clusters
    has_level = 0x0008 in self.endpoint.in_clusters
    has_electrical = 0x0B04 in self.endpoint.in_clusters
    has_multi_state = 0x0012 in self.endpoint.in_clusters
    has_sonoff = 0xFC11 in self.endpoint.in_clusters
    # ===== END STEP 3 =====

    # Sonoff devices are never contact sensors
    if has_sonoff:
        is_contact_sensor = False  # Already handled above, but keep for clarity

    # ===== STEP 4: Light vs Switch detection =====
    if (has_electrical and has_level or has_multi_state or has_sonoff) and not (has_color or has_lightlink):
        is_light = False
        logger.info(f"[{self.device.ieee}] EP{ep} Force SWITCH: Electrical/Multistate/Sonoff present")
    else:
        is_light = has_lightlink or has_opple or has_color or has_level
        logger.info(f"[{self.device.ieee}] EP{ep} OnOff detected as: {'LIGHT' if is_light else 'SWITCH'} "
                    f"(lightlink={has_lightlink}, opple={has_opple}, color={has_color}, level={has_level})")

    component = "light" if is_light else "switch"
    configs = []