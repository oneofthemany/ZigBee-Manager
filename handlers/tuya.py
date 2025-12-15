"""
Tuya-specific cluster handlers for Zigbee devices.
Handles: Tuya radar/mmWave sensors, air quality sensors, and other Tuya proprietary devices.
"""
import logging
from typing import Any, Dict, Optional, Callable, List
from dataclasses import dataclass
import asyncio
import time

from .base import ClusterHandler, register_handler

# Import the debugger for structured logging
try:
    from zigbee_debug import get_debugger
except ImportError:
    def get_debugger():
        return None

logger = logging.getLogger("handlers.tuya")

TUYA_CLUSTER_ID = 0xEF00

# Tuya DP command IDs
TUYA_SET_DATA = 0x00
TUYA_GET_DATA = 0x01
TUYA_SET_DATA_RESPONSE = 0x02
TUYA_ACTIVE_STATUS_REPORT = 0x06
TUYA_TIME_REQUEST = 0x24


@dataclass
class TuyaDP:
    """Defines a Tuya Data Point mapping."""
    dp_id: int
    name: str
    converter: Optional[Callable] = None
    scale: float = 1.0
    unit: str = ""
    # Type field for correct writing format
    type: int = 0x02  # Default to VALUE (Integer)


# Updated DP Mappings for 24GHz Radar (Standard / Generic)
TUYA_RADAR_DPS = {
    # Presence/Occupancy
    1: TuyaDP(1, "state", lambda x: bool(x), type=0x01), # BOOL

    # Configuration
    2: TuyaDP(2, "radar_sensitivity", scale=1, type=0x02), # VALUE
    102: TuyaDP(102, "presence_sensitivity", scale=1, type=0x02), # VALUE
    105: TuyaDP(105, "keep_time", scale=1, unit="s", type=0x02), # VALUE
    3: TuyaDP(3, "detection_distance_min", scale=0.01, unit="m", type=0x02),
    4: TuyaDP(4, "detection_distance_max", scale=0.01, unit="m", type=0x02),

    # Readings
    9: TuyaDP(9, "distance", scale=0.01, unit="m", type=0x02),
    104: TuyaDP(104, "illuminance", scale=1, unit="lux", type=0x02),
    10: TuyaDP(10, "fading_time", scale=1, unit="s", type=0x02),
}

# DP Mappings for ZY-M100-24GV2 (_TZE204_7gclukjs)
TUYA_RADAR_ZY_M100_DPS = {
    # DP 104: Binary Presence (True/False)
    104: TuyaDP(104, "presence", lambda x: bool(x), type=0x01),

    # DP 1: Radar State (Enum lookup)
    # 0 = none, 1 = presence, 2 = move
    1: TuyaDP(1, "radar_state",
              lambda x: {0: "none", 1: "presence", 2: "move"}.get(int(x), str(x)),
              type=0x04),

    # DP 103: Illuminance (Lux) - Specific to this model
    103: TuyaDP(103, "illuminance", scale=1, unit="lux", type=0x02),

    # DP 9: Distance (Z2M uses divideBy10 -> scale 0.1)
    9: TuyaDP(9, "distance", scale=0.1, unit="m", type=0x02),

    # Configuration DPs
    2: TuyaDP(2, "radar_sensitivity", scale=1, type=0x02),
    102: TuyaDP(102, "presence_sensitivity", scale=1, type=0x02),
    3: TuyaDP(3, "detection_distance_min", scale=0.01, unit="m", type=0x02),
    4: TuyaDP(4, "detection_distance_max", scale=0.01, unit="m", type=0x02),
    105: TuyaDP(105, "keep_time", scale=1, unit="s", type=0x02),

    # DP 10: Fading Time
    10: TuyaDP(10, "fading_time", scale=1, unit="s", type=0x02),
}

