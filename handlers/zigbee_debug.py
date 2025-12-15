"""
Zigbee Debug Module - Raw Packet Capture and Analysis
Provides comprehensive debugging for Zigbee communication issues.

This module helps troubleshoot issues like:
- Motion sensors not triggering updates
- Missing attribute reports
- Cluster command handling issues
"""
import asyncio
import os
import logging
from logging.handlers import RotatingFileHandler
import json
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from collections import deque
from dataclasses import dataclass, asdict
import traceback

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

os.makedirs("logs", exist_ok=True)

# Create dedicated logger for debug with RotatingFileHandler
debug_logger = logging.getLogger("zigbee.debug")
debug_logger.setLevel(logging.DEBUG)
debug_logger.propagate = False  # Don't propagate to root logger

# File handler with rotation - 10MB per file, keep 5 backups
debug_file_handler = RotatingFileHandler(
    'logs/zigbee_debug.log',
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=5
)
debug_file_handler.setLevel(logging.DEBUG)
debug_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
debug_logger.addHandler(debug_file_handler)

# Console handler (optional - only when enabled)
debug_console_handler = logging.StreamHandler()
debug_console_handler.setLevel(logging.DEBUG)
debug_console_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
))

logger = debug_logger

# Known cluster names for better readability
CLUSTER_NAMES = {
    0x0000: "Basic",
    0x0001: "Power Configuration",
    0x0003: "Identify",
    0x0004: "Groups",
    0x0005: "Scenes",
    0x0006: "On/Off",
    0x0008: "Level Control",
    0x000A: "Time",
    0x0019: "OTA Upgrade",
    0x0020: "Poll Control",
    0x0102: "Window Covering",
    0x0201: "Thermostat",
    0x0202: "Fan Control",
    0x0300: "Color Control",
    0x0400: "Illuminance Measurement",
    0x0402: "Temperature Measurement",
    0x0403: "Pressure Measurement",
    0x0405: "Relative Humidity",
    0x0406: "Occupancy Sensing",
    0x040D: "CO2 Measurement",
    0x042A: "PM2.5 Measurement",
    0x0500: "IAS Zone",
    0x0502: "IAS WD",
    0x0702: "Metering",
    0x0B04: "Electrical Measurement",
    0xEF00: "Tuya Manufacturer Specific",
    0xFC00: "Manufacturer Specific",
}

# ZCL Command Names
ZCL_GLOBAL_COMMANDS = {
    0x00: "Read Attributes",
    0x01: "Read Attributes Response",
    0x02: "Write Attributes",
    0x04: "Write Attributes Response",
    0x06: "Configure Reporting",
    0x07: "Configure Reporting Response",
    0x0A: "Report Attributes",
    0x0B: "Default Response",
    0x0D: "Discover Attributes",
}

# IAS Zone Commands
IAS_ZONE_COMMANDS = {
    0x00: "Zone Status Change Notification",
    0x01: "Zone Enroll Request",
}

# Occupancy Sensing Attributes
OCCUPANCY_ATTRS = {
    0x0000: "Occupancy",
    0x0001: "Occupancy Sensor Type",
    0x0010: "PIR Occupied to Unoccupied Delay",
    0x0011: "PIR Unoccupied to Occupied Delay",
    0x0012: "PIR Unoccupied to Occupied Threshold",
    # Philips-specific
    0x0030: "Sensitivity",
    0x0031: "Sensitivity Max",
}


@dataclass
class ZigbeePacket:
    """Represents a captured Zigbee packet."""
    timestamp: float
    timestamp_str: str
    ieee: str
    nwk: str
    profile: int
    profile_name: str
    cluster: int
    cluster_name: str
    src_ep: int
    dst_ep: int
    direction: str  # 'RX' or 'TX'
    raw_data: str
    decoded: Dict[str, Any]
    handler_triggered: bool = False
    handler_name: str = ""

    # Extra fields for motion detection analysis
    motion_detected: bool = False
    on_with_timed_off: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return asdict(self)


