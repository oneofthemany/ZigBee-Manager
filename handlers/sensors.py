import logging
import json
from typing import Any, Dict, Optional, List
from zigpy.zcl.clusters.measurement import IlluminanceMeasurement, TemperatureMeasurement
import asyncio
from .base import ClusterHandler, register_handler

try:
    # New zigpy versions (0.60+)
    from zigpy.types import uint16
except ImportError:
    try:
        # Older zigpy versions
        from zigpy.types.basic import uint16
    except ImportError:
        # Fallback for some specific intermediate versions
        from zigpy.types import uint16_t as uint16
logger = logging.getLogger("handlers.sensors")

# Cluster IDs
ILLUMINANCE_MEASUREMENT = IlluminanceMeasurement.cluster_id # 0x0400
TEMPERATURE_MEASUREMENT = TemperatureMeasurement.cluster_id # 0x0402
OCCUPANCY_SENSING = 0x0406 #

# Attribute IDs
ILLUMINANCE_MEASURED_VALUE = 0x0000
TEMPERATURE_MEASURED_VALUE = 0x0000
OCCUPANCY_VALUE = 0x0000

# Default Configuration Values for Battery-Powered Sensors (tuned for responsiveness and battery life)
# Illuminance
ILLUM_MIN_REPORT_INTERVAL = 5    # Report every 5 seconds (minimum)
ILLUM_MAX_REPORT_INTERVAL = 300  # Report every 5 minutes (maximum)
ILLUM_REPORTABLE_CHANGE = 100    # Report if the raw value changes by 100

# Temperature
TEMP_MIN_REPORT_INTERVAL = 30    # Report every 30 seconds (minimum)
TEMP_MAX_REPORT_INTERVAL = 3600  # Report every 1 hour (maximum)
TEMP_REPORTABLE_CHANGE = 50      # Report if the value changes by 0.5 degrees (50 raw units)


async def configure_illuminance_reporting(device, endpoint_id: int):
    """
    Configures automatic reporting for the Illuminance Measurement cluster (0x0400)
    on a specified endpoint. Essential for battery-powered sensors like Philips Hue.

    Args:
        device: The ZHADevice wrapper instance.
        endpoint_id: The specific endpoint ID (e.g., 2 for Philips Hue Motion).
    """
    ieee = device.ieee

    logger.debug(f"[{ieee}] Checking for Illuminance Measurement cluster on EP{endpoint_id}...")

    illuminance_ep = device.zigpy_dev.endpoints.get(endpoint_id)

    if not illuminance_ep or ILLUMINANCE_MEASUREMENT not in illuminance_ep.in_clusters:
        logger.debug(f"[{ieee}] Illuminance Measurement cluster (0x0400) not found on EP{endpoint_id}. Skipping configuration.")
        return

    try:
        cluster = illuminance_ep.in_clusters[ILLUMINANCE_MEASUREMENT]

        logger.info(
            f"[{ieee}] Configuring reporting for IlluminanceMeasuredValue (0x0000) on EP{endpoint_id}: "
            f"Min={ILLUM_MIN_REPORT_INTERVAL}s, Max={ILLUM_MAX_REPORT_INTERVAL}s, Change={ILLUM_REPORTABLE_CHANGE}"
        )

        # Send the Configure Reporting command
        async with asyncio.timeout(10.0):
            result = await cluster.configure_reporting(
                ILLUMINANCE_MEASURED_VALUE,
                uint16, # MeasuredValue is of type uint16
                ILLUM_MIN_REPORT_INTERVAL,
                ILLUM_MAX_REPORT_INTERVAL,
                ILLUM_REPORTABLE_CHANGE
            )

        logger.info(f"[{ieee}] Illuminance reporting configuration result: {result}")

        # Read the current value immediately to update the state after configuration
        async with asyncio.timeout(5.0):
            current_value = await cluster.read_attributes([ILLUMINANCE_MEASURED_VALUE])
        logger.info(f"[{ieee}] Initial Illuminance MeasuredValue read: {current_value}")

    except asyncio.TimeoutError:
         logger.warning(f"[{ieee}] Illuminance configuration timed out on EP{endpoint_id}")
    except Exception as e:
        logger.error(f"[{ieee}] Failed to configure Illuminance reporting on EP{endpoint_id}: {e}", exc_info=True)


async def configure_temperature_reporting(device, endpoint_id: int):
    """
    Configures automatic reporting for the Temperature Measurement cluster (0x0402)
    on a specified endpoint.
    """
    ieee = device.ieee

    logger.debug(f"[{ieee}] Checking for Temperature Measurement cluster on EP{endpoint_id}...")

    temp_ep = device.zigpy_dev.endpoints.get(endpoint_id)

    if not temp_ep or TEMPERATURE_MEASUREMENT not in temp_ep.in_clusters:
        logger.debug(f"[{ieee}] Temperature Measurement cluster (0x0402) not found on EP{endpoint_id}. Skipping configuration.")
        return

    try:
        cluster = temp_ep.in_clusters[TEMPERATURE_MEASUREMENT]

        logger.info(
            f"[{ieee}] Configuring reporting for TemperatureMeasuredValue (0x0000) on EP{endpoint_id}: "
            f"Min={TEMP_MIN_REPORT_INTERVAL}s, Max={TEMP_MAX_REPORT_INTERVAL}s, Change={TEMP_REPORTABLE_CHANGE}"
        )

        # Send the Configure Reporting command
        async with asyncio.timeout(10.0):
            result = await cluster.configure_reporting(
                TEMPERATURE_MEASURED_VALUE,
                uint16, # MeasuredValue is of type uint16 (raw is deg C * 100)
                TEMP_MIN_REPORT_INTERVAL,
                TEMP_MAX_REPORT_INTERVAL,
                TEMP_REPORTABLE_CHANGE
            )

        logger.info(f"[{ieee}] Temperature reporting configuration result: {result}")

    except asyncio.TimeoutError:
         logger.warning(f"[{ieee}] Temperature configuration timed out on EP{endpoint_id}")
    except Exception as e:
        logger.error(f"[{ieee}] Failed to configure Temperature reporting on EP{endpoint_id}: {e}", exc_info=True)

