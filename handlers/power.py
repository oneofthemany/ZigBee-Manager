"""
Power cluster handlers.
Handles: Electrical Measurement (0x0B04), Metering (0x0702)
"""
import logging
from typing import Any, Dict, List

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.power")

# ============================================================
# ELECTRICAL MEASUREMENT CLUSTER (0x0B04)
# ============================================================
@register_handler(0x0B04)
class ElectricalMeasurementHandler(ClusterHandler):
    CLUSTER_ID = 0x0B04
    REPORT_CONFIG = [("active_power", 10, 60, 10), ("rms_voltage", 60, 600, 5), ("rms_current", 10, 60, 100)]

    ATTR_ACTIVE_POWER          = 0x050B
    ATTR_RMS_VOLTAGE           = 0x0505
    ATTR_RMS_CURRENT           = 0x0508
    ATTR_AC_VOLTAGE_MULTIPLIER = 0x0600
    ATTR_AC_VOLTAGE_DIVISOR    = 0x0601
    ATTR_AC_CURRENT_MULTIPLIER = 0x0602
    ATTR_AC_CURRENT_DIVISOR    = 0x0603
    ATTR_AC_POWER_MULTIPLIER   = 0x0604
    ATTR_AC_POWER_DIVISOR      = 0x0605

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._power_multiplier   = 1
        self._power_divisor      = 1
        self._voltage_multiplier = 1
        self._voltage_divisor    = 1
        self._current_multiplier = 1
        self._current_divisor    = 1000

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None:
            return
        ep_id = self.endpoint.endpoint_id
        updates = {}

        if attrid == self.ATTR_ACTIVE_POWER:
            val = round(float(value) * self._power_multiplier / self._power_divisor, 1)
            updates[f"power_{ep_id}"] = val

        elif attrid == self.ATTR_RMS_VOLTAGE:
            val = round(float(value) * self._voltage_multiplier / self._voltage_divisor, 1)
            updates[f"voltage_{ep_id}"] = val

        elif attrid == self.ATTR_RMS_CURRENT:
            val = round(float(value) * self._current_multiplier / self._current_divisor, 3)
            updates[f"current_{ep_id}"] = val

        elif attrid == self.ATTR_AC_POWER_MULTIPLIER:   self._power_multiplier   = value or 1
        elif attrid == self.ATTR_AC_POWER_DIVISOR:      self._power_divisor      = value or 1
        elif attrid == self.ATTR_AC_VOLTAGE_MULTIPLIER: self._voltage_multiplier = value or 1
        elif attrid == self.ATTR_AC_VOLTAGE_DIVISOR:    self._voltage_divisor    = value or 1
        elif attrid == self.ATTR_AC_CURRENT_MULTIPLIER: self._current_multiplier = value or 1
        elif attrid == self.ATTR_AC_CURRENT_DIVISOR:    self._current_divisor    = value or 1

        if updates:
            self.device.update_state(updates)

    def parse_value(self, attr_id: int, value: Any) -> Any:
        if attr_id == self.ATTR_ACTIVE_POWER:
            return round(float(value) * self._power_multiplier / self._power_divisor, 1)
        elif attr_id == self.ATTR_RMS_VOLTAGE:
            return round(float(value) * self._voltage_multiplier / self._voltage_divisor, 1)
        elif attr_id == self.ATTR_RMS_CURRENT:
            return round(float(value) * self._current_multiplier / self._current_divisor, 3)
        return value

    async def configure(self):
        await super().configure()
        try:
            result = await self.cluster.read_attributes([
                'ac_voltage_multiplier', 'ac_voltage_divisor',
                'ac_current_multiplier', 'ac_current_divisor',
                'ac_power_multiplier',   'ac_power_divisor',
            ])
            logger.info(f"[{self.device.ieee}] EM raw scaling result: {result}")
            if result and result[0]:
                data = result[0]
                self._voltage_multiplier = data.get('ac_voltage_multiplier', 1) or 1
                self._voltage_divisor    = data.get('ac_voltage_divisor',    1) or 1
                self._current_multiplier = data.get('ac_current_multiplier', 1) or 1
                self._current_divisor    = data.get('ac_current_divisor',    1) or 1
                self._power_multiplier   = data.get('ac_power_multiplier',   1) or 1
                self._power_divisor      = data.get('ac_power_divisor',      1) or 1
                logger.info(
                    f"[{self.device.ieee}] EM scaling — "
                    f"V: {self._voltage_multiplier}/{self._voltage_divisor}, "
                    f"I: {self._current_multiplier}/{self._current_divisor}, "
                    f"P: {self._power_multiplier}/{self._power_divisor}"
                )
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to read EM scaling attrs: {e}", exc_info=True)

    def get_pollable_attributes(self) -> Dict[int, str]:
        ep = self.endpoint.endpoint_id
        return {
            self.ATTR_ACTIVE_POWER: f"power_{ep}",
            self.ATTR_RMS_VOLTAGE:  f"voltage_{ep}",
            self.ATTR_RMS_CURRENT:  f"current_{ep}",
        }

    def get_discovery_configs(self) -> List[Dict]:
        ep = self.endpoint.endpoint_id
        return [
            {"component": "sensor", "object_id": f"power_{ep}",   "config": {"name": f"Power {ep}",   "device_class": "power",   "unit_of_measurement": "W",  "value_template": f"{{{{ value_json.power_{ep} }}}}"}},
            {"component": "sensor", "object_id": f"voltage_{ep}", "config": {"name": f"Voltage {ep}", "device_class": "voltage", "unit_of_measurement": "V",  "value_template": f"{{{{ value_json.voltage_{ep} }}}}"}},
            {"component": "sensor", "object_id": f"current_{ep}", "config": {"name": f"Current {ep}", "device_class": "current", "unit_of_measurement": "A",  "value_template": f"{{{{ value_json.current_{ep} }}}}"}}
        ]