TUYA_AIR_QUALITY_DPS = {
    # Temperature & Humidity (common)
    1: TuyaDP(1, "temperature", scale=0.1, unit="°C"),
    2: TuyaDP(2, "humidity", scale=1, unit="%"),

    # Air Quality
    18: TuyaDP(18, "co2", scale=1, unit="ppm"),
    19: TuyaDP(19, "voc", scale=1, unit="ppm"),
    20: TuyaDP(20, "formaldehyde", scale=0.01, unit="mg/m³"),
    21: TuyaDP(21, "pm25", scale=1, unit="µg/m³"),

    # Alternative DP IDs used by some models
    3: TuyaDP(3, "pm25", scale=1, unit="µg/m³"),
    4: TuyaDP(4, "co2", scale=1, unit="ppm"),
    5: TuyaDP(5, "formaldehyde", scale=0.01, unit="mg/m³"),
    22: TuyaDP(22, "pm10", scale=1, unit="µg/m³"),
}

TUYA_SWITCH_DPS = {
    1: TuyaDP(1, "state_1", lambda x: "ON" if x else "OFF", type=0x01),
    2: TuyaDP(2, "state_2", lambda x: "ON" if x else "OFF", type=0x01),
    3: TuyaDP(3, "state_3", lambda x: "ON" if x else "OFF", type=0x01),
    4: TuyaDP(4, "state_4", lambda x: "ON" if x else "OFF", type=0x01),

    # Countdown timers
    9: TuyaDP(9, "countdown_1", scale=1, unit="s", type=0x02),
    10: TuyaDP(10, "countdown_2", scale=1, unit="s", type=0x02),
}

TUYA_VALVE_DPS = {
    1: TuyaDP(1, "state", lambda x: "ON" if x else "OFF", type=0x01),
    2: TuyaDP(2, "countdown", scale=1, unit="s", type=0x02),
    5: TuyaDP(5, "flow", scale=0.001, unit="L", type=0x02),  # Total flow
    6: TuyaDP(6, "battery", scale=1, unit="%", type=0x02),
    7: TuyaDP(7, "temperature", scale=0.1, unit="°C", type=0x02),
}