# ============================================================
# OCCUPANCY SENSING CLUSTER (0x0406)
# Used by: Philips Hue Motion Sensors, Aqara Motion Sensors
# This is the STANDARD way occupancy sensors report occupancy.
# ============================================================
@register_handler(0x0406)
class OccupancySensingHandler(ClusterHandler):
    """
    Handles Occupancy Sensing cluster (0x0406).
    """
    CLUSTER_ID = 0x0406
    REPORT_CONFIG = [
        ("occupancy", 0, 300, 1),  # Report immediately on change, max 5 min
    ]

    # Occupancy attribute IDs
    ATTR_OCCUPANCY = 0x0000
    ATTR_OCCUPANCY_TYPE = 0x0001
    ATTR_PIR_O_TO_U_DELAY = 0x0010  # Occupied to Unoccupied delay (seconds)
    ATTR_PIR_U_TO_O_DELAY = 0x0011  # Unoccupied to Occupied delay (seconds)
    ATTR_PIR_U_TO_O_THRESHOLD = 0x0012

    # Philips Hue Specific
    ATTR_SENSITIVITY = 0x0030
    ATTR_SENSITIVITY_MAX = 0x0031

    # Occupancy types
    OCCUPANCY_TYPES = {
        0: "PIR",
        1: "Ultrasonic",
        2: "PIR+Ultrasonic",
        3: "PhysicalContact"
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """Handle occupancy attribute updates."""
        try:
            # Handle wrapped types
            if hasattr(value, 'value'):
                value = value.value

            if attrid == self.ATTR_OCCUPANCY:
                # Occupancy is a bitmap, bit 0 = occupied
                is_occupied = bool(value & 0x01) if isinstance(value, int) else bool(value)

                self.device.update_state({
                    "occupancy": is_occupied,
                    "motion": is_occupied,
                    "presence": is_occupied,
                })

                # === FAST-PATH PUBLISH ===
                # Use non-blocking publish for immediate MQTT update
                if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                    safe_name = self.device.service.get_safe_name(self.device.ieee)
                    payload = json.dumps({
                        'occupancy': is_occupied,
                        'motion': is_occupied,
                        'presence': is_occupied
                    })
                    self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)
                # === END FAST-PATH PUBLISH ===

                status = "MOTION DETECTED" if is_occupied else "Motion cleared"
                logger.info(f"[{self.device.ieee}] Occupancy Sensing: {status}")

            elif attrid == self.ATTR_OCCUPANCY_TYPE:
                type_name = self.OCCUPANCY_TYPES.get(value, f"Unknown({value})")
                self.device.update_state({"occupancy_type": type_name})

            elif attrid == self.ATTR_PIR_O_TO_U_DELAY:
                self.device.update_state({"motion_timeout": value, "pir_o_to_u_delay": value})

            elif attrid == self.ATTR_SENSITIVITY:
                self.device.update_state({"sensitivity": value})

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error processing occupancy attribute: {e}")

    def get_attr_name(self, attrid: int) -> str:
        names = {
            self.ATTR_OCCUPANCY: "occupancy",
            self.ATTR_OCCUPANCY_TYPE: "occupancy_type",
            self.ATTR_PIR_O_TO_U_DELAY: "motion_timeout",
            self.ATTR_SENSITIVITY: "sensitivity"
        }
        return names.get(attrid, super().get_attr_name(attrid))

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_OCCUPANCY: "occupancy",
            self.ATTR_PIR_O_TO_U_DELAY: "motion_timeout",
            self.ATTR_SENSITIVITY: "sensitivity"
        }

    async def configure(self):
        """
        Configure occupancy sensing cluster.

        Override base configure() to also read configuration values from device
        (timeout, sensitivity) so they can be displayed in the UI.
        """
        # First do standard binding and reporting configuration
        await super().configure()

        # Now read configuration attributes
        try:
            # Read PIR timeout (how long motion stays active)
            try:
                result = await self.cluster.read_attributes([self.ATTR_PIR_O_TO_U_DELAY])
                if result and self.ATTR_PIR_O_TO_U_DELAY in result[0]:
                    timeout = result[0][self.ATTR_PIR_O_TO_U_DELAY]
                    if hasattr(timeout, 'value'):
                        timeout = timeout.value
                    self.device.update_state({
                        "motion_timeout": timeout,
                        "pir_o_to_u_delay": timeout
                    })
                    logger.info(f"[{self.device.ieee}] Read motion timeout: {timeout}s")
            except Exception as e:
                logger.debug(f"[{self.device.ieee}] Could not read motion timeout: {e}")

            # Read sensitivity (Philips/Aqara specific)
            man = (self.device.zigpy_dev.manufacturer or "").lower()
            if 'philips' in man or 'lumi' in man or 'signify' in man:
                try:
                    result = await self.cluster.read_attributes([self.ATTR_SENSITIVITY])
                    if result and self.ATTR_SENSITIVITY in result[0]:
                        sensitivity = result[0][self.ATTR_SENSITIVITY]
                        if hasattr(sensitivity, 'value'):
                            sensitivity = sensitivity.value
                        self.device.update_state({"sensitivity": sensitivity})
                        logger.info(f"[{self.device.ieee}] Read motion sensitivity: {sensitivity}")
                except Exception as e:
                    logger.debug(f"[{self.device.ieee}] Could not read sensitivity: {e}")

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Error reading occupancy config: {e}")

        return True

    # --- DYNAMIC CONFIGURATION EXPOSURE ---
    def get_configuration_options(self) -> List[Dict]:
        """Expose supported configurations to the frontend."""
        options = [
            {
                "name": "motion_timeout",
                "label": "Motion Timeout (s)",
                "type": "number",
                "min": 0, "max": 65535,
                "description": "Hardware timeout for PIR reset (O->U Delay)",
                "attribute_id": self.ATTR_PIR_O_TO_U_DELAY,
                "current_value": self.device.state.get("motion_timeout", 60)  # Show current value
            }
        ]

        # Only expose sensitivity if it's supported (Hue/Aqara)
        man = (self.device.zigpy_dev.manufacturer or "").lower()
        if 'philips' in man or 'lumi' in man or 'signify' in man:
            options.append({
                "name": "sensitivity",
                "label": "Motion Sensitivity",
                "type": "number",
                "min": 0, "max": 2,
                "description": "0=Low, 1=Medium, 2=High",
                "attribute_id": self.ATTR_SENSITIVITY,
                "current_value": self.device.state.get("sensitivity", 1)  # Show current value
            })

        return options

    async def apply_settings(self, settings: Dict[str, Any]):
        """
        Apply configuration changes from the frontend.

        This is called when user changes settings in the UI.
        """
        try:
            # Handle motion_timeout
            if "motion_timeout" in settings:
                timeout = int(settings["motion_timeout"])
                await self.cluster.write_attributes({"pir_o_to_u_delay": timeout})
                self.device.update_state({
                    "motion_timeout": timeout,
                    "pir_o_to_u_delay": timeout
                })
                logger.info(f"[{self.device.ieee}] Set motion timeout: {timeout}s")

            # Handle sensitivity
            if "sensitivity" in settings:
                sensitivity = int(settings["sensitivity"])
                # Philips uses attribute 0x0030
                await self.cluster.write_attributes({self.ATTR_SENSITIVITY: sensitivity})
                self.device.update_state({"sensitivity": sensitivity})
                logger.info(f"[{self.device.ieee}] Set sensitivity: {sensitivity}")

            return True

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Failed to apply occupancy settings: {e}")
            return False


    # --- HA Discovery ---
    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "binary_sensor",
            "object_id": "motion",
            "config": {
                "name": "Motion",
                "device_class": "motion",
                "value_template": "{{ 'ON' if value_json.occupancy else 'OFF' }}"
            }
        }]