# ============================================================
# METERING CLUSTER (0x0702)
# ============================================================
@register_handler(0x0702)
class MeteringHandler(ClusterHandler):
    CLUSTER_ID = 0x0702
    REPORT_CONFIG = [("instantaneous_demand", 30, 300, 10), ("current_summation_delivered", 300, 3600, 100)]

    ATTR_CURRENT_SUMMATION_DELIVERED = 0x0000
    ATTR_INSTANTANEOUS_DEMAND = 0x0400
    ATTR_MULTIPLIER = 0x0301
    ATTR_DIVISOR = 0x0302

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._multiplier = 1
        self._divisor = 1

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None: return
        ep_id = self.endpoint.endpoint_id
        updates = {}

        if attrid == self.ATTR_CURRENT_SUMMATION_DELIVERED:
            val = round(float(value) * self._multiplier / self._divisor, 3)
            updates[f"energy_{ep_id}"] = val
            if ep_id == 1: updates["energy"] = val

        elif attrid == self.ATTR_INSTANTANEOUS_DEMAND:
            val = round(float(value) * self._multiplier / self._divisor, 1)
            updates[f"power_demand_{ep_id}"] = val

        elif attrid == self.ATTR_MULTIPLIER:
            self._multiplier = value or 1
        elif attrid == self.ATTR_DIVISOR:
            self._divisor = value or 1

        if updates: self.device.update_state(updates)

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_CURRENT_SUMMATION_DELIVERED: "energy",
            self.ATTR_INSTANTANEOUS_DEMAND: "instantaneous_demand",
        }

    def get_discovery_configs(self) -> List[Dict]:
        ep = self.endpoint.endpoint_id
        return [{
            "component": "sensor", "object_id": f"energy_{ep}",
            "config": {
                "name": f"Energy {ep}", "device_class": "energy", "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
                "value_template": f"{{{{ value_json.energy_{ep} }}}}"
            }
        }]