class ZigbeeDebugger:
    """
    Comprehensive Zigbee packet debugger.
    Captures and analyzes all Zigbee traffic for troubleshooting.
    """

    def __init__(self, max_packets: int = 1000):
        self.packets: deque = deque(maxlen=max_packets)
        self.attribute_updates: deque = deque(maxlen=500)
        self.cluster_commands: deque = deque(maxlen=500)
        self.errors: deque = deque(maxlen=100)
        self.enabled = False  # Default to false
        self.filter_ieee: Optional[str] = None
        self.filter_cluster: Optional[int] = None

        # NEW: Callbacks for real-time streaming
        self._callbacks: List[Callable] = []

        # Statistics
        self.stats = {
            "total_packets": 0,
            "packets_by_cluster": {},
            "packets_by_device": {},
            "attribute_reports": 0,
            "cluster_commands": 0,
            "handler_triggers": 0,
        }

        logger.info("ZigbeeDebugger initialized")

    # --- NEW METHOD FOR STREAMING ---
    def add_callback(self, callback: Callable):
        """Add callback to be notified of new packets."""
        self._callbacks.append(callback)

    def enable(self, file_logging=True):
        """Enable debugging."""
        self.enabled = True
        logger.info(f"Debugging ENABLED (File logging: {file_logging})")
        return {"enabled": True, "file_logging": file_logging}

    def disable(self):
        """Disable debugging."""
        self.enabled = False
        logger.info("Debugging DISABLED")
        return {"enabled": False}

    # RENAMED from capture_raw_message to capture_packet to match core.py
    def capture_packet(
        self,
        sender_ieee: str,
        sender_nwk: int,
        profile: int,
        cluster: int,
        src_ep: int,
        dst_ep: int,
        message: bytes,
        direction: str = "RX"
    ) -> Optional[ZigbeePacket]:
        """Capture and analyze a raw Zigbee message."""
        if not self.enabled:
            return None

        # Apply filters
        if self.filter_ieee and sender_ieee != self.filter_ieee:
            return None
        if self.filter_cluster is not None and cluster != self.filter_cluster:
            return None

        timestamp = time.time()
        timestamp_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        # Get names
        profile_name = "HA" if profile == 0x0104 else f"0x{profile:04X}"
        cluster_name = CLUSTER_NAMES.get(cluster, f"0x{cluster:04X}")

        # Decode message
        decoded = self._decode_message(cluster, message)

        # Check for motion specific indicators in decoding
        is_motion = False
        on_timed = None

        # Logic for motion detection highlighting
        if cluster == 0x0500 and decoded.get("command_id") == 0x00:
             # IAS Zone Status Change
             is_motion = True
        elif cluster == 0x0006 and decoded.get("command_id") == 0x42:
             # On with Timed Off (Hue Motion)
             is_motion = True
             # Try to extract timing info if available
             if "payload" in decoded:
                 # This is a rough extraction, full parsing is better
                 on_timed = {"raw_payload": decoded["payload"]}

        packet = ZigbeePacket(
            timestamp=timestamp,
            timestamp_str=timestamp_str,
            ieee=str(sender_ieee),
            nwk=f"0x{sender_nwk:04X}" if sender_nwk else "Unknown",
            profile=profile,
            profile_name=profile_name,
            cluster=cluster,
            cluster_name=cluster_name,
            src_ep=src_ep,
            dst_ep=dst_ep,
            direction=direction,
            raw_data=message.hex() if message else "",
            decoded=decoded,
            motion_detected=is_motion,
            on_with_timed_off=on_timed
        )

        self.packets.append(packet)
        self.stats["total_packets"] += 1
        self.stats["packets_by_cluster"][cluster_name] = \
            self.stats["packets_by_cluster"].get(cluster_name, 0) + 1
        self.stats["packets_by_device"][sender_ieee] = \
            self.stats["packets_by_device"].get(sender_ieee, 0) + 1

        # Log important events
        self._log_packet(packet)

        # --- NEW: NOTIFY CALLBACKS (STREAMING) ---
        packet_dict = packet.to_dict()
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(packet_dict))
                else:
                    cb(packet_dict)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        return packet

    def _decode_message(self, cluster: int, message: bytes) -> Dict[str, Any]:
        """Decode ZCL message content."""
        if not message or len(message) < 3:
            return {"raw": message.hex() if message else ""}

        decoded = {}

        try:
            # ZCL Frame Header
            frame_ctrl = message[0]
            is_cluster_specific = bool(frame_ctrl & 0x01)
            is_mfr_specific = bool(frame_ctrl & 0x04)
            direction = "server_to_client" if frame_ctrl & 0x08 else "client_to_server"
            disable_default_rsp = bool(frame_ctrl & 0x10)

            decoded["frame_control"] = {
                "cluster_specific": is_cluster_specific,
                "manufacturer_specific": is_mfr_specific,
                "direction": direction,
                "disable_default_response": disable_default_rsp,
            }

            offset = 1

            # Manufacturer code (if present)
            if is_mfr_specific and len(message) > offset + 2:
                mfr_code = int.from_bytes(message[offset:offset+2], 'little')
                decoded["manufacturer_code"] = f"0x{mfr_code:04X}"
                offset += 2

            # TSN
            if len(message) > offset:
                decoded["tsn"] = message[offset]
                offset += 1

            # Command ID
            if len(message) > offset:
                cmd_id = message[offset]
                decoded["command_id"] = cmd_id
                offset += 1

                if is_cluster_specific:
                    # Cluster-specific command
                    if cluster == 0x0500:  # IAS Zone
                        decoded["command_name"] = IAS_ZONE_COMMANDS.get(cmd_id, f"Unknown(0x{cmd_id:02X})")
                        if cmd_id == 0x00 and len(message) > offset + 1:
                            # Zone Status Change Notification
                            zone_status = int.from_bytes(message[offset:offset+2], 'little')
                            decoded["zone_status"] = zone_status
                            decoded["alarm1_motion"] = bool(zone_status & 0x0001)
                            decoded["alarm2"] = bool(zone_status & 0x0002)
                            decoded["tamper"] = bool(zone_status & 0x0004)
                            decoded["battery_low"] = bool(zone_status & 0x0008)
                    else:
                        decoded["command_name"] = f"Cluster Cmd 0x{cmd_id:02X}"
                else:
                    # Global ZCL command
                    decoded["command_name"] = ZCL_GLOBAL_COMMANDS.get(cmd_id, f"Global Cmd 0x{cmd_id:02X}")

                    # Decode Report Attributes (0x0A)
                    if cmd_id == 0x0A and len(message) > offset:
                        decoded["attributes"] = self._decode_attribute_report(
                            cluster, message[offset:]
                        )

            # Remaining payload
            if len(message) > offset:
                decoded["payload"] = message[offset:].hex()

        except Exception as e:
            decoded["decode_error"] = str(e)
            decoded["raw"] = message.hex()

        return decoded

    def _decode_attribute_report(self, cluster: int, data: bytes) -> List[Dict]:
        """Decode attribute report payload."""
        attributes = []
        offset = 0

        try:
            while offset < len(data) - 2:
                attr_id = int.from_bytes(data[offset:offset+2], 'little')
                offset += 2

                if offset >= len(data):
                    break

                data_type = data[offset]
                offset += 1

                # Get attribute name
                if cluster == 0x0406:  # Occupancy
                    attr_name = OCCUPANCY_ATTRS.get(attr_id, f"0x{attr_id:04X}")
                else:
                    attr_name = f"0x{attr_id:04X}"

                # Decode value based on type
                value, consumed = self._decode_zcl_value(data_type, data[offset:])
                offset += consumed

                attributes.append({
                    "id": f"0x{attr_id:04X}",
                    "name": attr_name,
                    "type": f"0x{data_type:02X}",
                    "value": value,
                })

        except Exception as e:
            attributes.append({"decode_error": str(e)})

        return attributes

    def _decode_zcl_value(self, data_type: int, data: bytes) -> tuple:
        """Decode a ZCL typed value."""
        if not data:
            return None, 0

        # Boolean
        if data_type == 0x10:
            return bool(data[0]), 1

        # Uint8
        if data_type == 0x20:
            return data[0], 1

        # Uint16
        if data_type == 0x21:
            return int.from_bytes(data[:2], 'little'), 2

        # Uint32
        if data_type == 0x23:
            return int.from_bytes(data[:4], 'little'), 4

        # Int8
        if data_type == 0x28:
            return int.from_bytes(data[:1], 'little', signed=True), 1

        # Int16
        if data_type == 0x29:
            return int.from_bytes(data[:2], 'little', signed=True), 2

        # Bitmap8
        if data_type == 0x18:
            return data[0], 1

        # Bitmap16
        if data_type == 0x19:
            return int.from_bytes(data[:2], 'little'), 2

        # Enum8
        if data_type == 0x30:
            return data[0], 1

        # String
        if data_type == 0x42:
            length = data[0]
            return data[1:1+length].decode('utf-8', errors='ignore'), 1 + length

        # Default: return hex
        return data[:4].hex(), min(4, len(data))

    def _log_packet(self, packet: ZigbeePacket):
        """Log packet with appropriate level based on importance."""
        # Highlight important events
        is_important = False

        # Occupancy/Motion events are always important
        if packet.cluster in [0x0406, 0x0500]:
            is_important = True

        # Attribute reports are important
        if packet.decoded.get("command_name") == "Report Attributes":
            is_important = True

        # Zone Status Change is very important
        if "Zone Status Change" in packet.decoded.get("command_name", ""):
            is_important = True

        # Format the log message
        msg = (
            f"[{packet.timestamp_str}] {packet.direction} "
            f"[{packet.ieee}] EP{packet.src_ep}->{packet.dst_ep} "
            f"{packet.cluster_name}"
        )

        if "command_name" in packet.decoded:
            msg += f" | {packet.decoded['command_name']}"

        if "attributes" in packet.decoded:
            for attr in packet.decoded["attributes"]:
                msg += f" | {attr['name']}={attr['value']}"

        if "zone_status" in packet.decoded:
            motion = "MOTION" if packet.decoded.get("alarm1_motion") else "clear"
            msg += f" | {motion} (status=0x{packet.decoded['zone_status']:04X})"

        # *** MODIFIED: Add RAW DATA to log as requested ***
        msg += f" | Raw: {packet.raw_data}"

        if is_important:
            logger.warning(f"🔔 {msg}")
        else:
            logger.debug(msg)

    def record_attribute_update(
        self,
        ieee: str,
        cluster_id: int,
        endpoint_id: int,
        attr_id: int,
        value: Any,
        handler_name: str
    ):
        """Record an attribute update from a handler."""
        self.stats["attribute_reports"] += 1
        self.stats["handler_triggers"] += 1

        record = {
            "timestamp": datetime.now().isoformat(),
            "ieee": ieee,
            "endpoint": endpoint_id,
            "cluster_id": cluster_id,
            "cluster_name": CLUSTER_NAMES.get(cluster_id, f"0x{cluster_id:04X}"),
            "attr_id": f"0x{attr_id:04X}",
            "value": str(value),
            "handler": handler_name,
        }

        self.attribute_updates.append(record)

        logger.info(
            f"✅ Handler triggered: [{ieee}] {handler_name} - "
            f"Attr 0x{attr_id:04X} = {value}"
        )

    def record_cluster_command(
        self,
        ieee: str,
        cluster_id: int,
        endpoint_id: int,
        command_id: int,
        args: Any,
        handler_name: str
    ):
        """Record a cluster command received by a handler."""
        self.stats["cluster_commands"] += 1
        self.stats["handler_triggers"] += 1

        record = {
            "timestamp": datetime.now().isoformat(),
            "ieee": ieee,
            "endpoint": endpoint_id,
            "cluster_id": cluster_id,
            "cluster_name": CLUSTER_NAMES.get(cluster_id, f"0x{cluster_id:04X}"),
            "command_id": f"0x{command_id:02X}",
            "args": str(args),
            "handler": handler_name,
        }

        self.cluster_commands.append(record)

        logger.info(
            f"✅ Command received: [{ieee}] {handler_name} - "
            f"Cmd 0x{command_id:02X} args={args}"
        )

    def record_error(self, ieee: str, error: str, context: str = ""):
        """Record an error."""
        record = {
            "timestamp": datetime.now().isoformat(),
            "ieee": ieee,
            "error": error,
            "context": context,
            "traceback": traceback.format_exc(),
        }
        self.errors.append(record)
        logger.error(f"❌ Error [{ieee}]: {error} - {context}")

    def get_packets(
        self,
        limit: int = 100,
        ieee_filter: str = None,
        cluster_filter: int = None,
        importance: str = None
    ) -> List[Dict]:
        """Get captured packets with optional filtering.

        Args:
            limit: Maximum number of packets to return
            ieee_filter: Partial or full IEEE address (case-insensitive, supports partial matches)
            cluster_filter: Cluster ID to filter by
            importance: Filter by importance level ('critical', 'high')
        """
        packets = list(self.packets)

        # IEEE Filter: Support partial matches (case-insensitive)
        # Follows ZHA pattern: flexible matching for user convenience
        if ieee_filter:
            ieee_lower = ieee_filter.lower()
            packets = [p for p in packets if ieee_lower in p.ieee.lower()]

        # Cluster Filter: Exact match on cluster ID
        if cluster_filter is not None:
            packets = [p for p in packets if p.cluster == cluster_filter]

        # Importance Filter: Filter by cluster importance
        if importance == 'critical' or importance == 'high':
             # Important clusters: IAS Zone (0x0500), Occupancy (0x0406)
             # Following ZHA pattern for security/motion-critical clusters
             important_clusters = [0x0500, 0x0406]
             packets = [p for p in packets if p.cluster in important_clusters]

        # Return most recent first (reverse chronological)
        packets = packets[-limit:][::-1]

        return [p.to_dict() for p in packets]

    def get_motion_events(self, limit: int = 50) -> List[Dict]:
        """Get recent motion detection events from packets."""
        motion_packets = [p for p in self.packets if p.motion_detected]
        return [p.to_dict() for p in motion_packets[-limit:][::-1]]

    def get_log_file_contents(self, lines: int = 500) -> str:
        """Read the last N lines of the debug log file."""
        try:
            log_file = 'logs/zigbee_debug.log'
            if not os.path.exists(log_file):
                return "Log file not found."

            with open(log_file, 'r') as f:
                # Read all lines and take the last N
                # Efficient enough for debug log sizes
                content = f.readlines()
                return ''.join(content[-lines:])
        except Exception as e:
            return f"Error reading log file: {e}"

    def get_device_summary(self, ieee: str) -> Dict:
        """Get debug summary for a specific device."""
        packets = [p for p in self.packets if p.ieee == ieee]
        attrs = [a for a in self.attribute_updates if a["ieee"] == ieee]
        cmds = [c for c in self.cluster_commands if c["ieee"] == ieee]
        errors = [e for e in self.errors if e["ieee"] == ieee]

        # Cluster breakdown
        clusters = {}
        for p in packets:
            clusters[p.cluster_name] = clusters.get(p.cluster_name, 0) + 1

        return {
            "ieee": ieee,
            "total_packets": len(packets),
            "attribute_updates": len(attrs),
            "cluster_commands": len(cmds),
            "errors": len(errors),
            "clusters": clusters,
            "recent_packets": [p.to_dict() for p in packets[-20:]],
            "recent_attribute_updates": attrs[-20:],
            "recent_cluster_commands": cmds[-20:],
            "recent_errors": errors[-10:],
        }

    def get_stats(self) -> Dict:
        """Get debugging statistics."""
        return {
            **self.stats,
            "enabled": self.enabled,
            "packets_captured": len(self.packets),
            "attribute_updates_captured": len(self.attribute_updates),
            "cluster_commands_captured": len(self.cluster_commands),
            "errors_captured": len(self.errors),
        }

    def clear(self):
        """Clear all captured data."""
        self.packets.clear()
        self.attribute_updates.clear()
        self.cluster_commands.clear()
        self.errors.clear()
        self.stats = {
            "total_packets": 0,
            "packets_by_cluster": {},
            "packets_by_device": {},
            "attribute_reports": 0,
            "cluster_commands": 0,
            "handler_triggers": 0,
        }
        logger.info("Debugger cleared")


# Global debugger instance
debugger = ZigbeeDebugger()


def get_debugger() -> ZigbeeDebugger:
    """Get the global debugger instance."""
    return debugger