# ============================================================
# TUYA MANUFACTURER CLUSTER (0xEF00)
# ============================================================
@register_handler(TUYA_CLUSTER_ID)
class TuyaClusterHandler(ClusterHandler):
    """
    Handles Tuya manufacturer-specific cluster (0xEF00).
    Processes Tuya Data Points for radar sensors, air quality monitors, etc.
    """
    CLUSTER_ID = TUYA_CLUSTER_ID

    # DP type identifiers
    DP_TYPE_RAW = 0x00
    DP_TYPE_BOOL = 0x01
    DP_TYPE_VALUE = 0x02  # 4-byte integer
    DP_TYPE_STRING = 0x03
    DP_TYPE_ENUM = 0x04
    DP_TYPE_BITMAP = 0x05

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._dp_map = self._identify_dp_map()
        self._seq = 0

    def _identify_dp_map(self) -> Dict[int, TuyaDP]:
        """Identify which DP map to use based on device model."""
        model = str(getattr(self.device.zigpy_dev, 'model', '')).lower()
        manufacturer = str(getattr(self.device.zigpy_dev, 'manufacturer', '')).lower()

        # Specific check for the ZY-M100-24GV2 variants (_TZE204_7gclukjs)
        # This matches the Z2M definition
        if '_tze204_7gclukjs' in manufacturer or 'zy-m100' in model:
            logger.info(f"[{self.device.ieee}] Using ZY-M100 Radar DP map")
            return TUYA_RADAR_ZY_M100_DPS

        # Radar/mmWave presence sensors
        if any(x in model for x in ['zg-204z', 'ts0601', 'radar', 'mmwave', 'presence']):
            logger.info(f"[{self.device.ieee}] Using Tuya Radar DP map")
            return TUYA_RADAR_DPS

        # Air quality sensors
        if any(x in model for x in ['air', 'co2', 'pm25', 'voc']):
            logger.info(f"[{self.device.ieee}] Using Tuya Air Quality DP map")
            return TUYA_AIR_QUALITY_DPS

        # Valve devices
        if 'valve' in model:
            logger.info(f"[{self.device.ieee}] Using Tuya Valve DP map")
            return TUYA_VALVE_DPS

        # Check manufacturer strings
        if '_tze204' in manufacturer or '_tze200' in manufacturer:
            logger.info(f"[{self.device.ieee}] Using Tuya Radar DP map (by manufacturer)")
            return TUYA_RADAR_DPS

        # Default to radar (most common)
        logger.info(f"[{self.device.ieee}] Using default Tuya DP map")
        return TUYA_RADAR_DPS

    def cluster_command(self, tsn: int, command_id: int, args):
        """Handle Tuya cluster commands (data reports)."""
        logger.debug(f"[{self.device.ieee}] Tuya command: cmd=0x{command_id:02x}, args={args}")

        # Ensure base logging/debugging is called (even if we override logic later)
        super().cluster_command(tsn, command_id, args)

        if command_id in [TUYA_SET_DATA_RESPONSE, TUYA_ACTIVE_STATUS_REPORT]:
            self._handle_data_report(args)
        elif command_id == TUYA_TIME_REQUEST:
            logger.debug(f"[{self.device.ieee}] Tuya time request received")
            # Could respond with current time if needed

    def _handle_data_report(self, args):
        """Process Tuya data report."""
        try:
            if not args:
                return

            # Tuya payload structure varies, handle both formats
            data = args[0] if isinstance(args, (list, tuple)) else args

            # Convert to bytes if needed
            if hasattr(data, 'serialize'):
                data = data.serialize()
            elif isinstance(data, (list, tuple)):
                data = bytes(data)
            elif not isinstance(data, bytes):
                data = bytes(data)

            self._parse_tuya_payload(data)

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error handling Tuya data: {e}")

    def _parse_tuya_payload(self, data: bytes):
        """Parse Tuya payload and extract DPs."""
        if len(data) < 4:
            return

        # Standard Tuya format: [seq_hi, seq_lo, dp_id, dp_type, len_hi, len_lo, ...data...]
        # Some devices have slightly different formats
        offset = 0
        parsed_dps = [] # Collect parsed DP structures for enhanced logging

        # Try to skip sequence number if present (common pattern)
        if offset == 0 and len(data) > 6:
            # Skip sequence number if present (common pattern 0x00 and a low byte)
            if data[0] == 0x00 and data[1] < 0x80:
                offset = 2

        start_offset = offset

        while offset < len(data) - 4:
            try:
                # Store starting position for this DP
                dp_start = offset

                dp_id = data[offset]
                dp_type = data[offset + 1]
                dp_len = (data[offset + 2] << 8) | data[offset + 3]

                if offset + 4 + dp_len > len(data):
                    break

                dp_data = data[offset + 4:offset + 4 + dp_len]

                # Process and get the value before scaling/conversion for logging
                raw_value, parsed_value = self._process_dp_logic(dp_id, dp_type, dp_data)

                # Add structured data for enhanced debugging
                parsed_dps.append({
                    "dp_id": dp_id,
                    "dp_type": dp_type,
                    "dp_len": dp_len,
                    "raw_hex": dp_data.hex(),
                    "raw_value": raw_value,
                    "parsed_value": parsed_value, # Value after initial decoding (before scaling/converter)
                    "dp_def_name": self._dp_map.get(dp_id).name if self._dp_map.get(dp_id) else "Unknown",
                    "dp_def_scale": self._dp_map.get(dp_id).scale if self._dp_map.get(dp_id) else 1.0,
                    "dp_def_unit": self._dp_map.get(dp_id).unit if self._dp_map.get(dp_id) else "",
                })

                offset += 4 + dp_len

            except (IndexError, ValueError) as e:
                logger.debug(f"[{self.device.ieee}] Parse error at offset {offset}: {e}")
                break

        # --- ENHANCED DEBUGGING EMISSION ---
        debugger = get_debugger()
        if debugger and parsed_dps:
            debugger.record_tuya_report(
                self.device.ieee,
                data.hex(),
                parsed_dps
            )
        # --- END ENHANCED DEBUGGING EMISSION ---

        # Now actually process the DPs for state update (this calls the _process_dp again but uses the logic below)
        self._update_state_from_dps(data)


    def _process_dp_logic(self, dp_id: int, dp_type: int, dp_data: bytes):
        """Decodes raw DP data into a Python object (pre-scaling/conversion)."""
        raw_value = None

        # Decode raw value based on type
        if dp_type == self.DP_TYPE_BOOL:
            raw_value = bool(dp_data[0]) if dp_data else False
            parsed_value = raw_value
        elif dp_type == self.DP_TYPE_VALUE:
            raw_value = int.from_bytes(dp_data, 'big', signed=True)
            parsed_value = raw_value
        elif dp_type == self.DP_TYPE_ENUM:
            raw_value = dp_data[0] if dp_data else 0
            parsed_value = raw_value
        elif dp_type == self.DP_TYPE_STRING:
            raw_value = dp_data.decode('utf-8', errors='ignore')
            parsed_value = raw_value
        elif dp_type == self.DP_TYPE_BITMAP:
            raw_value = int.from_bytes(dp_data, 'big')
            parsed_value = raw_value
        else:
            raw_value = dp_data.hex()
            parsed_value = raw_value

        return raw_value, parsed_value

    def _update_state_from_dps(self, data: bytes):
        """Re-parses the payload to update state (original core logic)."""
        offset = 0

        # Try to skip sequence number if present (common pattern)
        if offset == 0 and len(data) > 6:
            if data[0] == 0x00 and data[1] < 0x80:
                offset = 2

        while offset < len(data) - 4:
            try:
                dp_id = data[offset]
                dp_type = data[offset + 1]
                dp_len = (data[offset + 2] << 8) | data[offset + 3]

                if offset + 4 + dp_len > len(data):
                    break

                dp_data = data[offset + 4:offset + 4 + dp_len]

                # --- START Original _process_dp logic ---

                # Decode value based on type
                raw_value, value = self._process_dp_logic(dp_id, dp_type, dp_data)

                # Look up DP definition
                dp_def = self._dp_map.get(dp_id)

                if dp_def:
                    # Apply converter if defined
                    if dp_def.converter:
                        try:
                            value = dp_def.converter(value)
                        except:
                            pass

                    # Apply scale
                    if isinstance(value, (int, float)) and dp_def.scale != 1.0:
                        value = round(value * dp_def.scale, 2)

                    # 1. DROP Distance (DP9) to prevent spamming
                    if dp_id == 9 or dp_def.name == "distance":
                        return # Skip state update for distance

                    # 2. Prioritize Critical DPs (Presence / State)
                    qos = None
                    if dp_id in [1, 104]:  # State or Presence
                        qos = 2
                        logger.info(f"[{self.device.ieee}] 🚨 Critical (QoS 2): {dp_def.name} = {value}")
                    else:
                        logger.info(f"[{self.device.ieee}] DP{dp_id}: {dp_def.name} = {value}{dp_def.unit}")

                    # Prepare state update
                    state_update = {dp_def.name: value}

                    # SPECIAL HANDLING: For illuminance, create both attributes (ZHA pattern)
                    if dp_def.name == "illuminance":
                        state_update["illuminance_lux"] = value
                        logger.debug(f"[{self.device.ieee}] Created illuminance_lux alias: {value}")

                    # === FAST-PATH PUBLISH for presence/state (CRITICAL DPs) ===
                    if dp_id in [1, 104] and self.device.service.mqtt and hasattr(self.device.service.mqtt, 'publish_fast'):
                        # For radar presence, publish IMMEDIATELY via fast path
                        safe_name = self.device.service.get_safe_name(self.device.ieee)

                        # Update state in memory first
                        self.device.state.update(state_update)
                        self.device.last_seen = int(time.time() * 1000)
                        self.device.service._cache_dirty = True

                        # Fast non-blocking MQTT publish
                        import json
                        payload = json.dumps(state_update)
                        self.device.service.mqtt.publish_fast(f"{safe_name}/state", payload, qos=0)

                        logger.debug(f"[{self.device.ieee}] FAST-PATH: Published {dp_def.name}={value}")

                    else:
                        # Normal update for non-critical DPs (temperature, humidity, etc.)
                        self.device.update_state(state_update, qos=qos)
                    # === END FAST-PATH ===

                else:
                    # Unknown DP - log it for debugging
                    logger.debug(f"[{self.device.ieee}] Unknown DP{dp_id} (type {dp_type}): {value}")
                    self.device.update_state({f"dp_{dp_id}": value})

                # --- END Original _process_dp logic ---

                offset += 4 + dp_len

            except Exception as e:
                logger.error(f"[{self.device.ieee}] Error processing DP at offset {offset}: {e}")
                break


    def handle_raw_data(self, message: bytes):
        """Handle raw Tuya message data (called from device.py)."""
        self._parse_tuya_payload(message)

    async def configure(self):
        """Configure Tuya device - usually no standard binding needed."""
        logger.info(f"[{self.device.ieee}] Tuya device configured (no standard binding)")
        return True

    async def apply_settings(self, settings: Dict[str, Any]):
        """
        Apply settings from frontend (called via device.configure).
        Maps frontend keys (move_sensitivity) to DP names (radar_sensitivity).
        """
        key_map = {
            "radar_sensitivity": "radar_sensitivity",
            "presence_sensitivity": "presence_sensitivity",
            "keep_time": "keep_time",
            "detection_distance_min": "detection_distance_min",
            "detection_distance_max": "detection_distance_max",
            "fading_time": "fading_time",

            # Legacy mapping support (if old UI code is still present)
            "move_sensitivity": "radar_sensitivity",
            "presence_timeout": "keep_time",
            "min_dist": "detection_distance_min",
            "max_dist": "detection_distance_max"
        }

        logger.info(f"[{self.device.ieee}] Applying Tuya Settings: {settings}")

        # Track successfully applied settings to update state
        applied_settings = {}

        for ui_key, value in settings.items():
            # Map UI key to internal DP Name
            dp_name = key_map.get(ui_key, ui_key)

            # Find the DP definition by name
            target_dp = None
            for dp_id, dp_def in self._dp_map.items():
                if dp_def.name == dp_name:
                    target_dp = dp_def
                    break

            if target_dp:
                try:
                    # Inverse scale: writing 6.0m -> send 600
                    # For integers (like sensitivity 0-10), scale 1 means 7 -> 7
                    raw_val = int(value / target_dp.scale)

                    # Use the configured type from the DP definition, default to VALUE
                    dp_type = target_dp.type if hasattr(target_dp, 'type') else self.DP_TYPE_VALUE

                    success = await self.send_dp(target_dp.dp_id, dp_type, raw_val)

                    if success:
                        # Track the applied setting using the DP name
                        applied_settings[dp_name] = value
                        logger.info(f"[{self.device.ieee}] Set {dp_name} ({target_dp.dp_id}) to {value} (raw: {raw_val}, type: {dp_type})")
                    else:
                        logger.warning(f"[{self.device.ieee}] DP send returned False for {dp_name}")

                except Exception as e:
                    logger.error(f"[{self.device.ieee}] Failed to set {dp_name}: {e}")

                # Ensure delay happens even if command failed to allow network recovery
                # Increased to 0.5s for stability
                await asyncio.sleep(0.5)
            else:
                logger.warning(f"[{self.device.ieee}] Could not find DP for setting '{ui_key}'")

        # Update device state with all successfully applied settings
        # This ensures the frontend sees the new values immediately
        if applied_settings:
            logger.info(f"[{self.device.ieee}] Updating state with applied settings: {applied_settings}")
            self.device.update_state(applied_settings)

    async def poll(self) -> Dict[str, Any]:
        """
        Poll the device for current status (Tuya specific).

        Uses TUYA_QUERY_DATA (0x03) command which triggers the device
        to send back its current datapoint values.

        NOTE: Not all Tuya devices support polling. Some radar sensors only
        report data when state changes (event-driven).
        """
        logger.info(f"[{self.device.ieee}] Polling Tuya device (sending query)")

        self._seq = (self._seq + 1) % 0x10000
        zcl_seq = self._seq & 0xFF

        # TUYA_QUERY_DATA = 0x03 - used by ZHA's "data query spell"
        TUYA_QUERY_DATA = 0x03

        # Build ZCL frame for query (no payload needed)
        frame_control = 0x15  # Manufacturer-specific cluster command
        manuf_id = 0xFFFF     # NO_MANUFACTURER_ID

        zcl_frame = bytes([
            frame_control,
            manuf_id & 0xFF,
            (manuf_id >> 8) & 0xFF,
            zcl_seq,
            TUYA_QUERY_DATA,
            ])

        try:
            zigpy_device = self.cluster.endpoint.device
            endpoint_id = self.cluster.endpoint.endpoint_id

            result = await zigpy_device.request(
                profile=0x0104,
                cluster=TUYA_CLUSTER_ID,
                src_ep=endpoint_id,
                dst_ep=endpoint_id,
                sequence=zcl_seq,
                data=zcl_frame,
                expect_reply=False
            )

            logger.debug(f"[{self.device.ieee}] Poll query sent (result: {result})")
            return {"status": "polled"}

        except Exception as e:
            # Many Tuya devices don't support active polling
            # They report state changes automatically via ACTIVE_STATUS_REPORT
            logger.debug(f"[{self.device.ieee}] Poll failed (device may not support it): {e}")
            return {}

    async def send_dp(self, dp_id: int, dp_type: int, value: Any):
        """
        Send a DP command to the device using raw ZCL frame construction.

        This uses the zigpy device's request() method to send raw ZCL frames,
        bypassing all cluster command schema validation.

        Based on analysis of ZHA's TuyaNewManufCluster which requires:
        - Manufacturer ID: 0xFFFF (NO_MANUFACTURER_ID)
        - Command ID: 0x00 (TUYA_SET_DATA)
        - Manufacturer-specific frame control
        """
        self._seq = (self._seq + 1) % 0x10000

        # Encode value based on type
        if dp_type == self.DP_TYPE_BOOL:
            dp_data = bytes([1 if value else 0])
        elif dp_type == self.DP_TYPE_VALUE:
            dp_data = int(value).to_bytes(4, 'big', signed=True)
        elif dp_type == self.DP_TYPE_ENUM:
            dp_data = bytes([int(value)])
        elif dp_type == self.DP_TYPE_STRING:
            dp_data = str(value).encode('utf-8')
        else:
            dp_data = bytes(value) if isinstance(value, (list, tuple)) else bytes([value])

        # Build the Tuya datapoint payload (after ZCL header)
        # Structure: status(1) + tsn(2) + dp_id(1) + dp_type(1) + len(2) + data(n)
        tuya_payload = bytes([
            0x00,                         # status
            (self._seq >> 8) & 0xFF,      # tsn high byte
            self._seq & 0xFF,             # tsn low byte
            dp_id,                        # datapoint ID
            dp_type,                      # datapoint type
            (len(dp_data) >> 8) & 0xFF,   # data length high
            len(dp_data) & 0xFF,          # data length low
        ]) + dp_data

        # Build complete ZCL frame with manufacturer-specific header
        # Frame Control byte: 0x15 = 0b00010101
        #   - Bits 0-1: Frame type = 01 (Cluster-specific)
        #   - Bit 2: Manufacturer specific = 1 (Yes)
        #   - Bit 3: Direction = 0 (Client to Server)
        #   - Bit 4: Disable default response = 1 (Yes)
        frame_control = 0x15
        manuf_id = 0xFFFF  # NO_MANUFACTURER_ID - critical for Tuya
        zcl_seq = self._seq & 0xFF

        zcl_frame = bytes([
            frame_control,
            manuf_id & 0xFF,          # Manufacturer ID low byte
            (manuf_id >> 8) & 0xFF,   # Manufacturer ID high byte
            zcl_seq,                  # Sequence number
            TUYA_SET_DATA,            # Command ID (0x00)
        ]) + tuya_payload

        logger.debug(f"[{self.device.ieee}] DP{dp_id} ZCL frame: {zcl_frame.hex()}")

        try:
            # Get the zigpy device and endpoint
            zigpy_device = self.cluster.endpoint.device
            endpoint_id = self.cluster.endpoint.endpoint_id

            # Send using device.request() - this is the most reliable method
            # as it bypasses all cluster schema validation
            result = await zigpy_device.request(
                profile=0x0104,           # HA Profile ID
                cluster=TUYA_CLUSTER_ID,  # 0xEF00
                src_ep=endpoint_id,
                dst_ep=endpoint_id,
                sequence=zcl_seq,
                data=zcl_frame,
                expect_reply=False
            )

            logger.info(f"[{self.device.ieee}] DP{dp_id}={value} sent successfully (result: {result})")
            return True

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Failed to send DP{dp_id}: {e}")
            import traceback
            logger.debug(f"[{self.device.ieee}] Traceback:\n{traceback.format_exc()}")
            return False

    # Convenience methods for common operations
    async def set_presence_sensitivity(self, value: int):
        """Set presence detection sensitivity (0-10)."""
        await self.send_dp(102, self.DP_TYPE_VALUE, value)

    async def set_motion_sensitivity(self, value: int):
        """Set motion detection sensitivity (0-10)."""
        await self.send_dp(2, self.DP_TYPE_VALUE, value)

    async def set_presence_timeout(self, seconds: int):
        """Set presence detection timeout in seconds."""
        await self.send_dp(105, self.DP_TYPE_VALUE, seconds)

    async def set_detection_range(self, min_m: float, max_m: float):
        """Set detection distance range in meters."""
        await self.send_dp(3, self.DP_TYPE_VALUE, int(min_m * 100))
        await self.send_dp(4, self.DP_TYPE_VALUE, int(max_m * 100))

    # --- HA DISCOVERY ---
    def get_discovery_configs(self) -> List[Dict]:
        """Generate HA discovery configs."""
        configs = []
        for dp_id, dp_def in self._dp_map.items():
            name = dp_def.name

            # Filter out unknown/raw DPs if they sneak in
            if not name or name.startswith("dp_"): continue

            # 1. Binary Sensors
            if name in ["presence", "occupancy"] or (name == "state" and dp_def.type == self.DP_TYPE_BOOL):
                configs.append({
                    "component": "binary_sensor",
                    "object_id": name,
                    "config": {
                        "name": name.replace("_", " ").title(),
                        "device_class": "occupancy" if "presence" in name else "motion",
                        "value_template": f"{{{{ 'ON' if value_json.{name} else 'OFF' }}}}"
                    }
                })

            # 2. Sensors
            elif name in ["illuminance", "distance", "radar_state", "temperature", "humidity",
                          "co2", "voc", "pm25", "formaldehyde", "battery", "flow"]:
                cfg = {
                    "name": name.replace("_", " ").title(),
                    "value_template": f"{{{{ value_json.{name} }}}}"
                }
                if dp_def.unit: cfg["unit_of_measurement"] = dp_def.unit

                if "illuminance" in name:
                    cfg["device_class"] = "illuminance"
                    cfg["state_class"] = "measurement"  # Add state_class for statistics
                elif "temperature" in name: cfg["device_class"] = "temperature"
                elif "humidity" in name: cfg["device_class"] = "humidity"
                elif "battery" in name: cfg["device_class"] = "battery"
                elif "co2" in name: cfg["device_class"] = "carbon_dioxide"

                configs.append({
                    "component": "sensor",
                    "object_id": name,
                    "config": cfg
                })


            # 3. Numbers (Configuration)
            elif name in ["radar_sensitivity", "presence_sensitivity", "keep_time",
                          "detection_distance_min", "detection_distance_max", "fading_time",
                          "countdown_1", "countdown_2", "countdown"]:

                # Defaults
                min_v, max_v, step = 0, 10, 1

                # Custom ranges
                if "sensitivity" in name: min_v, max_v = 1, 10
                if "keep_time" in name: min_v, max_v = 0, 3600
                if "fading_time" in name: min_v, max_v = 0, 3600
                if "distance" in name:
                    min_v, max_v, step = 0, 10, 0.01
                    # Special handling for ZY-M100 if we are using that map
                    if self._dp_map == TUYA_RADAR_ZY_M100_DPS:
                        max_v = 9.0
                        step = 0.75

                configs.append({
                    "component": "number",
                    "object_id": name,
                    "config": {
                        "name": name.replace("_", " ").title(),
                        "min": min_v, "max": max_v, "step": step,
                        "mode": "box",
                        "value_template": f"{{{{ value_json.{name} }}}}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER", # Core fills this
                        "command_template": f'{{"tuya_{name}": {{{{ value }}}} }}'
                    }
                })

        return configs

    # --- UI & CONFIGURATION EXPOSURE ---
    def get_configuration_options(self) -> List[Dict]:
        """
        Return configuration options for the frontend based on the active DP map.
        This allows the frontend to dynamically build the settings form.
        """
        options = []
        for dp_id, dp_def in self._dp_map.items():
            name = dp_def.name

            # Determine if this DP is a configuration setting
            # We filter by name since DP type alone isn't enough (some values are read-only)
            is_config = name in [
                "radar_sensitivity", "presence_sensitivity", "keep_time",
                "detection_distance_min", "detection_distance_max",
                "fading_time", "motion_timeout"
            ]

            if is_config:
                # Default ranges
                min_v, max_v, step = 0, 10, 1

                if "sensitivity" in name: min_v, max_v = 1, 10
                if "keep_time" in name: min_v, max_v = 0, 3600
                if "fading_time" in name: min_v, max_v = 0, 3600

                # ZY-M100 specific distance logic
                if "distance" in name:
                    min_v, max_v, step = 0, 10, 0.01
                    if self._dp_map == TUYA_RADAR_ZY_M100_DPS:
                        # ZY-M100 uses 0.75m steps
                        step = 0.75
                        max_v = 9.0
                        if "max" in name: min_v = 0.75

                options.append({
                    "name": name,
                    "label": name.replace("_", " ").title(),
                    "type": "number",
                    "min": min_v,
                    "max": max_v,
                    "step": step,
                    "description": f"Set {name.replace('_', ' ')}",
                    "attribute_id": f"dp_{dp_id}" # Virtual attribute ID
                })

        return options