# ============================================================
# TEMPERATURE MEASUREMENT CLUSTER (0x0402)
# ============================================================
@register_handler(0x0402)
class TemperatureMeasurementHandler(ClusterHandler):
    """
    Handles Temperature Measurement cluster (0x0402).
    Temperature is reported in centidegrees Celsius (value / 100 = °C).
    """
    CLUSTER_ID = 0x0402
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 20),  # Report every 10s-5min or 0.2°C change
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_MIN_VALUE = 0x0001
    ATTR_MAX_VALUE = 0x0002
    ATTR_TOLERANCE = 0x0003

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Temperature is in centidegrees Celsius
            if value is not None and value != 0x8000:  # 0x8000 = invalid
                temp_c = round(float(value) / 100, 2)
                self.device.update_state({"temperature": temp_c})
                logger.debug(f"[{self.device.ieee}] Temperature: {temp_c}°C")

        elif attrid == self.ATTR_TOLERANCE:
             if hasattr(value, 'value'): value = value.value
             self.device.update_state({"temperature_tolerance": value})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "temperature"
        return super().get_attr_name(attrid)

    def parse_value(self, attrid: int, value: Any) -> Any:
        if attrid == self.ATTR_MEASURED_VALUE:
            return round(float(value) / 100, 2) if value is not None else None
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_MEASURED_VALUE: "temperature",
            self.ATTR_TOLERANCE: "temperature_tolerance"
        }

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "temperature",
            "config": {
                "name": "Temperature", "device_class": "temperature", "unit_of_measurement": "°C",
                "value_template": "{{ value_json.temperature }}"
            }
        }]
