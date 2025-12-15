"""
Security cluster handlers for Zigbee devices.
Handles: IAS Zone (motion, door/window, smoke, leak sensors)

This is the PRIMARY handler for motion sensors that use IAS Zone cluster (0x0500).
Many sensors (including Philips Hue motion sensors) send motion events via
Zone Status Change Notification commands, NOT via attribute reports.
"""
import logging
from typing import Any, Dict, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.security")


# IAS Zone Status Bits
class ZoneStatus:
    """IAS Zone Status bitmap values."""
    ALARM1 = 0x0001       # Motion detected / Door open / Alarm triggered
    ALARM2 = 0x0002       # Secondary alarm
    TAMPER = 0x0004       # Tamper detected
    BATTERY_LOW = 0x0008  # Low battery
    SUPERVISION = 0x0010  # Supervision reports
    RESTORE = 0x0020      # Restore reports
    TROUBLE = 0x0040      # Trouble/failure
    AC_MAINS = 0x0080     # AC mains fault
    TEST = 0x0100         # Test mode
    BATTERY_DEFECT = 0x0200  # Battery defect


# IAS Zone Types
ZONE_TYPES = {
    0x0000: "standard_cie",
    0x000D: "motion_sensor",
    0x0015: "contact_switch",
    0x0028: "fire_sensor",
    0x002A: "water_sensor",
    0x002B: "co_sensor",
    0x002C: "vibration_sensor",
    0x002D: "remote_control",
    0x010F: "key_fob",
    0x0115: "keypad",
    0x021D: "standard_warning",
    0x0225: "glass_break",
    0x0226: "security_repeater",
}