# ============================================================
# TUYA PRIVATE CLUSTER 2 (0xE001)
# Some devices use this for additional features
# ============================================================
@register_handler(0xE001)
class TuyaPrivateCluster2Handler(ClusterHandler):
    """Handles Tuya private cluster 0xE001."""
    CLUSTER_ID = 0xE001

    def cluster_command(self, tsn: int, command_id: int, args):
        logger.debug(f"[{self.device.ieee}] Tuya 0xE001: cmd=0x{command_id:02x}")


# ============================================================
# ANALOG INPUT CLUSTER (0x000C)
# Used by some Tuya devices for sensor data
# ============================================================
@register_handler(0x000C)
class AnalogInputHandler(ClusterHandler):
    """
    Handles Analog Input cluster (0x000C).
    Used by some Tuya devices for sensor readings.
    """
    CLUSTER_ID = 0x000C

    ATTR_PRESENT_VALUE = 0x0055
    ATTR_STATUS_FLAGS = 0x006F
    ATTR_OUT_OF_SERVICE = 0x0051
    ATTR_DESCRIPTION = 0x001C

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_PRESENT_VALUE:
            # Value interpretation depends on device
            self.device.update_state({"analog_value": value})
            logger.debug(f"[{self.device.ieee}] Analog input: {value}")

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_PRESENT_VALUE:
            return "analog_value"
        return super().get_attr_name(attrid)