# ============================================================
# ILLUMINANCE MEASUREMENT CLUSTER (0x0400)
# ============================================================
@register_handler(0x0400)
class IlluminanceMeasurementHandler(ClusterHandler):
    """
    Handles Illuminance Measurement cluster (0x0400).
    Illuminance formula: 10^((value - 1) / 10000) lux
    """
    CLUSTER_ID = 0x0400
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 500),  # Report every 10s-5min or significant change
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_MIN_VALUE = 0x0001
    ATTR_MAX_VALUE = 0x0002
    ATTR_TOLERANCE = 0x0003
    ATTR_LIGHT_SENSOR_TYPE = 0x0004

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Illuminance formula: 10^((value - 1) / 10000) lux
            if value is not None and value != 0 and value != 0xFFFF:
                try:
                    lux = round(10 ** ((float(value) - 1) / 10000), 1)
                except (ValueError, OverflowError):
                    lux = 0
            else:
                lux = 0
            self.device.update_state({"illuminance": lux, "illuminance_lux": lux})
            logger.debug(f"[{self.device.ieee}] Illuminance: {lux} lux (raw: {value})")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "illuminance"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "illuminance"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "illuminance",
            "config": {
                "name": "Illuminance", "device_class": "illuminance", "unit_of_measurement": "lx",
                "value_template": "{{ value_json.illuminance }}"
            }
        }]

# ============================================================
# RELATIVE HUMIDITY CLUSTER (0x0405)
# ============================================================
@register_handler(0x0405)
class RelativeHumidityHandler(ClusterHandler):
    """
    Handles Relative Humidity Measurement cluster (0x0405).
    Humidity is in centipercent (value / 100 = %).
    """
    CLUSTER_ID = 0x0405
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 100),  # Report every 10s-5min or 1% change
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Humidity is in centipercent
            humidity = round(float(value) / 100, 1) if value is not None else None
            self.device.update_state({"humidity": humidity})
            logger.debug(f"[{self.device.ieee}] Humidity: {humidity}%")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "humidity"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "humidity"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "humidity",
            "config": {
                "name": "Humidity", "device_class": "humidity", "unit_of_measurement": "%",
                "value_template": "{{ value_json.humidity }}"
            }
        }]

# ============================================================
# PRESSURE MEASUREMENT CLUSTER (0x0403)
# ============================================================
@register_handler(0x0403)
class PressureMeasurementHandler(ClusterHandler):
    """
    Handles Pressure Measurement cluster (0x0403).
    Pressure is in hPa (hectopascals) or millibars.
    """
    CLUSTER_ID = 0x0403
    REPORT_CONFIG = [
        ("measured_value", 10, 600, 10),
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_SCALED_VALUE = 0x0010

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid in [self.ATTR_MEASURED_VALUE, self.ATTR_SCALED_VALUE]:
            if hasattr(value, 'value'):
                value = value.value
            pressure = float(value) if value is not None else None
            self.device.update_state({"pressure": pressure})
            logger.debug(f"[{self.device.ieee}] Pressure: {pressure} hPa")

    def get_attr_name(self, attrid: int) -> str:
        if attrid in [self.ATTR_MEASURED_VALUE, self.ATTR_SCALED_VALUE]:
            return "pressure"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "pressure"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "pressure",
            "config": {
                "name": "Pressure", "device_class": "pressure", "unit_of_measurement": "hPa",
                "value_template": "{{ value_json.pressure }}"
            }
        }]


# ============================================================
# CO2 MEASUREMENT CLUSTER (0x040D)
# ============================================================
@register_handler(0x040D)
class CO2MeasurementHandler(ClusterHandler):
    """Handles Carbon Dioxide Concentration Measurement cluster (0x040D)."""
    CLUSTER_ID = 0x040D
    REPORT_CONFIG = [
        ("measured_value", 30, 600, 50),  # Report every 30s-10min or 50ppm change
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            co2 = float(value) if value is not None else None
            self.device.update_state({"co2": co2})
            logger.debug(f"[{self.device.ieee}] CO2: {co2} ppm")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "co2"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "co2"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "co2",
            "config": {
                "name": "CO2", "device_class": "co2", "unit_of_measurement": "ppm",
                "value_template": "{{ value_json.co2 }}"
            }
        }]

# ============================================================
# PM2.5 MEASUREMENT CLUSTER (0x042A)
# ============================================================
@register_handler(0x042A)
class PM25MeasurementHandler(ClusterHandler):
    """Handles PM2.5 Concentration Measurement cluster (0x042A)."""
    CLUSTER_ID = 0x042A
    REPORT_CONFIG = [
        ("measured_value", 30, 600, 5),
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            pm25 = float(value) if value is not None else None
            self.device.update_state({"pm25": pm25})
            logger.debug(f"[{self.device.ieee}] PM2.5: {pm25} µg/m³")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "pm25"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "pm25"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "pm25",
            "config": {
                "name": "PM2.5", "device_class": "pm25", "unit_of_measurement": "µg/m³",
                "value_template": "{{ value_json.pm25 }}"
            }
        }]

# ============================================================
# FORMALDEHYDE MEASUREMENT CLUSTER (0x042B)
# ============================================================
@register_handler(0x042B)
class FormaldehydeMeasurementHandler(ClusterHandler):
    """Handles Formaldehyde Concentration Measurement cluster (0x042B)."""
    CLUSTER_ID = 0x042B

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            formaldehyde = float(value) if value is not None else None
            self.device.update_state({"formaldehyde": formaldehyde})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "formaldehyde"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "formaldehyde"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "formaldehyde",
            "config": {
                "name": "Formaldehyde", "device_class": "formaldehyde", "unit_of_measurement": "mg/m³",
                "value_template": "{{ value_json.formaldehyde }}"
            }
        }]

