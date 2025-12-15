"""
Fast Path Processor - Time-Critical ZCL Frame Handler
=====================================================
Based on ZHA's handle_cluster_request pattern for immediate processing.

This module provides:
- Direct ZCL frame parsing without full attribute chain
- Immediate state updates for motion/presence events
- Fast-path MQTT publishing (< 5ms latency)
- Minimal event loop blocking

Critical for:
- Motion sensors (0x0406 Occupancy Sensing)
- Radar sensors (0xEF00 Tuya)
- Door/window sensors (0x0500 IAS Zone)
"""
import logging
import time
import json
from typing import Optional, Tuple

logger = logging.getLogger("handlers.fast_path")


class FastPathProcessor:
    """
    Intercepts and fast-tracks time-critical Zigbee messages.

    Design:
    - Parses ZCL frames directly from raw bytes
    - Bypasses normal attribute_updated chain
    - Updates state and publishes in < 5ms
    - Still allows normal processing for logging/debug

    Based on ZHA's approach for motion sensors and IAS zones.
    """

    # ZCL Frame Control bits
    FRAME_TYPE_MASK = 0x03
    FRAME_TYPE_GLOBAL = 0x00
    FRAME_TYPE_CLUSTER_SPECIFIC = 0x01

    # ZCL Command IDs
    CMD_READ_ATTRIBUTES = 0x00
    CMD_READ_ATTRIBUTES_RSP = 0x01
    CMD_WRITE_ATTRIBUTES = 0x02
    CMD_REPORT_ATTRIBUTES = 0x0A
    CMD_DEFAULT_RESPONSE = 0x0B

    # ZCL Data Types
    DATA_TYPE_NO_DATA = 0x00
    DATA_TYPE_BOOL = 0x10
    DATA_TYPE_UINT8 = 0x20
    DATA_TYPE_UINT16 = 0x21
    DATA_TYPE_UINT32 = 0x23
    DATA_TYPE_INT8 = 0x28
    DATA_TYPE_INT16 = 0x29
    DATA_TYPE_ENUM8 = 0x30
    DATA_TYPE_BITMAP8 = 0x18

    def __init__(self, service):
        """
        Initialize fast path processor.

        Args:
            service: Parent ZigbeeService instance
        """
        self.service = service

        # Statistics
        self._stats = {
            'total_processed': 0,
            'fast_path_hits': 0,
            'occupancy_events': 0,
            'tuya_events': 0,
            'ias_events': 0,
            'parse_errors': 0,
        }

        logger.info("Fast path processor initialized")

    def process_frame(
            self,
            sender_ieee: str,
            profile: int,
            cluster: int,
            src_ep: int,
            dst_ep: int,
            message: bytes
    ) -> bool:
        """
        Attempt to fast-path process a ZCL frame.

        Args:
            sender_ieee: IEEE address of sender
            profile: Zigbee profile ID
            cluster: Cluster ID
            src_ep: Source endpoint
            dst_ep: Destination endpoint
            message: Raw ZCL frame bytes

        Returns:
            True if fast-pathed (still continue normal processing for debug),
            False if not applicable for fast path

        Performance Target: < 1ms
        """
        self._stats['total_processed'] += 1

        # Only fast-path ZCL frames (Home Automation profile)
        if profile != 0x0104:
            return False

        # --- 1. Occupancy Sensing (0x0406) ---
        if cluster == 0x0406:
            # Occupancy Sensing - motion sensors
            result = self._fast_path_occupancy(sender_ieee, message)
            if result:
                self._stats['fast_path_hits'] += 1
                self._stats['occupancy_events'] += 1
            return result

        # --- 2. On/Off (0x0006) - Used by IKEA/Tuya for motion ---
        elif cluster == 0x0006:
            result = self._fast_path_onoff(sender_ieee, message)
            if result:
                self._stats['fast_path_hits'] += 1
                self._stats['occupancy_events'] += 1
            return result

        # --- 3. Tuya Manufacturer (0xEF00) ---
        elif cluster == 0xEF00:
            # Tuya - radar sensors
            result = self._fast_path_tuya(sender_ieee, message)
            if result:
                self._stats['fast_path_hits'] += 1
                self._stats['tuya_events'] += 1
            return result

        # --- 4. IAS Zone (0x0500) ---
        elif cluster == 0x0500:
            # IAS Zone - door/window sensors
            result = self._fast_path_ias_zone(sender_ieee, message)
            if result:
                self._stats['fast_path_hits'] += 1
                self._stats['ias_events'] += 1
            return result

        return False

    def _fast_path_occupancy(self, ieee: str, message: bytes) -> bool:
        """Fast-path for Occupancy Sensing cluster (0x0406)."""
        return self._fast_path_generic_attribute(ieee, message, 0x0000, "Occupancy")

    def _fast_path_onoff(self, ieee: str, message: bytes) -> bool:
        """
        Fast-path for On/Off cluster (0x0006).
        Used by many motion sensors (IKEA, Generic Tuya) to report motion.
        """
        return self._fast_path_generic_attribute(ieee, message, 0x0000, "On/Off")

    def _fast_path_generic_attribute(self, ieee: str, message: bytes, target_attr_id: int, label: str) -> bool:
        """
        Generic helper to parse Report Attributes for a boolean/bitmap state.
        """
        try:
            # Need at least 3 bytes for ZCL header (FC + TSN + CMD)
            if len(message) < 3:
                return False

            frame_control = message[0]
            # tsn = message[1]
            command_id = message[2]

            # Only handle Report Attributes (0x0A)
            if command_id != self.CMD_REPORT_ATTRIBUTES:
                return False

            # Parse attribute reports
            idx = 3
            while idx + 3 <= len(message):  # AttrID(2) + Type(1)
                attr_id = int.from_bytes(message[idx:idx + 2], byteorder='little')
                data_type = message[idx + 2]

                # Check for our target attribute (usually 0x0000)
                if attr_id == target_attr_id:
                    # Handle Boolean (0x10) or Bitmap8 (0x18)
                    if data_type in [self.DATA_TYPE_BOOL, self.DATA_TYPE_BITMAP8]:
                        if idx + 3 < len(message):
                            value = message[idx + 3]
                            is_active = bool(value & 0x01)

                            # IMMEDIATE PUBLISH
                            self._emit_motion_immediate(ieee, is_active)

                            logger.debug(f"[{ieee}] FAST-PATH {label}: {is_active} (raw=0x{value:02x})")
                            return True

                # Calculate size to skip to next attribute
                # Check if we have enough bytes for the value
                if idx + 3 >= len(message):
                    break

                data_size = self._get_data_type_size(data_type, message, idx + 3)
                if data_size < 0:
                    break # Unknown type, abort

                idx += 3 + data_size

        except Exception as e:
            logger.debug(f"[{ieee}] Fast-path {label} parse error: {e}")
            self._stats['parse_errors'] += 1

        return False

    def _fast_path_tuya(self, ieee: str, message: bytes) -> bool:
        """Fast-path for Tuya cluster (0xEF00)."""
        try:
            if len(message) < 7:
                return False

            command_id = message[2]

            # Commands that contain DP data: 0x01, 0x02, 0x06
            if command_id not in (0x01, 0x02, 0x06):
                return False

            # Payload starts at index 3
            # Structure: Status(1) + TSN(2) + DPs...
            # So first DP starts at index 6 (3 + 3 header bytes)
            idx = 6

            presence_value = None

            while idx + 4 <= len(message):
                dp_id = message[idx]
                dp_type = message[idx + 1]
                dp_len = int.from_bytes(message[idx + 2:idx + 4], byteorder='big')

                if idx + 4 + dp_len > len(message):
                    break

                dp_data = message[idx + 4:idx + 4 + dp_len]

                # DP 1: State (ENUM: 0=none, 1=presence, 2=move)
                if dp_id == 1:
                    if dp_type == 0x04 and dp_len == 1:  # ENUM
                        enum_val = dp_data[0]
                        # 0 = none, 1 = presence, 2 = move
                        presence_value = enum_val > 0  # True if presence OR move
                        logger.debug(f"[{ieee}] Tuya DP1 state={enum_val}")
                    elif dp_type == 0x01:  # BOOL (some models)
                        presence_value = bool(dp_data[0])

                # DP 104: Binary presence
                elif dp_id == 104:
                    if dp_type == 0x01 and dp_len == 1:  # BOOL
                        presence_value = bool(dp_data[0])
                        logger.debug(f"[{ieee}] Tuya DP104 presence={presence_value}")

                idx += 4 + dp_len

            if presence_value is not None:
                self._emit_presence_immediate(ieee, presence_value, None)
                logger.info(f"[{ieee}] FAST-PATH Tuya Presence: {presence_value}")
                return True

        except Exception as e:
            logger.debug(f"[{ieee}] Fast-path Tuya parse error: {e}")
            self._stats['parse_errors'] += 1

        return False

    def _fast_path_ias_zone(self, ieee: str, message: bytes) -> bool:
        """
        Fast-path for IAS Zone cluster (0x0500).

        Used by door/window sensors, leak sensors, etc.

        Most important command:
        - 0x00: Zone Status Change Notification

        Args:
            ieee: Device IEEE address
            message: Raw IAS Zone frame

        Returns:
            True if zone status was extracted
        """
        try:
            if len(message) < 5:
                return False

            frame_control = message[0]
            tsn = message[1]
            command_id = message[2]

            # Zone Status Change Notification (cluster-specific, command 0x00)
            if command_id == 0x00:
                # Payload: zone_status (2 bytes) + extended_status (1 byte)
                if len(message) >= 5:
                    zone_status = int.from_bytes(message[3:5], byteorder='little')

                    # Bit 0: Alarm 1 (door open, motion, etc.)
                    # Bit 1: Alarm 2
                    # Bit 3: Tamper
                    # Bit 5: Battery

                    alarm1 = bool(zone_status & 0x01)
                    alarm2 = bool(zone_status & 0x02)
                    tamper = bool(zone_status & 0x08)
                    battery_low = bool(zone_status & 0x20)

                    # Publish immediately
                    self._emit_ias_zone_immediate(ieee, zone_status, alarm1, tamper, battery_low)

                    logger.debug(f"[{ieee}] FAST-PATH IAS Zone: status=0x{zone_status:04x}, alarm={alarm1}")
                    return True

        except Exception as e:
            logger.debug(f"[{ieee}] Fast-path IAS Zone parse error: {e}")
            self._stats['parse_errors'] += 1

        return False

    def _emit_motion_immediate(self, ieee: str, occupied: bool):
        """
        Emit motion event with minimal latency.

        Steps:
        1. Update device state in memory
        2. Fast-path MQTT publish (non-blocking)
        3. WebSocket broadcast (non-blocking)

        Target latency: < 5ms
        """
        if ieee not in self.service.devices:
            return

        device = self.service.devices[ieee]

        # Update state immediately
        device.state['occupancy'] = occupied
        device.state['motion'] = occupied
        device.state['presence'] = occupied
        device.last_seen = int(time.time() * 1000)

        # Mark cache dirty (will save in background)
        self.service._cache_dirty = True

        # Fast MQTT publish (non-blocking)
        if self.service.mqtt and hasattr(self.service.mqtt, 'publish_fast'):
            safe_name = self.service.get_safe_name(ieee)
            payload = json.dumps({
                'occupancy': occupied,
                'motion': occupied,
                'presence': occupied
            })
            self.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

        # WebSocket broadcast (non-blocking if using queue)
        if hasattr(self.service, 'event_callback'):
            try:
                import asyncio
                # Use call_soon_threadsafe or create_task to avoid blocking
                # We assume event_callback is async
                # Check if there is a running loop, otherwise we can't emit
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.service.event_callback('device_state', {
                        'ieee': ieee,
                        'state': {'occupancy': occupied, 'motion': occupied}
                    }))
                except RuntimeError:
                    pass
            except Exception as e:
                logger.debug(f"WebSocket broadcast error: {e}")

    def _emit_presence_immediate(self, ieee: str, present: bool, distance: Optional[int] = None):
        """Emit Tuya radar presence event with minimal latency."""
        if ieee not in self.service.devices:
            return

        device = self.service.devices[ieee]

        # Update state
        device.state['presence'] = present
        device.state['state'] = present
        device.state['occupancy'] = present

        if distance is not None:
            device.state['distance'] = distance / 100.0  # Convert to meters

        device.last_seen = int(time.time() * 1000)
        self.service._cache_dirty = True

        # Fast MQTT publish
        if self.service.mqtt and hasattr(self.service.mqtt, 'publish_fast'):
            safe_name = self.service.get_safe_name(ieee)
            state_dict = {
                'presence': present,
                'state': present,
                'occupancy': present
            }
            if distance is not None:
                state_dict['distance'] = device.state['distance']

            payload = json.dumps(state_dict)
            self.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

    def _emit_ias_zone_immediate(self, ieee: str, zone_status: int, alarm: bool,
                                 tamper: bool, battery_low: bool):
        """Emit IAS Zone event with minimal latency."""
        if ieee not in self.service.devices:
            return

        device = self.service.devices[ieee]

        # Update state
        device.state['zone_status'] = zone_status
        device.state['contact'] = not alarm  # Inverted: alarm=True means contact=False (open)
        device.state['tamper'] = tamper
        device.state['battery_low'] = battery_low
        device.last_seen = int(time.time() * 1000)
        self.service._cache_dirty = True

        # Fast MQTT publish
        if self.service.mqtt and hasattr(self.service.mqtt, 'publish_fast'):
            safe_name = self.service.get_safe_name(ieee)
            payload = json.dumps({
                'contact': not alarm,
                'tamper': tamper,
                'battery_low': battery_low
            })
            self.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

    def _get_data_type_size(self, data_type: int, message: bytes, idx: int) -> int:
        """
        Get size of ZCL data type.

        Args:
            data_type: ZCL data type ID
            message: Full message (for variable-length types)
            idx: Current index in message

        Returns:
            Size in bytes, or -1 if unknown/error
        """
        # Fixed-size types
        size_map = {
            0x00: 0,  # No data
            0x10: 1,  # Boolean
            0x18: 1,  # Bitmap8
            0x19: 2,  # Bitmap16
            0x1A: 3,  # Bitmap24
            0x1B: 4,  # Bitmap32
            0x20: 1,  # Uint8
            0x21: 2,  # Uint16
            0x22: 3,  # Uint24
            0x23: 4,  # Uint32
            0x28: 1,  # Int8
            0x29: 2,  # Int16
            0x2B: 4,  # Int32
            0x30: 1,  # Enum8
            0x31: 2,  # Enum16
            0x39: 2,  # Float16
            0x3A: 4,  # Float32
        }

        if data_type in size_map:
            return size_map[data_type]

        # Variable-length types (have length prefix)
        if data_type in (0x41, 0x42):  # Octet string, char string
            if idx < len(message):
                return 1 + message[idx]  # Length byte + data

        # Unknown type
        logger.debug(f"Unknown ZCL data type: 0x{data_type:02x}")
        return -1

    def get_stats(self) -> dict:
        """Get fast path statistics."""
        return {
            **self._stats,
            'hit_rate': (self._stats['fast_path_hits'] / max(1, self._stats['total_processed'])) * 100
        }


# Unit Testing
if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.DEBUG)


    # Mock service
    class MockService:
        def __init__(self):
            self.devices = {}
            self.mqtt = None
            self._cache_dirty = False

        def get_safe_name(self, ieee):
            return ieee.replace(":", "")


    processor = FastPathProcessor(MockService())

    # Occupancy Sensing Report Attributes
    print("\nTest 1: Occupancy Sensing")
    # Frame: FCF=0x18, TSN=0x01, CMD=0x0A (Report), Attr=0x0000, Type=0x18 (Bitmap8), Value=0x01
    test_frame = bytes([0x18, 0x01, 0x0A, 0x00, 0x00, 0x18, 0x01])
    result = processor.process_frame("00:11:22:33:44:55:66:77", 0x0104, 0x0406, 1, 1, test_frame)
    print(f"  Result: {result}")
    print(f"  Stats: {processor.get_stats()}")

    print("\nFast path processor tests completed")