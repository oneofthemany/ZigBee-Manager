#!/usr/bin/env python3
"""
Zigbee Serial Interrogator
===========================
Determines serial flow control, baud rate, and adapter type of connected
Zigbee coordinator devices. Differentiates between normal serial devices
and Zigbee adapters.

Supported adapters:
  - Silicon Labs EZSP (Ember) — EFR32/EM35x based (e.g. Elelabs, HUSBZB-1, SkyConnect)
  - Silicon Labs CPC Multi-PAN (RCP) — Zigbee + Thread + Matter over EFR32
  - Dresden Elektronik ConBee / RaspBee (deCONZ serial protocol)
  - Texas Instruments Z-Stack (CC253x / CC26x2 based)

Usage:
  python zigbee_interrogator.py                    # Auto-detect all serial ports
  python zigbee_interrogator.py --port /dev/ttyUSB0  # Probe a specific port
  python zigbee_interrogator.py --verbose            # Detailed logging
"""

import argparse
import glob
import json
import logging
import os
import re
import struct
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pyserial is required.  Install with:  pip install pyserial")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constants & Enums
# ─────────────────────────────────────────────────────────────────────────────

COMMON_BAUD_RATES = [115200, 460800, 230400, 57600, 38400, 19200, 9600]
PROBE_TIMEOUT = 0.8          # seconds per probe attempt
READ_TIMEOUT = 0.6           # read timeout within a probe
MAX_RETRIES = 2              # retries per baud/flow combination

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║              Zigbee Serial Interrogator v1.3                 ║
║  EZSP · CPC/Multi-PAN · ConBee/RaspBee · Z-Stack · Auto      ║
╚══════════════════════════════════════════════════════════════╝
"""


class FlowControl(Enum):
    NONE = "none"
    RTSCTS = "rtscts"
    XONXOFF = "xonxoff"


class AdapterFamily(Enum):
    EZSP = "Silicon Labs EZSP (Ember)"
    CPC_MULTIPAN = "Silicon Labs CPC Multi-PAN (RCP)"
    CONBEE = "Dresden Elektronik ConBee/RaspBee"
    ZSTACK = "Texas Instruments Z-Stack"
    UNKNOWN_ZIGBEE = "Unknown Zigbee-like device"
    NOT_ZIGBEE = "Non-Zigbee serial device"


# ─────────────────────────────────────────────────────────────────────────────
# Result data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdapterInfo:
    """Collected information about a detected adapter."""
    port: str = ""
    adapter_family: AdapterFamily = AdapterFamily.NOT_ZIGBEE
    baud_rate: int = 0
    flow_control: FlowControl = FlowControl.NONE
    firmware_version: str = ""
    stack_version: str = ""
    hardware_id: str = ""
    eui64: str = ""
    board_name: str = ""
    extra: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"  Port             : {self.port}",
            f"  Adapter Family   : {self.adapter_family.value}",
            f"  Baud Rate        : {self.baud_rate}",
            f"  Flow Control     : {self.flow_control.value}",
        ]
        if self.firmware_version:
            lines.append(f"  Firmware Version : {self.firmware_version}")
        if self.stack_version:
            lines.append(f"  Stack Version    : {self.stack_version}")
        if self.eui64:
            lines.append(f"  EUI-64 (MAC)     : {self.eui64}")
        if self.hardware_id:
            lines.append(f"  Hardware ID      : {self.hardware_id}")
        if self.board_name:
            lines.append(f"  Board/Model      : {self.board_name}")
        for k, v in self.extra.items():
            lines.append(f"  {k:17s}: {v}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level serial helpers
# ─────────────────────────────────────────────────────────────────────────────

def open_serial(port: str, baud: int, flow: FlowControl, timeout: float = READ_TIMEOUT) -> serial.Serial:
    """Open a serial port safely.
    
    Prevents EFR32 adapters from accidentally entering the Gecko Bootloader by
    ensuring DTR (Reset) and RTS (Boot) are de-asserted (False = High Voltage)
    before and immediately after opening the port.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = timeout
    ser.write_timeout = timeout
    
    # Pre-set to avoid bootloader trap
    ser.dtr = False
    ser.rts = False

    if flow == FlowControl.XONXOFF:
        ser.xonxoff = True
    elif flow == FlowControl.RTSCTS:
        ser.rtscts = True

    ser.open()

    # Force de-assert immediately in case OS driver toggled them during open()
    ser.dtr = False
    ser.rts = False

    return ser


def flush_port(ser: serial.Serial):
    """Drain any stale data from buffers."""
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    time.sleep(0.05)
    try:
        ser.read(4096)
    except Exception:
        pass


def safe_write_read(ser: serial.Serial, payload: bytes, read_len: int = 256,
                    delay: float = 0.15) -> bytes:
    """Write payload, wait, read response.  Returns bytes (may be empty).
    Uses a hard wall-clock timeout via threading to prevent indefinite hangs
    when hardware flow control blocks the write."""
    result = [b""]

    def _do_io():
        try:
            flush_port(ser)
            ser.write(payload)
            ser.flush()
            time.sleep(delay)
            result[0] = ser.read(read_len)
        except (serial.SerialException, OSError) as exc:
            logging.debug("Serial I/O error: %s", exc)
            result[0] = b""

    hard_timeout = delay + ser.timeout + 1.5   # generous wall-clock limit
    t = threading.Thread(target=_do_io, daemon=True)
    t.start()
    t.join(timeout=hard_timeout)

    if t.is_alive():
        logging.debug("Serial I/O hard timeout (%.1fs) — likely flow control hang", hard_timeout)
        try:
            ser.cancel_write()
        except Exception:
            pass
        try:
            ser.cancel_read()
        except Exception:
            pass
        return b""

    return result[0]