# ============================================================
# VOC MEASUREMENT CLUSTER (0x042E)
# ============================================================
@register_handler(0x042E)
class VOCMeasurementHandler(ClusterHandler):
    """Handles VOC (Volatile Organic Compound) Measurement cluster."""
    CLUSTER_ID = 0x042E

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            voc = float(value) if value is not None else None
            self.device.update_state({"voc": voc})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "voc"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "voc"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "voc",
            "config": {
                "name": "VoC", "device_class": "voc", "unit_of_measurement": "µg/m³",
                "value_template": "{{ value_json.voc }}"
            }
        }]
# ============================================================
# POWER CONFIGURATION CLUSTER (0x0001)
# Battery status for battery-powered devices
# ============================================================
@register_handler(0x0001)
class PowerConfigurationHandler(ClusterHandler):
    """
    Handles Power Configuration cluster (0x0001) - Battery info.
    Battery percentage is reported as 0-200 (0.5% steps).
    """
    CLUSTER_ID = 0x0001
    REPORT_CONFIG = [
        ("battery_percentage_remaining", 3600, 21600, 2),  # Every 1-6 hours or 1% change
    ]

    ATTR_BATTERY_VOLTAGE = 0x0020
    ATTR_BATTERY_PERCENTAGE = 0x0021

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if hasattr(value, 'value'):
            value = value.value

        if attrid == self.ATTR_BATTERY_VOLTAGE:
            # Voltage in 100mV units. Value of 54 means 5.4V.
            voltage = float(value) / 10 if value is not None else None
            self.device.update_state({"battery_voltage": voltage})
            logger.debug(f"[{self.device.ieee}] Battery voltage: {voltage}V (raw: {value})")

        elif attrid == self.ATTR_BATTERY_PERCENTAGE:
            # Percentage is 0-200 (0.5% steps), divide by 2. Value of 170 means 85%.
            percentage = min(100, round(value / 2)) if value is not None else None
            self.device.update_state({"battery": percentage})
            logger.debug(f"[{self.device.ieee}] Battery: {percentage}% (raw: {value})")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_BATTERY_VOLTAGE:
            return "battery_voltage"
        if attrid == self.ATTR_BATTERY_PERCENTAGE:
            return "battery"
        return super().get_attr_name(attrid)

    def parse_value(self, attrid: int, value: Any) -> Any:
        """Parse raw values before they hit device state from generic poller."""
        if attrid == self.ATTR_BATTERY_VOLTAGE:
             return float(value) / 10 if value is not None else None
        if attrid == self.ATTR_BATTERY_PERCENTAGE:
             return min(100, round(value / 2)) if value is not None else None
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_BATTERY_PERCENTAGE: "battery",
            self.ATTR_BATTERY_VOLTAGE: "battery_voltage"
        }

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "battery",
            "config": {
                "name": "Battery", "device_class": "battery", "unit_of_measurement": "%",
                "value_template": "{{ value_json.battery }}"
            }
        }]