@register_handler(0x0500)
class IASZoneHandler(ClusterHandler):
    """
    Handles IAS Zone cluster (0x0500).
    
    This is the CORRECT handler for motion sensors, door/window contacts,
    smoke detectors, and other security sensors.
    
    KEY INSIGHT: Motion sensors send Zone Status Change Notification COMMANDS,
    not attribute reports. This is handled in cluster_command(), not attribute_updated().
    
    Supported devices:
    - Philips Hue Motion Sensor (SML001, SML002, SML003, SML004)
    - Aqara Motion Sensors
    - Samsung SmartThings Motion Sensors
    - Door/Window contacts
    - Smoke/Leak detectors
    """
    CLUSTER_ID = 0x0500

    # IAS Zone Attributes
    ATTR_ZONE_STATE = 0x0000
    ATTR_ZONE_TYPE = 0x0001
    ATTR_ZONE_STATUS = 0x0002
    ATTR_CIE_ADDR = 0x0010
    ATTR_ZONE_ID = 0x0011

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._zone_type = None
        self._zone_id = None
        self._last_status = 0
        
        logger.info(f"[{self.device.ieee}] IAS Zone handler initialized")

    def cluster_command(self, tsn: int, command_id: int, args):
        """
        Handle IAS Zone cluster commands.
        
        Command 0x00: Zone Status Change Notification
        This is how motion sensors report motion - NOT via attribute updates!
        
        Command 0x01: Zone Enroll Request
        Device wants to enroll with the CIE (coordinator).
        """
        logger.info(f"[{self.device.ieee}] IAS Zone command received: "
                   f"cmd=0x{command_id:02x}, tsn={tsn}, args={args}")

        if command_id == 0x00:
            # Zone Status Change Notification - THIS IS THE MOTION EVENT
            self._handle_zone_status_change(args)
            
        elif command_id == 0x01:
            # Zone Enroll Request
            logger.info(f"[{self.device.ieee}] IAS Zone Enroll Request received")
            # We should respond with enroll response, but this is usually
            # handled during initial configuration
            
        else:
            logger.debug(f"[{self.device.ieee}] Unknown IAS Zone command: 0x{command_id:02x}")

    def _handle_zone_status_change(self, args):
        """
        Process Zone Status Change Notification.
        This is the MAIN method that detects motion events.
        
        Zone Status is a 16-bit bitmap:
        - Bit 0 (0x0001): Alarm 1 - Motion detected OR Door open
        - Bit 1 (0x0002): Alarm 2 - Secondary alarm
        - Bit 2 (0x0004): Tamper
        - Bit 3 (0x0008): Battery low
        - Bit 4 (0x0010): Supervision reports
        - Bit 5 (0x0020): Restore reports
        - Bit 6 (0x0040): Trouble
        - Bit 7 (0x0080): AC (Mains) fault
        """
        try:
            if not args:
                logger.warning(f"[{self.device.ieee}] Empty zone status change args")
                return

            # Extract zone status from args
            # Args format: (zone_status, extended_status, zone_id, delay)
            # or just: (zone_status,) depending on device
            status = args[0]
            
            # Handle zigpy wrapped types
            if hasattr(status, 'value'):
                status = status.value
            elif hasattr(status, '__int__'):
                status = int(status)
            else:
                try:
                    status = int(status)
                except:
                    logger.error(f"[{self.device.ieee}] Cannot parse zone status: {status}")
                    return

            self._last_status = status

            # Parse the status bitmap
            alarm1 = bool(status & ZoneStatus.ALARM1)
            alarm2 = bool(status & ZoneStatus.ALARM2)
            tamper = bool(status & ZoneStatus.TAMPER)
            battery_low = bool(status & ZoneStatus.BATTERY_LOW)
            trouble = bool(status & ZoneStatus.TROUBLE)

            # Build state update based on zone type
            updates = {
                "zone_status": status,
                "tamper": tamper,
                "battery_low": battery_low,
                "trouble": trouble,
            }

            # Determine device type and set appropriate state keys
            zone_type = self._zone_type or 0x000D  # Default to motion sensor

            if zone_type == 0x0015:
                # Contact switch (door/window sensor)
                # Alarm1 = True means OPEN (magnet separated)
                updates["contact"] = not alarm1  # True = closed, False = open
                updates["is_open"] = alarm1
                log_msg = f"OPEN" if alarm1 else "CLOSED"
                
            elif zone_type in [0x000D, 0x0000]:
                # Motion sensor or standard CIE
                # Alarm1 = True means MOTION DETECTED
                updates["occupancy"] = alarm1
                updates["motion"] = alarm1
                updates["presence"] = alarm1
                log_msg = "MOTION DETECTED" if alarm1 else "Motion cleared"
                
            elif zone_type == 0x002A:
                # Water/Leak sensor
                updates["water_leak"] = alarm1
                log_msg = "LEAK DETECTED" if alarm1 else "Leak cleared"
                
            elif zone_type == 0x0028:
                # Fire/Smoke sensor
                updates["smoke"] = alarm1
                log_msg = "SMOKE DETECTED" if alarm1 else "Smoke cleared"
                
            elif zone_type == 0x002B:
                # CO sensor
                updates["co_detected"] = alarm1
                log_msg = "CO DETECTED" if alarm1 else "CO cleared"
                
            elif zone_type == 0x002C:
                # Vibration sensor
                updates["vibration"] = alarm1
                log_msg = "VIBRATION" if alarm1 else "Vibration stopped"
                
            else:
                # Generic fallback
                updates["alarm"] = alarm1
                updates["occupancy"] = alarm1
                updates["motion"] = alarm1
                log_msg = f"Alarm: {alarm1}"

            # Update device state
            self.device.update_state(updates)
            
            logger.info(f"[{self.device.ieee}] IAS Zone: {log_msg} "
                       f"(status=0x{status:04x}, tamper={tamper}, battery_low={battery_low})")

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Failed to parse IAS zone status: {e}")
            import traceback
            traceback.print_exc()

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """
        Handle IAS Zone attribute updates.
        Zone Status can also be reported via attribute (0x0002).
        """
        try:
            # Handle wrapped types
            if hasattr(value, 'value'):
                value = value.value

            if attrid == self.ATTR_ZONE_TYPE:
                # Zone type tells us what kind of sensor this is
                self._zone_type = value
                type_name = ZONE_TYPES.get(value, f"unknown_0x{value:04x}")
                self.device.update_state({"zone_type": type_name})
                logger.info(f"[{self.device.ieee}] IAS Zone Type: {type_name}")
                
            elif attrid == self.ATTR_ZONE_STATUS:
                # Zone status reported as attribute (some devices do this)
                self._handle_zone_status_change([value])
                
            elif attrid == self.ATTR_ZONE_STATE:
                # Zone state: 0 = not enrolled, 1 = enrolled
                enrolled = bool(value)
                self.device.update_state({"zone_enrolled": enrolled})
                logger.debug(f"[{self.device.ieee}] Zone enrolled: {enrolled}")
                
            elif attrid == self.ATTR_ZONE_ID:
                self._zone_id = value
                logger.debug(f"[{self.device.ieee}] Zone ID: {value}")

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error processing IAS attribute "
                        f"0x{attrid:04x}: {e}")

    async def configure(self):
        """
        Configure IAS Zone cluster.
        
        This involves:
        1. Binding the cluster
        2. Writing the CIE address (coordinator IEEE) for enrollment
        3. Reading zone type to understand what kind of sensor this is
        """
        try:
            # Bind the cluster
            await self.cluster.bind()
            logger.info(f"[{self.device.ieee}] IAS Zone cluster bound")

            # Get coordinator IEEE address for CIE enrollment
            try:
                coord = self.device.service.app.get_device(
                    self.device.service.app.state.node_info.ieee
                )
                
                # Write CIE address to enroll the zone
                await self.cluster.write_attributes({
                    'cie_addr': coord.ieee
                })
                logger.info(f"[{self.device.ieee}] IAS Zone enrolled with CIE address")
            except Exception as e:
                logger.warning(f"[{self.device.ieee}] Failed to write CIE address: {e}")

            # Read zone type to understand what kind of sensor this is
            try:
                result = await self.cluster.read_attributes([
                    self.ATTR_ZONE_TYPE, 
                    self.ATTR_ZONE_STATE
                ])
                if result and result[0]:
                    if self.ATTR_ZONE_TYPE in result[0]:
                        self._zone_type = result[0][self.ATTR_ZONE_TYPE]
                        if hasattr(self._zone_type, 'value'):
                            self._zone_type = self._zone_type.value
                        type_name = ZONE_TYPES.get(self._zone_type, f"unknown")
                        logger.info(f"[{self.device.ieee}] Zone type: {type_name}")
            except Exception as e:
                logger.warning(f"[{self.device.ieee}] Failed to read zone type: {e}")

            return True
            
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] IAS Zone configuration failed: {e}")
            return False

    def get_attr_name(self, attrid: int) -> str:
        names = {
            self.ATTR_ZONE_STATE: "zone_state",
            self.ATTR_ZONE_TYPE: "zone_type",
            self.ATTR_ZONE_STATUS: "zone_status",
        }
        return names.get(attrid, super().get_attr_name(attrid))

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_ZONE_STATUS: "zone_status",
        }