# ─────────────────────────────────────────────────────────────────────────────
# EZSP (Ember) detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class EZSPProbe:
    """
    Probe for Silicon Labs EZSP (EmberZNet Serial Protocol).
    """

    ASH_FLAG = 0x7E
    ASH_RST = bytes([0x1A, 0xC0, 0x38, 0xBC, 0x7E])
    ASH_RSTACK_MARKER = bytes([0x1A, 0xC1])

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls.ASH_RST

    @staticmethod
    def _stuff(data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if b in (0x7E, 0x11, 0x13, 0x18, 0x1A, 0x7D):
                out.append(0x7D)
                out.append(b ^ 0x20)
            else:
                out.append(b)
        return bytes(out)

    @staticmethod
    def _unstuff(data: bytes) -> bytes:
        out = bytearray()
        escape = False
        for b in data:
            if escape:
                out.append(b ^ 0x20)
                escape = False
            elif b == 0x7D:
                escape = True
            else:
                out.append(b)
        return bytes(out)

    @staticmethod
    def _crc_ccitt(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    @staticmethod
    def _randomize_ash_data(seed: int, data: bytes) -> bytes:
        out = bytearray()
        rand = seed
        for b in data:
            if rand & 1:
                rand = (rand >> 1) ^ 0xB8
            else:
                rand = rand >> 1
            out.append(b ^ rand)
        return bytes(out)

    @classmethod
    def _build_ash_data_frame(cls, seq: int, ack: int, ezsp_frame: bytes) -> bytes:
        control = ((seq & 0x07) << 4) | (ack & 0x07)
        rand_byte = 0x42
        randomized_ezsp = cls._randomize_ash_data(rand_byte, ezsp_frame)
        inner = bytes([control, rand_byte]) + randomized_ezsp
        crc = cls._crc_ccitt(inner)
        inner += struct.pack(">H", crc)
        return cls._stuff(inner) + bytes([cls.ASH_FLAG])

    @staticmethod
    def _build_ezsp_version_cmd(desired_version: int, seq: int = 0) -> bytes:
        if desired_version >= 8:
            return bytes([seq, 0x00, 0x01, 0x00, 0x00, desired_version])
        else:
            return bytes([0x00, 0x00, desired_version])

    @classmethod
    def _parse_ash_response(cls, raw: bytes) -> Optional[bytes]:
        frames = raw.split(bytes([cls.ASH_FLAG]))
        for frame_data in frames:
            if len(frame_data) < 4:
                continue
            unstuffed = cls._unstuff(frame_data)
            if len(unstuffed) < 5:
                continue
            control = unstuffed[0]
            if control & 0x80 == 0:
                rand_byte = unstuffed[1]
                randomized_ezsp = unstuffed[2:-2]
                ezsp_frame = cls._randomize_ash_data(rand_byte, randomized_ezsp)
                return ezsp_frame
        return None

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        resp = safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.3)
        if not resp:
            return None
        if 0xC1 in resp and cls.ASH_FLAG in resp:
            logging.debug("EZSP: Got RSTACK response (%d bytes)", len(resp))
            return resp
        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        info.adapter_family = AdapterFamily.EZSP
        print("  ┌─ EZSP Interrogation ─────────────────────────────────")

        print("  │ Sending ASH RST...", end="", flush=True)
        resp = safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.4)
        if resp:
            print(f" RSTACK ({len(resp)} bytes)")
        else:
            print(" no response")

        version_found = False
        seq_num = 0

        for ezsp_ver in [4, 13, 14, 12, 11, 8, 7]:
            print(f"  │ Query EZSP version (trying v{ezsp_ver})...", end="", flush=True)
            ezsp_cmd = cls._build_ezsp_version_cmd(ezsp_ver, seq=seq_num)
            ash_frame = cls._build_ash_data_frame(seq=seq_num, ack=0, ezsp_frame=ezsp_cmd)
            resp = safe_write_read(ser, ash_frame, read_len=128, delay=0.3)
            
            if resp:
                print(f" got {len(resp)} bytes", end="", flush=True)
                payload = cls._parse_ash_response(resp)
                if payload:
                    parsed = cls._parse_version_response(payload, ezsp_ver)
                    if parsed:
                        info.firmware_version = f"EZSP v{parsed['protocol_version']}"
                        info.stack_version = f"EmberZNet {parsed['stack_version']}"
                        info.extra["Stack Type"] = parsed['stack_type']
                        print(f" → EZSP v{parsed['protocol_version']}, EmberZNet {parsed['stack_version']}")
                        version_found = True
                        break
                    else:
                        print(" (unrecognized EZSP payload)")
                else:
                    print(" (no parseable EZSP payload)")
            else:
                print(" timeout")

            if not version_found and not resp:
                safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.3)
                seq_num = 0
            else:
                seq_num = (seq_num + 1) & 0xFF

        if not version_found:
            print("  │ ⚠ Could not read EZSP version")

        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")
        print("  └─────────────────────────────────────────────────────")

    @staticmethod
    def _parse_version_response(payload: bytes, requested_ver: int) -> Optional[dict]:
        if not payload:
            return None
        if len(payload) >= 9 and payload[1] == 0x80 and payload[2] == 0x01 and payload[3:5] == b'\x00\x00':
            proto_ver, stack_type_raw = payload[5], payload[6]
            stack_ver = struct.unpack("<H", payload[7:9])[0]
            return {
                "protocol_version": proto_ver,
                "stack_type": "Mesh" if stack_type_raw == 2 else f"Type {stack_type_raw}",
                "stack_version": f"{(stack_ver >> 12) & 0xF}.{(stack_ver >> 8) & 0xF}.{stack_ver & 0xFF}",
            }
        if len(payload) >= 6 and payload[0] == 0x80 and payload[1] == 0x00:
            proto_ver, stack_type_raw = payload[2], payload[3]
            stack_ver = struct.unpack("<H", payload[4:6])[0]
            return {
                "protocol_version": proto_ver,
                "stack_type": "Mesh" if stack_type_raw == 2 else f"Type {stack_type_raw}",
                "stack_version": f"{(stack_ver >> 12) & 0xF}.{(stack_ver >> 8) & 0xF}.{stack_ver & 0xFF}",
            }
        return None

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                prod_lower = (p.product or p.description or "").lower()
                mfg = p.manufacturer or ""

                known_boards = {
                    ("10C4", "EA60"): "Silicon Labs CP2102 based adapter",
                    ("10C4", "8A2A"): "Nortek HUSBZB-1 (dual Zigbee/Z-Wave)",
                    ("1A86", "55D4"): "SkyConnect / Nabu Casa",
                    ("1A86", "7523"): "CH340 based Zigbee adapter",
                }
                if p.vid and p.pid:
                    key = (f"{p.vid:04X}", f"{p.pid:04X}")
                    if key in known_boards:
                        info.board_name = known_boards[key]

                if "skyconnect" in prod_lower or "nabu" in prod_lower:
                    info.board_name = "Nabu Casa SkyConnect (EFR32MG21)"
                elif "elelabs" in prod_lower:
                    info.board_name = "Elelabs Zigbee adapter (EFR32)"
                elif "sonoff" in prod_lower:
                    info.board_name = "SONOFF Zigbee Coordinator"
                elif "husbzb" in prod_lower:
                    info.board_name = "Nortek HUSBZB-1"
                elif not info.board_name and p.product:
                    info.board_name = f"{mfg} {p.product}".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# CPC / Multi-PAN (RCP) detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class CPCMultiPANProbe:
    """
    Probe for Silicon Labs CPC (Co-Processor Communication) protocol.
    CPC framing: FLAG(0x7E) + Address(1) + Length(2) + Control(1) + HCS(2) + [Payload] + FCS(2) + FLAG(0x7E)
    """

    HDLC_FLAG = 0x7E
    EP_SYSTEM = 0x00
    CMD_PROP_GET = 0x02

    PROP_PROTOCOL_VERSION = 0x03
    PROP_CAPABILITIES = 0x04
    PROP_SECONDARY_CPC_VERSION = 0x05

    @classmethod
    def get_test_payload(cls) -> bytes:
        # SABM is an unnumbered HDLC frame used to initialize the CPC connection
        return cls._build_cpc_frame(address=0x00, control=0x3F)

    @staticmethod
    def _crc16_ccitt(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    @classmethod
    def _build_cpc_frame(cls, address: int, control: int, payload: bytes = b"") -> bytes:
        length = len(payload)
        header = struct.pack("<BHB", address, length, control)
        hcs = cls._crc16_ccitt(header)
        
        frame_body = header + struct.pack("<H", hcs) + payload
        fcs = cls._crc16_ccitt(payload)
        frame_body += struct.pack("<H", fcs)

        encoded = bytearray([cls.HDLC_FLAG])
        for b in frame_body:
            if b == 0x7E:
                encoded.extend([0x7D, 0x5E])
            elif b == 0x7D:
                encoded.extend([0x7D, 0x5D])
            else:
                encoded.append(b)
        encoded.append(cls.HDLC_FLAG)
        return bytes(encoded)

    @classmethod
    def _build_prop_get(cls, prop_id: int) -> bytes:
        cpc_payload = bytes([cls.EP_SYSTEM, cls.CMD_PROP_GET, prop_id])
        return cls._build_cpc_frame(address=0x00, control=0x03, payload=cpc_payload)

    @classmethod
    def _parse_hdlc_response(cls, raw: bytes) -> list[dict]:
        results = []
        in_frame = False
        frame_bytes = bytearray()

        i = 0
        while i < len(raw):
            b = raw[i]
            if b == cls.HDLC_FLAG:
                if in_frame and len(frame_bytes) > 0:
                    unstuffed = bytearray()
                    j = 0
                    while j < len(frame_bytes):
                        if frame_bytes[j] == 0x7D and j + 1 < len(frame_bytes):
                            unstuffed.append(frame_bytes[j + 1] ^ 0x20)
                            j += 2
                        else:
                            unstuffed.append(frame_bytes[j])
                            j += 1

                    if len(unstuffed) >= 6: 
                        address = unstuffed[0]
                        length = struct.unpack("<H", unstuffed[1:3])[0]
                        control = unstuffed[3]
                        
                        payload_len = min(length, len(unstuffed) - 8)
                        if payload_len < 0:
                            payload_len = 0
                            
                        payload = bytes(unstuffed[6:6+payload_len])
                        results.append({
                            "address": address,
                            "length": length,
                            "control": control,
                            "raw": unstuffed.hex(),
                            "payload": payload,
                        })
                in_frame = True
                frame_bytes = bytearray()
            elif in_frame:
                frame_bytes.append(b)
            i += 1
        return results

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        # Try SABM first to establish connection
        sabm = cls._build_cpc_frame(address=0x00, control=0x3F)
        resp_sabm = safe_write_read(ser, sabm, read_len=128, delay=0.3)
        if resp_sabm and cls.HDLC_FLAG in resp_sabm:
            logging.debug("CPC: Got SABM response")
            return resp_sabm

        # Try CPC property-get fallback
        frame = cls._build_prop_get(cls.PROP_PROTOCOL_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp and cls.HDLC_FLAG in resp:
            logging.debug("CPC: Got HDLC Prop response")
            return resp

        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        info.adapter_family = AdapterFamily.CPC_MULTIPAN
        print("  ┌─ CPC Multi-PAN (RCP) Interrogation ──────────────────")

        print("  │ Query CPC protocol version...", end="", flush=True)
        frame = cls._build_prop_get(cls.PROP_PROTOCOL_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames and hdlc_frames[0]["payload"]:
                payload = hdlc_frames[0]["payload"]
                if len(payload) >= 3:
                    ver_data = payload[2:]
                    if len(ver_data) >= 1:
                        info.firmware_version = f"CPC protocol v{ver_data[0]}"
                        print(f" v{ver_data[0]}")
                    else:
                        print(f" raw: {payload.hex()}")
                else:
                    print(" short payload")
            else:
                print(" no parseable HDLC")
        else:
            print(" timeout")

        print("  │ Query CPC firmware version...", end="", flush=True)
        frame = cls._build_prop_get(cls.PROP_SECONDARY_CPC_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames and hdlc_frames[0]["payload"]:
                payload = hdlc_frames[0]["payload"]
                if len(payload) >= 3:
                    ver_data = payload[2:]
                    if len(ver_data) >= 3:
                        info.stack_version = f"CPC {ver_data[0]}.{ver_data[1]}.{ver_data[2]}"
                        print(f" {info.stack_version}")
                    else:
                        print(" short payload")
            else:
                print(" no parse")
        else:
            print(" timeout")

        print("  │ Query capabilities...", end="", flush=True)
        frame = cls._build_prop_get(cls.PROP_CAPABILITIES)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames and hdlc_frames[0]["payload"]:
                payload = hdlc_frames[0]["payload"]
                cap_data = payload[2:] if len(payload) >= 3 else payload
                if cap_data:
                    caps = int.from_bytes(cap_data[:min(4, len(cap_data))], "little")
                    cap_names = []
                    if caps & 0x01: cap_names.append("Zigbee")
                    if caps & 0x02: cap_names.append("Thread")
                    if caps & 0x04: cap_names.append("BLE")
                    if caps & 0x08: cap_names.append("Matter")
                    info.extra["Capabilities"] = ", ".join(cap_names) if cap_names else f"0x{caps:08X}"
                    print(f" {info.extra['Capabilities']}")
                else:
                    print(" empty")
            else:
                print(" no parse")
        else:
            print(" timeout")

        info.extra["Firmware Type"] = "Multi-PAN RCP (not NCP/EZSP)"

        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")
        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                product = (p.product or "").lower()
                mfg = (p.manufacturer or "").lower()

                if "skyconnect" in product or "nabu" in product:
                    info.board_name = "Nabu Casa SkyConnect (Multi-PAN)"
                elif "sonoff" in product:
                    chip = "MG24" if "mg24" in product else "MG21" if "mg21" in product else ""
                    info.board_name = f"SONOFF Zigbee Dongle {chip} (Multi-PAN)".strip()
                elif "elelabs" in product:
                    info.board_name = "Elelabs (Multi-PAN)"
                elif not info.board_name:
                    info.board_name = f"{mfg} {p.product} (Multi-PAN RCP)".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# ConBee / RaspBee detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class ConBeeProbe:
    """
    Probe for Dresden Elektronik ConBee / RaspBee adapters.
    """

    SLIP_END = 0xC0
    CMD_DEVICE_STATE = 0x07
    CMD_VERSION = 0x0D
    CMD_READ_PARAM = 0x0A
    PARAM_MAC_ADDRESS = 0x01

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls._build_frame(cls.CMD_VERSION)

    @classmethod
    def _build_frame(cls, command: int, payload: bytes = b"") -> bytes:
        seq = 0x01
        frame_len = 5 + len(payload)
        frame = struct.pack("<BBBH", command, seq, 0x00, frame_len) + payload

        crc = (~sum(frame) + 1) & 0xFFFF
        frame += struct.pack("<H", crc)

        encoded = bytearray([cls.SLIP_END])
        for b in frame:
            if b == 0xC0: encoded.extend([0xDB, 0xDC])
            elif b == 0xDB: encoded.extend([0xDB, 0xDD])
            else: encoded.append(b)
        encoded.append(cls.SLIP_END)
        return bytes(encoded)

    @classmethod
    def _parse_frame(cls, raw: bytes) -> Optional[tuple]:
        start = raw.find(bytes([cls.SLIP_END]))
        if start < 0: return None

        decoded = bytearray()
        escape = False
        for b in raw[start + 1:]:
            if b == cls.SLIP_END: break
            if escape:
                if b == 0xDC: decoded.append(0xC0)
                elif b == 0xDD: decoded.append(0xDB)
                else: decoded.append(b)
                escape = False
            elif b == 0xDB: escape = True
            else: decoded.append(b)

        if len(decoded) < 7: return None
        return (decoded[0], decoded[2], bytes(decoded[5:-2]) if len(decoded) > 7 else b"")

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        frame = cls._build_frame(cls.CMD_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp and cls._parse_frame(resp):
            return resp

        frame2 = cls._build_frame(cls.CMD_DEVICE_STATE)
        resp2 = safe_write_read(ser, frame2, read_len=128, delay=0.3)
        if resp2 and cls._parse_frame(resp2):
            return resp2

        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        info.adapter_family = AdapterFamily.CONBEE
        print("  ┌─ ConBee/RaspBee Interrogation ───────────────────────")

        print("  │ Query firmware version...", end="", flush=True)
        frame = cls._build_frame(cls.CMD_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and parsed[0] == cls.CMD_VERSION and len(parsed[2]) >= 4:
                ver_num = struct.unpack("<I", parsed[2][:4])[0]
                platform_id = ver_num & 0xFF
                info.firmware_version = f"{(ver_num >> 24) & 0xFF}.{(ver_num >> 16) & 0xFF}.{(ver_num >> 8) & 0xFF}"
                
                platform_names = {0x03: "ConBee", 0x04: "RaspBee", 0x05: "ConBee II", 0x07: "ConBee III"}
                info.board_name = platform_names.get(platform_id, f"Platform 0x{platform_id:02X}")
                info.stack_version = f"deCONZ firmware {info.firmware_version}"
                print(f" {info.board_name} firmware {info.firmware_version}")
            else:
                print(" unparseable")
        else:
            print(" timeout")

        print("  │ Query MAC address...", end="", flush=True)
        mac_payload = struct.pack("<BHB", cls.PARAM_MAC_ADDRESS, 0x0000, 0x08)
        frame = cls._build_frame(cls.CMD_READ_PARAM, mac_payload)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and len(parsed[2]) >= 8:
                info.eui64 = ":".join(f"{b:02X}" for b in reversed(parsed[2][-8:]))
                print(f" {info.eui64}")
            else:
                print(" unparseable")
        else:
            print(" timeout")

        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")
        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                prod = (p.product or "").lower()
                if "conbee" in prod or "raspbee" in prod:
                    info.board_name = p.product
                break


# ─────────────────────────────────────────────────────────────────────────────
# Z-Stack (Texas Instruments) detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class ZStackProbe:
    """
    Probe for Texas Instruments Z-Stack (CC253x, CC26x2).
    """

    SOF = 0xFE
    SYS = 0x21
    SYS_PING = 0x01
    SYS_VERSION = 0x02
    SYS_GET_EXT_ADDR = 0x04
    SRSP = 0x60

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls._build_frame(cls.SYS, cls.SYS_PING)

    @classmethod
    def _build_frame(cls, cmd0: int, cmd1: int, payload: bytes = b"") -> bytes:
        frame = bytes([len(payload), cmd0, cmd1]) + payload
        fcs = 0
        for b in frame: fcs ^= b
        return bytes([cls.SOF]) + frame + bytes([fcs])

    @classmethod
    def _parse_frame(cls, raw: bytes) -> Optional[tuple]:
        idx = raw.find(bytes([cls.SOF]))
        if idx < 0 or idx + 4 > len(raw): return None

        data = raw[idx:]
        if len(data) < 5: return None
        length = data[1]
        
        if len(data) < 5 + length: return None
        return (data[2], data[3], data[4:4 + length])

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        frame = cls._build_frame(cls.SYS, cls.SYS_PING)
        resp = safe_write_read(ser, frame, read_len=64, delay=0.25)
        if not resp: return None

        parsed = cls._parse_frame(resp)
        if parsed:
            cmd0, cmd1, _ = parsed
            if cmd0 == (cls.SRSP | cls.SYS) and cmd1 == cls.SYS_PING:
                return resp
            if cmd0 & 0x40:
                return resp
        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        info.adapter_family = AdapterFamily.ZSTACK
        print("  ┌─ Z-Stack Interrogation ──────────────────────────────")

        print("  │ Query SYS_VERSION...", end="", flush=True)
        frame = cls._build_frame(cls.SYS, cls.SYS_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and len(parsed[2]) >= 5:
                payload = parsed[2]
                major, minor, maint = payload[2], payload[3], payload[4]
                info.firmware_version = f"{major}.{minor}.{maint}"
                chip = {0x00: "CC2530", 0x01: "CC2531", 0x04: "CC2652R/P", 0x06: "CC1352P"}.get(payload[1], "Unknown TI chip")
                
                info.stack_version = "Z-Stack 3.x (Zigbee 3.0)" if major == 3 else f"Z-Stack Home {major}.{minor}"
                print(f" {info.stack_version} on {chip}")
            else:
                print(" unparseable")
        else:
            print(" timeout")

        print("  │ Query EUI-64 MAC...", end="", flush=True)
        frame = cls._build_frame(cls.SYS, cls.SYS_GET_EXT_ADDR)
        resp = safe_write_read(ser, frame, read_len=64, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and len(parsed[2]) >= 8:
                info.eui64 = ":".join(f"{b:02X}" for b in reversed(parsed[2][:8]))
                print(f" {info.eui64}")
            else:
                print(" unparseable")
        else:
            print(" timeout")

        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")
        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                prod = (p.product or "").lower()
                mfg = (p.manufacturer or "").lower()

                if "cc2652" in prod or "cc2652" in mfg: info.board_name = "TI CC2652 based coordinator"
                elif "cc2531" in prod: info.board_name = "TI CC2531 USB stick"
                elif "sonoff" in prod: info.board_name = "SONOFF Zigbee 3.0 Dongle Plus"
                elif "zzh" in prod: info.board_name = "Electrolama zig-a-zig-ah! (zzh)"
                elif "slzb" in prod: info.board_name = "SMLIGHT SLZB adapter"
                elif not info.board_name: info.board_name = f"{mfg} {p.product}".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# Main Interrogator orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ZigbeeInterrogator:

    PREFERRED = {
        "ezsp": [(115200, FlowControl.NONE), (460800, FlowControl.NONE), (115200, FlowControl.RTSCTS)],
        "cpc": [(115200, FlowControl.NONE), (460800, FlowControl.NONE), (230400, FlowControl.NONE)],
        "conbee": [(115200, FlowControl.NONE), (38400, FlowControl.NONE)],
        "zstack": [(115200, FlowControl.NONE), (115200, FlowControl.RTSCTS)],
    }

    KNOWN_USB_ZIGBEE = {
        (0x10C4, 0xEA60): ("ezsp",   "Silicon Labs CP210x UART Bridge"),
        (0x10C4, 0x8A2A): ("ezsp",   "Nortek HUSBZB-1 (Zigbee/Z-Wave)"),
        (0x1A86, 0x55D4): ("ezsp",   "Nabu Casa SkyConnect"),
        (0x1CF1, 0x0030): ("conbee", "Dresden Elektronik ConBee II"),
        (0x0403, 0x6015): ("conbee", "Dresden Elektronik ConBee (FTDI)"),
        (0x0451, 0x16A8): ("zstack", "TI CC2531 USB"),
        (0x10C4, 0x8B34): ("zstack", "Electrolama zzh (CC2652)"),
        (0x1A86, 0x7523): (None,     "CH340 (possible Zigbee adapter)"),
    }

    KNOWN_NON_ZIGBEE_USB = {
        (0x1D6B, 0x0002), (0x1D6B, 0x0003), (0x8087, None), (0x0A5C, None),
        (0x27C6, None), (0x8086, 0x0B63), (0x046D, None), (0x04F2, None),
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results: list[AdapterInfo] = []

    def _is_known_non_zigbee(self, vid: int, pid: int) -> bool:
        return (vid, pid) in self.KNOWN_NON_ZIGBEE_USB or (vid, None) in self.KNOWN_NON_ZIGBEE_USB

    def _run_lsusb(self) -> list[dict]:
        devices = []
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    m = re.match(r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s+(.*)", line)
                    if m:
                        devices.append({
                            "bus": m.group(1), "device": m.group(2),
                            "vid": int(m.group(3), 16), "pid": int(m.group(4), 16),
                            "description": m.group(5).strip(),
                        })
        except Exception:
            pass
        return devices

    def _map_usb_to_tty(self, bus: str, device: str, vid: int, pid: int) -> Optional[str]:
        sys_pattern = f"/sys/bus/usb/devices/{int(bus)}-*"
        for usb_path in glob.glob(sys_pattern):
            for root, dirs, files in os.walk(usb_path):
                if "tty" in dirs:
                    for tty_name in os.listdir(os.path.join(root, "tty")):
                        tty_path = f"/dev/{tty_name}"
                        if os.path.exists(tty_path): return tty_path
                for fname in files:
                    if fname.startswith(("ttyUSB", "ttyACM")):
                        if os.path.exists(f"/dev/{fname}"): return f"/dev/{fname}"

        for p in serial.tools.list_ports.comports():
            if p.vid == vid and p.pid == pid: return p.device
        return None

    def discover_candidates(self) -> list[dict]:
        candidates, seen_ports, col_w = [], set(), 58
        print(f"\n  ┌─ USB Device Discovery {'─' * (col_w - 24)}┐")

        usb_devices = self._run_lsusb()
        if usb_devices:
            print(f"  │ {'lsusb: ' + str(len(usb_devices)) + ' USB device(s) on bus':<{col_w}}│")
            for dev in usb_devices:
                vid, pid, desc = dev["vid"], dev["pid"], dev["description"]
                if self._is_known_non_zigbee(vid, pid): continue

                known = self.KNOWN_USB_ZIGBEE.get((vid, pid))
                priority = 1 if known else 2
                if not known and not any(k in desc.lower() for k in ["uart", "serial", "cp210", "ch340", "ftdi"]):
                    continue

                tty = self._map_usb_to_tty(dev["bus"], dev["device"], vid, pid)
                if tty and tty not in seen_ports:
                    seen_ports.add(tty)
                    candidates.append({"port": tty, "vid": vid, "pid": pid, "description": desc,
                                       "likely_family": known[0] if known else None, "priority": priority})
                    label = f"{'✓' if known else '?'} {vid:04X}:{pid:04X} {known[1] if known else desc}"
                    print(f"  │ {label:<{col_w}}│\n  │   {'└─ mapped → ' + tty:<{col_w}}│")
        
        for p in serial.tools.list_ports.comports():
            if p.device in seen_ports or self._is_known_non_zigbee(p.vid or 0, p.pid or 0): continue
            known = self.KNOWN_USB_ZIGBEE.get((p.vid, p.pid))
            candidates.append({"port": p.device, "vid": p.vid, "pid": p.pid,
                               "description": p.product or "", "likely_family": known[0] if known else None,
                               "priority": 1 if known else 2})
            seen_ports.add(p.device)
            print(f"  │ {'✓' if known else '?'} {p.vid or 0:04X}:{p.pid or 0:04X} → {p.device:<{col_w - 18}}│")

        candidates.sort(key=lambda c: c["priority"])
        if not candidates: print(f"  │ {'⚠ No USB serial / UART bridges found':<{col_w}}│")
        else: print(f"  │ {'':<{col_w}}│\n  │ {f'Candidate serial ports: {len(candidates)}':<{col_w}}│")
        print(f"  └{'─' * col_w}┘")
        return candidates

    def _try_probe(self, port: str, baud: int, flow: FlowControl,
                   probe_cls, name: str) -> Optional[serial.Serial]:
        for attempt in range(MAX_RETRIES):
            ser = None
            try:
                ser = open_serial(port, baud, flow)
                flush_port(ser)
                if probe_cls.detect(ser):
                    logging.info("  ✓ %s detected at %d baud, flow=%s (attempt %d)",
                                 name, baud, flow.value, attempt + 1)
                    return ser
                ser.close()
            except (serial.SerialException, OSError, TimeoutError) as exc:
                if ser:
                    try: ser.close()
                    except Exception: pass
        return None

    def _test_flow_mode(self, port: str, baud: int, flow: FlowControl,
                        probe_cls, rounds: int = 3) -> dict:
        successes, total_bytes, resp_times = 0, 0, []
        for _ in range(rounds):
            ser = None
            try:
                ser = open_serial(port, baud, flow)
                flush_port(ser)
                t0 = time.monotonic()
                resp = probe_cls.detect(ser)
                resp_times.append(time.monotonic() - t0)
                if resp:
                    successes += 1
                    total_bytes += len(resp)
                ser.close()
            except (serial.SerialException, OSError, TimeoutError):
                resp_times.append(999.0)
                if ser:
                    try: ser.close()
                    except Exception: pass

        return {"flow": flow, "successes": successes, "rounds": rounds,
                "avg_resp_time": sum(resp_times) / len(resp_times) if resp_times else 999.0,
                "total_bytes": total_bytes}

    def _check_cts_pin(self, port: str, baud: int) -> Optional[bool]:
        ser = None
        try:
            ser = open_serial(port, baud, FlowControl.NONE)
            ser.rts = True
            time.sleep(0.1)
            cts = ser.cts
            ser.close()
            return cts
        except Exception:
            if ser:
                try: ser.close()
                except Exception: pass
            return None

    def _check_xonxoff(self, port: str, baud: int, probe_cls) -> bool:
        ser = None
        try:
            ser = open_serial(port, baud, FlowControl.NONE)
            flush_port(ser)
            ser.write(b'\x13') # XOFF
            ser.flush()
            time.sleep(0.1)

            payload = probe_cls.get_test_payload()
            ser.write(payload)
            ser.flush()
            time.sleep(0.3)
            blocked_resp = ser.read(128)

            ser.write(b'\x11') # XON
            ser.flush()
            time.sleep(0.1)
            flush_port(ser)
            
            ser.write(payload)
            ser.flush()
            time.sleep(0.3)
            resumed_resp = ser.read(128)
            ser.close()

            return len(blocked_resp) == 0 and len(resumed_resp) > 0
        except Exception:
            if ser:
                try: ser.close()
                except Exception: pass
            return False

    def verify_flow_control(self, port: str, baud: int, probe_cls, initial_flow: FlowControl) -> FlowControl:
        print("  ┌─ Flow Control Verification ──────────────────────────")
        print("  │ Checking CTS pin state...", end="", flush=True)
        cts_state = self._check_cts_pin(port, baud)
        cts_info = "unknown" if cts_state is None else "asserted" if cts_state else "not_asserted"
        print(" unreadable" if cts_state is None else " ASSERTED (high)" if cts_state else " NOT asserted (low)")

        test_results = {}
        for flow in [FlowControl.NONE, FlowControl.RTSCTS, FlowControl.XONXOFF]:
            label = flow.value.upper().ljust(7)
            print(f"  │ Testing {label}...", end="", flush=True)
            stats = self._test_flow_mode(port, baud, flow, probe_cls, rounds=3)
            test_results[flow] = stats
            status = "✓" if stats["successes"] == 3 else "✗" if stats["successes"] == 0 else "~"
            print(f" {status} {stats['successes']}/{stats['rounds']} success, avg {stats['avg_resp_time']*1000:.0f}ms, {stats['total_bytes']} bytes")

        print("  │ Testing XON/XOFF behaviour...", end="", flush=True)
        xonxoff_active = self._check_xonxoff(port, baud, probe_cls)
        print(" responds to XON/XOFF" if xonxoff_active else " ignores XON/XOFF")
        print("  │")

        scores = {}
        for flow, stats in test_results.items():
            score = stats["successes"] * 100
            if stats["avg_resp_time"] < 0.5: score += 20
            elif stats["avg_resp_time"] < 1.0: score += 10
            score += min(stats["total_bytes"], 50)

            if flow == FlowControl.RTSCTS and cts_info == "not_asserted": score -= 150
            if flow == FlowControl.RTSCTS and cts_info == "asserted": score += 30
            if flow == FlowControl.XONXOFF and not xonxoff_active: score -= 50
            if flow == FlowControl.XONXOFF and xonxoff_active: score += 50
            scores[flow] = score

        best_flow = max(scores, key=scores.get)
        for flow in [FlowControl.NONE, FlowControl.RTSCTS, FlowControl.XONXOFF]:
            s = test_results[flow]
            marker = " ← BEST" if flow == best_flow else ""
            print(f"  │ {flow.value.upper().ljust(7)}: score={scores[flow]:+4d}  ({s['successes']}/{s['rounds']} ok, {s['avg_resp_time']*1000:.0f}ms avg){marker}")

        print(f"  │ {'⚡ Corrected' if best_flow != initial_flow else '✓ Confirmed'}: {best_flow.value}")
        print("  └─────────────────────────────────────────────────────")
        return best_flow

    def probe_port(self, port: str, candidate: Optional[dict] = None) -> Optional[AdapterInfo]:
        print(f"\n{'─' * 60}\n  Probing: {port}")
        if candidate and candidate.get("vid"):
            print(f"  USB ID : {candidate['vid']:04X}:{candidate['pid']:04X} — {candidate.get('description','')}")
        print(f"{'─' * 60}")

        likely = candidate.get("likely_family") if candidate else None
        all_protos = [("ezsp", EZSPProbe), ("cpc", CPCMultiPANProbe), ("zstack", ZStackProbe), ("conbee", ConBeeProbe)]
        if likely:
            all_protos.sort(key=lambda x: 0 if x[0] == likely else 1)
            print(f"  Hint   : likely {likely.upper()} — probing that first")

        probes_to_try = []
        if likely and likely in self.PREFERRED:
            probe_cls = next(cls for name, cls in all_protos if name == likely)
            for baud, flow in self.PREFERRED[likely]: probes_to_try.append((baud, flow, probe_cls, likely.upper()))
        
        for name, cls in all_protos:
            if name == likely: continue
            for baud, flow in self.PREFERRED[name]: probes_to_try.append((baud, flow, cls, name.upper()))

        for baud in COMMON_BAUD_RATES:
            for flow in [FlowControl.NONE, FlowControl.RTSCTS]:
                for name, cls in all_protos:
                    if (baud, flow, cls, name.upper()) not in probes_to_try:
                        probes_to_try.append((baud, flow, cls, name.upper()))

        unique_probes = []
        for e in probes_to_try:
            if (e[0], e[1], e[3]) not in [(u[0], u[1], u[3]) for u in unique_probes]: unique_probes.append(e)

        for i, (baud, flow, probe_cls, name) in enumerate(unique_probes):
            print(f"  [{i + 1}/{len(unique_probes)}] Trying {name} @ {baud} baud, flow={flow.value}...", end="", flush=True)
            ser = self._try_probe(port, baud, flow, probe_cls, name)
            if ser:
                print(" DETECTED!")
                ser.close()
                verified_flow = self.verify_flow_control(port, baud, probe_cls, flow)
                info = AdapterInfo(port=port, baud_rate=baud, flow_control=verified_flow)
                
                ser2 = None
                try:
                    ser2 = open_serial(port, baud, verified_flow)
                    flush_port(ser2)
                    probe_cls.interrogate(ser2, info)
                finally:
                    if ser2:
                        try: ser2.close()
                        except Exception: pass

                self.results.append(info)
                return info
            else:
                print(" no", flush=True)

        print("  → No Zigbee adapter detected on this port.")
        info = AdapterInfo(port=port, adapter_family=AdapterFamily.NOT_ZIGBEE)
        for p in serial.tools.list_ports.comports():
            if p.device == port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.board_name = f"{p.manufacturer or ''} {p.product or p.description or ''}".strip()
                break
        self.results.append(info)
        return info

    def scan_all(self, ports: Optional[list[str]] = None) -> list[AdapterInfo]:
        candidates = [{"port": p, "vid": 0, "pid": 0, "description": "", "likely_family": None, "priority": 3} for p in ports] if ports else self.discover_candidates()
        if not candidates:
            print("\n  No candidate serial ports found.")
            return []
        for cand in candidates:
            try: self.probe_port(cand["port"], candidate=cand)
            except Exception as exc: logging.error("Error probing %s: %s", cand["port"], exc)
        return self.results

    def print_report(self):
        zigbee_results = [r for r in self.results if r.adapter_family != AdapterFamily.NOT_ZIGBEE]
        other_results = [r for r in self.results if r.adapter_family == AdapterFamily.NOT_ZIGBEE]
        print(f"\n{'═' * 60}\n  SCAN RESULTS\n{'═' * 60}")

        if zigbee_results:
            print(f"\n  ✅ Zigbee adapters found: {len(zigbee_results)}")
            for i, r in enumerate(zigbee_results, 1): print(f"\n  ── Adapter #{i} {'─' * 44}\n{r.summary()}")
        else:
            print("\n  ⚠  No Zigbee adapters detected.")

        if other_results:
            print(f"\n  ── Non-Zigbee serial ports ({len(other_results)}) {'─' * 25}")
            for r in other_results: print(f"    • {r.port} — {r.board_name or 'unknown device'}{f' [{r.hardware_id}]' if r.hardware_id else ''}")
        print(f"\n{'═' * 60}")

    def export_json(self) -> str:
        return json.dumps([asdict(r) for r in self.results], indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Zigbee Serial Interrogator — detect and identify Zigbee adapters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported adapters:
  EZSP (Ember)     Silicon Labs EFR32/EM35x NCP — SkyConnect, HUSBZB-1, Elelabs, SONOFF-E
  CPC Multi-PAN    Silicon Labs EFR32 RCP — Multi-PAN firmware (Zigbee+Thread+Matter)
  ConBee/RaspBee   Dresden Elektronik — ConBee II/III, RaspBee I/II
  Z-Stack          Texas Instruments  — CC2531, CC2652, SONOFF-P, zzh, SLZB
        """,
    )
    parser.add_argument("-p", "--port", type=str, default=None, help="Specific serial port to probe (default: auto-detect all)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose/debug logging")
    parser.add_argument("-j", "--json", action="store_true", help="Output results as JSON")
    parser.add_argument("-o", "--output", type=str, default=None, help="Write JSON results to file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)-8s %(message)s")
    print(BANNER)

    interrogator = ZigbeeInterrogator(verbose=args.verbose)
    interrogator.scan_all([args.port] if args.port else None)

    if args.json or args.output:
        json_str = interrogator.export_json()
        if args.output:
            with open(args.output, "w") as f: f.write(json_str)
            print(f"\n  JSON results written to: {args.output}")
        if args.json: print(json_str)
    else:
        interrogator.print_report()

if __name__ == "__main__":
    main()