# ============================================================
# OCCUPANCY SENSING CLUSTER (0x0406)
# Used by: Philips Hue Motion Sensors, Aqara Motion Sensors
# This is the STANDARD way occupancy sensors report occupancy.
# ============================================================
@register_handler(0x0406)
class OccupancySensingHandler(ClusterHandler):
    """
    Handles Occupancy Sensing cluster (0x0406).
    """
    CLUSTER_ID = 0x0406
    REPORT_CONFIG = [
        ("occupancy", 0, 300, 1),  # Report immediately on change, max 5 min
    ]

    # Occupancy attribute IDs
    ATTR_OCCUPANCY = 0x0000
    ATTR_OCCUPANCY_TYPE = 0x0001
    ATTR_PIR_O_TO_U_DELAY = 0x0010  # Occupied to Unoccupied delay (seconds)
    ATTR_PIR_U_TO_O_DELAY = 0x0011  # Unoccupied to Occupied delay (seconds)
    ATTR_PIR_U_TO_O_THRESHOLD = 0x0012

    # Philips Hue Specific
    ATTR_SENSITIVITY = 0x0030
    ATTR_SENSITIVITY_MAX = 0x0031

    # Occupancy types
    OCCUPANCY_TYPES = {
        0: "PIR",
        1: "Ultrasonic",
        2: "PIR+Ultrasonic",
        3: "PhysicalContact"
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """Handle occupancy attribute updates."""
        try:
            # Handle wrapped types
            if hasattr(value, 'value'):
                value = value.value

            if attrid == self.ATTR_OCCUPANCY:
                # Occupancy is a bitmap, bit 0 = occupied
                is_occupied = bool(value & 0x01) if isinstance(value, int) else bool(value)

                self.device.update_state({
                    "occupancy": is_occupied,
                    "motion": is_occupied,
                    "presence": is_occupied,
                })

                # === FAST-PATH PUBLISH ===
                # Use non-blocking publish for immediate MQTT update
                if self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                    safe_name = self.device.service.get_safe_name(self.device.ieee)
                    payload = json.dumps({
                        'occupancy': is_occupied,
                        'motion': is_occupied,
                        'presence': is_occupied
                    })
                    self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)
                # === END FAST-PATH PUBLISH ===

                status = "MOTION DETECTED" if is_occupied else "Motion cleared"
                logger.info(f"[{self.device.ieee}] Occupancy Sensing: {status}")

            elif attrid == self.ATTR_OCCUPANCY_TYPE:
                type_name = self.OCCUPANCY_TYPES.get(value, f"Unknown({value})")
                self.device.update_state({"occupancy_type": type_name})

            elif attrid == self.ATTR_PIR_O_TO_U_DELAY:
                self.device.update_state({"motion_timeout": value, "pir_o_to_u_delay": value})

            elif attrid == self.ATTR_SENSITIVITY:
                self.device.update_state({"sensitivity": value})

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error processing occupancy attribute: {e}")

    def get_attr_name(self, attrid: int) -> str:
        names = {
            self.ATTR_OCCUPANCY: "occupancy",
            self.ATTR_OCCUPANCY_TYPE: "occupancy_type",
            self.ATTR_PIR_O_TO_U_DELAY: "motion_timeout",
            self.ATTR_SENSITIVITY: "sensitivity"
        }
        return names.get(attrid, super().get_attr_name(attrid))

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_OCCUPANCY: "occupancy",
            self.ATTR_PIR_O_TO_U_DELAY: "motion_timeout",
            self.ATTR_SENSITIVITY: "sensitivity"
        }

    async def configure(self):
        """
        Configure occupancy sensing cluster.

        Override base configure() to also read configuration values from device
        (timeout, sensitivity) so they can be displayed in the UI.
        """
        # First do standard binding and reporting configuration
        await super().configure()

        # Now read configuration attributes
        try:
            # Read PIR timeout (how long motion stays active)
            try:
                result = await self.cluster.read_attributes([self.ATTR_PIR_O_TO_U_DELAY])
                if result and self.ATTR_PIR_O_TO_U_DELAY in result[0]:
                    timeout = result[0][self.ATTR_PIR_O_TO_U_DELAY]
                    if hasattr(timeout, 'value'):
                        timeout = timeout.value
                    self.device.update_state({
                        "motion_timeout": timeout,
                        "pir_o_to_u_delay": timeout
                    })
                    logger.info(f"[{self.device.ieee}] Read motion timeout: {timeout}s")
            except Exception as e:
                logger.debug(f"[{self.device.ieee}] Could not read motion timeout: {e}")

            # Read sensitivity (Philips/Aqara specific)
            man = (self.device.zigpy_dev.manufacturer or "").lower()
            if 'philips' in man or 'lumi' in man or 'signify' in man:
                try:
                    result = await self.cluster.read_attributes([self.ATTR_SENSITIVITY])
                    if result and self.ATTR_SENSITIVITY in result[0]:
                        sensitivity = result[0][self.ATTR_SENSITIVITY]
                        if hasattr(sensitivity, 'value'):
                            sensitivity = sensitivity.value
                        self.device.update_state({"sensitivity": sensitivity})
                        logger.info(f"[{self.device.ieee}] Read motion sensitivity: {sensitivity}")
                except Exception as e:
                    logger.debug(f"[{self.device.ieee}] Could not read sensitivity: {e}")

        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Error reading occupancy config: {e}")

        return True

    # --- DYNAMIC CONFIGURATION EXPOSURE ---
    def get_configuration_options(self) -> List[Dict]:
        """Expose supported configurations to the frontend."""
        options = [
            {
                "name": "motion_timeout",
                "label": "Motion Timeout (s)",
                "type": "number",
                "min": 0, "max": 65535,
                "description": "Hardware timeout for PIR reset (O->U Delay)",
                "attribute_id": self.ATTR_PIR_O_TO_U_DELAY,
                "current_value": self.device.state.get("motion_timeout", 60)  # Show current value
            }
        ]

        # Only expose sensitivity if we think it's supported (Hue/Aqara)
        man = (self.device.zigpy_dev.manufacturer or "").lower()
        if 'philips' in man or 'lumi' in man or 'signify' in man:
            options.append({
                "name": "sensitivity",
                "label": "Motion Sensitivity",
                "type": "number",
                "min": 0, "max": 2,
                "description": "0=Low, 1=Medium, 2=High",
                "attribute_id": self.ATTR_SENSITIVITY,
                "current_value": self.device.state.get("sensitivity", 1)  # Show current value
            })

        return options

    async def apply_settings(self, settings: Dict[str, Any]):
        """
        Apply configuration changes from the frontend.

        This is called when user changes settings in the UI.
        """
        try:
            # Handle motion_timeout
            if "motion_timeout" in settings:
                timeout = int(settings["motion_timeout"])
                await self.cluster.write_attributes({"pir_o_to_u_delay": timeout})
                self.device.update_state({
                    "motion_timeout": timeout,
                    "pir_o_to_u_delay": timeout
                })
                logger.info(f"[{self.device.ieee}] Set motion timeout: {timeout}s")

            # Handle sensitivity
            if "sensitivity" in settings:
                sensitivity = int(settings["sensitivity"])
                # Philips uses attribute 0x0030
                await self.cluster.write_attributes({self.ATTR_SENSITIVITY: sensitivity})
                self.device.update_state({"sensitivity": sensitivity})
                logger.info(f"[{self.device.ieee}] Set sensitivity: {sensitivity}")

            return True

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Failed to apply occupancy settings: {e}")
            return False


    # --- HA Discovery ---
    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "binary_sensor",
            "object_id": "motion",
            "config": {
                "name": "Motion",
                "device_class": "motion",
                "value_template": "{{ 'ON' if value_json.occupancy else 'OFF' }}"
            }
        }]


# ============================================================
# TEMPERATURE MEASUREMENT CLUSTER (0x0402)
# ============================================================
@register_handler(0x0402)
class TemperatureMeasurementHandler(ClusterHandler):
    """
    Handles Temperature Measurement cluster (0x0402).
    Temperature is reported in centidegrees Celsius (value / 100 = °C).
    """
    CLUSTER_ID = 0x0402
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 20),  # Report every 10s-5min or 0.2°C change
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_MIN_VALUE = 0x0001
    ATTR_MAX_VALUE = 0x0002
    ATTR_TOLERANCE = 0x0003

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Temperature is in centidegrees Celsius
            if value is not None and value != 0x8000:  # 0x8000 = invalid
                temp_c = round(float(value) / 100, 2)
                self.device.update_state({"temperature": temp_c})
                logger.debug(f"[{self.device.ieee}] Temperature: {temp_c}°C")

        elif attrid == self.ATTR_TOLERANCE:
             if hasattr(value, 'value'): value = value.value
             self.device.update_state({"temperature_tolerance": value})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "temperature"
        return super().get_attr_name(attrid)

    def parse_value(self, attrid: int, value: Any) -> Any:
        if attrid == self.ATTR_MEASURED_VALUE:
            return round(float(value) / 100, 2) if value is not None else None
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_MEASURED_VALUE: "temperature",
            self.ATTR_TOLERANCE: "temperature_tolerance"
        }

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "temperature",
            "config": {
                "name": "Temperature", "device_class": "temperature", "unit_of_measurement": "°C",
                "value_template": "{{ value_json.temperature }}"
            }
        }]
# ============================================================
# ILLUMINANCE MEASUREMENT CLUSTER (0x0400)
# ============================================================
@register_handler(0x0400)
class IlluminanceMeasurementHandler(ClusterHandler):
    """
    Handles Illuminance Measurement cluster (0x0400).
    Illuminance formula: 10^((value - 1) / 10000) lux
    """
    CLUSTER_ID = 0x0400
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 500),  # Report every 10s-5min or significant change
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_MIN_VALUE = 0x0001
    ATTR_MAX_VALUE = 0x0002
    ATTR_TOLERANCE = 0x0003
    ATTR_LIGHT_SENSOR_TYPE = 0x0004

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Illuminance formula: 10^((value - 1) / 10000) lux
            if value is not None and value != 0 and value != 0xFFFF:
                try:
                    lux = round(10 ** ((float(value) - 1) / 10000), 1)
                except (ValueError, OverflowError):
                    lux = 0
            else:
                lux = 0
            self.device.update_state({"illuminance": lux, "illuminance_lux": lux})
            logger.debug(f"[{self.device.ieee}] Illuminance: {lux} lux (raw: {value})")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "illuminance"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "illuminance"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "illuminance",
            "config": {
                "name": "Illuminance", "device_class": "illuminance", "unit_of_measurement": "lx",
                "value_template": "{{ value_json.illuminance }}"
            }
        }]

# ============================================================
# RELATIVE HUMIDITY CLUSTER (0x0405)
# ============================================================
@register_handler(0x0405)
class RelativeHumidityHandler(ClusterHandler):
    """
    Handles Relative Humidity Measurement cluster (0x0405).
    Humidity is in centipercent (value / 100 = %).
    """
    CLUSTER_ID = 0x0405
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 100),  # Report every 10s-5min or 1% change
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            # Humidity is in centipercent
            humidity = round(float(value) / 100, 1) if value is not None else None
            self.device.update_state({"humidity": humidity})
            logger.debug(f"[{self.device.ieee}] Humidity: {humidity}%")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "humidity"
        return super().get_attr_name(attrid)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "humidity"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "humidity",
            "config": {
                "name": "Humidity", "device_class": "humidity", "unit_of_measurement": "%",
                "value_template": "{{ value_json.humidity }}"
            }
        }]

# ============================================================
# PRESSURE MEASUREMENT CLUSTER (0x0403)
# ============================================================
@register_handler(0x0403)
class PressureMeasurementHandler(ClusterHandler):
    """
    Handles Pressure Measurement cluster (0x0403).
    Pressure is in hPa (hectopascals) or millibars.
    """
    CLUSTER_ID = 0x0403
    REPORT_CONFIG = [
        ("measured_value", 10, 600, 10),
    ]

    ATTR_MEASURED_VALUE = 0x0000
    ATTR_SCALED_VALUE = 0x0010

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid in [self.ATTR_MEASURED_VALUE, self.ATTR_SCALED_VALUE]:
            if hasattr(value, 'value'):
                value = value.value
            pressure = float(value) if value is not None else None
            self.device.update_state({"pressure": pressure})
            logger.debug(f"[{self.device.ieee}] Pressure: {pressure} hPa")

    def get_attr_name(self, attrid: int) -> str:
        if attrid in [self.ATTR_MEASURED_VALUE, self.ATTR_SCALED_VALUE]:
            return "pressure"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "pressure"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "pressure",
            "config": {
                "name": "Pressure", "device_class": "pressure", "unit_of_measurement": "hPa",
                "value_template": "{{ value_json.pressure }}"
            }
        }]


# ============================================================
# CO2 MEASUREMENT CLUSTER (0x040D)
# ============================================================
@register_handler(0x040D)
class CO2MeasurementHandler(ClusterHandler):
    """Handles Carbon Dioxide Concentration Measurement cluster (0x040D)."""
    CLUSTER_ID = 0x040D
    REPORT_CONFIG = [
        ("measured_value", 30, 600, 50),  # Report every 30s-10min or 50ppm change
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            co2 = float(value) if value is not None else None
            self.device.update_state({"co2": co2})
            logger.debug(f"[{self.device.ieee}] CO2: {co2} ppm")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "co2"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "co2"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "co2",
            "config": {
                "name": "CO2", "device_class": "co2", "unit_of_measurement": "ppm",
                "value_template": "{{ value_json.co2 }}"
            }
        }]

# ============================================================
# PM2.5 MEASUREMENT CLUSTER (0x042A)
# ============================================================
@register_handler(0x042A)
class PM25MeasurementHandler(ClusterHandler):
    """Handles PM2.5 Concentration Measurement cluster (0x042A)."""
    CLUSTER_ID = 0x042A
    REPORT_CONFIG = [
        ("measured_value", 30, 600, 5),
    ]

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            pm25 = float(value) if value is not None else None
            self.device.update_state({"pm25": pm25})
            logger.debug(f"[{self.device.ieee}] PM2.5: {pm25} µg/m³")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "pm25"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "pm25"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "pm25",
            "config": {
                "name": "PM2.5", "device_class": "pm25", "unit_of_measurement": "µg/m³",
                "value_template": "{{ value_json.pm25 }}"
            }
        }]

# ============================================================
# FORMALDEHYDE MEASUREMENT CLUSTER (0x042B)
# ============================================================
@register_handler(0x042B)
class FormaldehydeMeasurementHandler(ClusterHandler):
    """Handles Formaldehyde Concentration Measurement cluster (0x042B)."""
    CLUSTER_ID = 0x042B

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            formaldehyde = float(value) if value is not None else None
            self.device.update_state({"formaldehyde": formaldehyde})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "formaldehyde"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "formaldehyde"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "formaldehyde",
            "config": {
                "name": "Formaldehyde", "device_class": "formaldehyde", "unit_of_measurement": "mg/m³",
                "value_template": "{{ value_json.formaldehyde }}"
            }
        }]

# ============================================================
# VOC MEASUREMENT CLUSTER (0x042E)
# ============================================================
@register_handler(0x042E)
class VOCMeasurementHandler(ClusterHandler):
    """Handles VOC (Volatile Organic Compound) Measurement cluster."""
    CLUSTER_ID = 0x042E

    ATTR_MEASURED_VALUE = 0x0000

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if attrid == self.ATTR_MEASURED_VALUE:
            if hasattr(value, 'value'):
                value = value.value
            voc = float(value) if value is not None else None
            self.device.update_state({"voc": voc})

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_MEASURED_VALUE:
            return "voc"
        return super().get_attr_name(attrid)


    def get_pollable_attributes(self) -> Dict[int, str]:
        return {self.ATTR_MEASURED_VALUE: "voc"}

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "voc",
            "config": {
                "name": "VoC", "device_class": "voc", "unit_of_measurement": "µg/m³",
                "value_template": "{{ value_json.voc }}"
            }
        }]
# ============================================================
# POWER CONFIGURATION CLUSTER (0x0001)
# Battery status for battery-powered devices
# ============================================================
@register_handler(0x0001)
class PowerConfigurationHandler(ClusterHandler):
    """
    Handles Power Configuration cluster (0x0001) - Battery info.
    Battery percentage is reported as 0-200 (0.5% steps).
    """
    CLUSTER_ID = 0x0001
    REPORT_CONFIG = [
        ("battery_percentage_remaining", 3600, 21600, 2),  # Every 1-6 hours or 1% change
    ]

    ATTR_BATTERY_VOLTAGE = 0x0020
    ATTR_BATTERY_PERCENTAGE = 0x0021

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        if hasattr(value, 'value'):
            value = value.value

        if attrid == self.ATTR_BATTERY_VOLTAGE:
            # Voltage in 100mV units. Value of 54 means 5.4V.
            voltage = float(value) / 10 if value is not None else None
            self.device.update_state({"battery_voltage": voltage})
            logger.debug(f"[{self.device.ieee}] Battery voltage: {voltage}V (raw: {value})")

        elif attrid == self.ATTR_BATTERY_PERCENTAGE:
            # Percentage is 0-200 (0.5% steps), divide by 2. Value of 170 means 85%.
            percentage = min(100, round(value / 2)) if value is not None else None
            self.device.update_state({"battery": percentage})
            logger.debug(f"[{self.device.ieee}] Battery: {percentage}% (raw: {value})")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_BATTERY_VOLTAGE:
            return "battery_voltage"
        if attrid == self.ATTR_BATTERY_PERCENTAGE:
            return "battery"
        return super().get_attr_name(attrid)

    def parse_value(self, attrid: int, value: Any) -> Any:
        """Parse raw values before they hit device state from generic poller."""
        if attrid == self.ATTR_BATTERY_VOLTAGE:
             return float(value) / 10 if value is not None else None
        if attrid == self.ATTR_BATTERY_PERCENTAGE:
             return min(100, round(value / 2)) if value is not None else None
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_BATTERY_PERCENTAGE: "battery",
            self.ATTR_BATTERY_VOLTAGE: "battery_voltage"
        }

    def get_discovery_configs(self) -> List[Dict]:
        return [{
            "component": "sensor", "object_id": "battery",
            "config": {
                "name": "Battery", "device_class": "battery", "unit_of_measurement": "%",
                "value_template": "{{ value_json.battery }}"
            }
        }]
