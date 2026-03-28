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
import platform
import re
import struct
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
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
║              Zigbee Serial Interrogator v1.4                 ║
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
    """Open a serial port with specified parameters.

    Includes a robust hardware reset sequence to ensure EFR32 adapters are booted
    into the application and not stuck in the Gecko Bootloader when the OS connects.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = timeout
    ser.write_timeout = timeout
    ser.xonxoff = (flow == FlowControl.XONXOFF)

    # Pre-set to avoid accidental bootloader entry if OS allows
    ser.dtr = False
    ser.rts = False

    ser.open()

    # Hardware Reset Sequence into Application Mode
    # DTR = Reset (active low). RTS = Boot (active low).
    # True asserts the line (pulls it low).
    ser.dtr = True   # Hold in Reset
    ser.rts = False  # Ensure Boot pin is high (App mode)
    time.sleep(0.05)
    ser.dtr = False  # Release Reset
    time.sleep(0.15) # Allow firmware to boot up

    # Now enable RTS/CTS if requested
    if flow == FlowControl.RTSCTS:
        ser.rtscts = True

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
        # Thread is stuck in a blocked write/read — we can't kill it, but
        # we can close the port which will cause the blocked call to raise.
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

    EZSP runs over ASHv2 framing.  We send an ASH RST frame, expect an
    ASH RSTACK response, then query for version, EUI-64, and board info.
    """

    # ASH framing constants
    ASH_FLAG = 0x7E
    ASH_RST = bytes([0x1A, 0xC0, 0x38, 0xBC, 0x7E])   # ASH reset frame
    ASH_RSTACK_MARKER = bytes([0x1A, 0xC1])              # start of RSTACK

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls.ASH_RST

    @staticmethod
    def _stuff(data: bytes) -> bytes:
        """ASH byte-stuffing (escape special bytes)."""
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
        """Remove ASH byte-stuffing."""
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
        """CRC-CCITT used by ASH framing."""
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
        """ASH data byte randomizer. Required for valid payload transmission."""
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
        """Build a full ASH DATA frame wrapping an EZSP command."""
        control = ((seq & 0x07) << 4) | (ack & 0x07)   # DATA frame, retransmit=0
        # Add random byte required by ASHv2 before EZSP payload
        rand_byte = 0x42
        # Apply data randomizer to the payload
        randomized_ezsp = cls._randomize_ash_data(rand_byte, ezsp_frame)
        inner = bytes([control, rand_byte]) + randomized_ezsp
        crc = cls._crc_ccitt(inner)
        inner += struct.pack(">H", crc)
        return cls._stuff(inner) + bytes([cls.ASH_FLAG])

    @staticmethod
    def _build_ezsp_version_cmd(desired_version: int, seq: int = 0) -> bytes:
        """Build an EZSP 'version' command frame for the given protocol version."""
        if desired_version >= 8:
            # Extended format: 1-byte sequence + frame_control + 0x01 (format) +
            #                  2-byte frame_id + parameter
            return bytes([seq,         # sequence
                          0x00,        # frame control
                          0x01,        # frame format version (extended)
                          0x00, 0x00,  # frame ID = 0x0000 (version)
                          desired_version])
        else:
            # Legacy format: frame_control + frame_id + parameter (ASH handles sequence)
            return bytes([0x00,        # frame control
                          0x00,        # frame ID = 0x00 (version)
                          desired_version])

    @classmethod
    def _parse_ash_response(cls, raw: bytes) -> Optional[bytes]:
        """Extract EZSP payload from an ASH DATA response frame."""
        # Find flag boundaries
        frames = raw.split(bytes([cls.ASH_FLAG]))
        for frame_data in frames:
            if len(frame_data) < 4:
                continue
            unstuffed = cls._unstuff(frame_data)
            if len(unstuffed) < 5:
                continue
            control = unstuffed[0]
            # DATA frames have bit 7 = 0
            if control & 0x80 == 0:
                rand_byte = unstuffed[1]
                # Skip control byte + randomiser, strip 2-byte CRC
                randomized_ezsp = unstuffed[2:-2]
                ezsp_frame = cls._randomize_ash_data(rand_byte, randomized_ezsp)
                return ezsp_frame
        return None

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        """Send ASH RST and check for RSTACK response."""
        resp = safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.3)
        if not resp:
            return None
        # RSTACK contains 0xC1 in the response
        if 0xC1 in resp and cls.ASH_FLAG in resp:
            logging.debug("EZSP: Got RSTACK response (%d bytes)", len(resp))
            return resp
        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        """Query EZSP adapter for version and identity information."""
        info.adapter_family = AdapterFamily.EZSP
        print("  ┌─ EZSP Interrogation ─────────────────────────────────")

        # Re-send RST to establish clean state
        print("  │ Sending ASH RST...", end="", flush=True)
        resp = safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.4)
        if resp:
            print(f" RSTACK ({len(resp)} bytes)")
            logging.debug("  │   raw: %s", resp.hex())
        else:
            print(" no response")

        # --- EZSP Version command ---
        version_found = False
        seq_num = 0

        # We probe with 4 first. Even if the adapter is v13, sending a v4 query
        # causes it to respond using legacy framing with its true version number!
        for ezsp_ver in [4, 13, 14, 12, 11, 8, 7]:
            print(f"  │ Query EZSP version (trying v{ezsp_ver})...", end="", flush=True)
            ezsp_cmd = cls._build_ezsp_version_cmd(ezsp_ver, seq=seq_num)
            ash_frame = cls._build_ash_data_frame(seq=seq_num, ack=0, ezsp_frame=ezsp_cmd)
            resp = safe_write_read(ser, ash_frame, read_len=128, delay=0.3)

            if resp:
                print(f" got {len(resp)} bytes", end="", flush=True)
                logging.debug("  │   raw: %s", resp.hex())
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
                        logging.debug("  │   derandomized payload: %s", payload.hex())
                else:
                    print(" (no parseable EZSP payload)")
            else:
                print(" timeout")

            if not version_found and not resp:
                # If communication timed out, reset the ASH state and our sequence
                safe_write_read(ser, cls.ASH_RST, read_len=64, delay=0.3)
                seq_num = 0
            else:
                seq_num = (seq_num + 1) & 0xFF

        if not version_found:
            print("  │ ⚠ Could not read EZSP version (adapter may need reset)")

        # Identify hardware from USB descriptor info
        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")

        print("  └─────────────────────────────────────────────────────")

    @staticmethod
    def _parse_version_response(payload: bytes, requested_ver: int) -> Optional[dict]:
        """Parse an EZSP version response from the derandomized payload."""
        if not payload:
            return None

        # Try extended format (v8+):
        # seq(1), fc(1), format(1), id(2), proto(1), type(1), ver(2)
        if len(payload) >= 9 and payload[1] == 0x80 and payload[2] == 0x01 and payload[3:5] == b'\x00\x00':
            proto_ver = payload[5]
            stack_type_raw = payload[6]
            stack_ver = struct.unpack("<H", payload[7:9])[0]
            major = (stack_ver >> 12) & 0xF
            minor = (stack_ver >> 8) & 0xF
            patch = stack_ver & 0xFF
            return {
                "protocol_version": proto_ver,
                "stack_type": "Mesh" if stack_type_raw == 2 else f"Type {stack_type_raw}",
                "stack_version": f"{major}.{minor}.{patch}",
            }

        # Try legacy format (< v8):
        # fc(1), id(1), proto(1), type(1), ver(2)
        if len(payload) >= 6 and payload[0] == 0x80 and payload[1] == 0x00:
            proto_ver = payload[2]
            stack_type_raw = payload[3]
            stack_ver = struct.unpack("<H", payload[4:6])[0]
            major = (stack_ver >> 12) & 0xF
            minor = (stack_ver >> 8) & 0xF
            patch = stack_ver & 0xFF
            return {
                "protocol_version": proto_ver,
                "stack_type": "Mesh" if stack_type_raw == 2 else f"Type {stack_type_raw}",
                "stack_version": f"{major}.{minor}.{patch}",
            }

        return None

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        """Identify EZSP hardware from USB metadata."""
        port_name = ser.port
        for p in serial.tools.list_ports.comports():
            if p.device == port_name:
                vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.hardware_id = vid_pid
                product = p.product or p.description or ""
                manufacturer = p.manufacturer or ""

                # Known hardware signatures
                known_boards = {
                    ("10C4", "EA60"): "Silicon Labs CP2102 based adapter",
                    ("10C4", "8A2A"): "Nortek HUSBZB-1 (dual Zigbee/Z-Wave)",
                    ("1CF1", "0030"): "Dresden Elektronik ConBee II",
                    ("1A86", "55D4"): "SkyConnect / Nabu Casa",
                    ("1A86", "7523"): "CH340 based Zigbee adapter",
                    ("0403", "6015"): "FTDI FT230X based adapter",
                    ("0403", "6001"): "FTDI FT232 based adapter",
                }

                if p.vid and p.pid:
                    key = (f"{p.vid:04X}", f"{p.pid:04X}")
                    if key in known_boards:
                        info.board_name = known_boards[key]

                # Refine from product string
                prod_lower = product.lower()
                if "skyconnect" in prod_lower or "nabu" in prod_lower:
                    info.board_name = "Nabu Casa SkyConnect (EFR32MG21)"
                elif "elelabs" in prod_lower:
                    info.board_name = "Elelabs Zigbee adapter (EFR32)"
                elif "sonoff" in prod_lower:
                    info.board_name = "SONOFF Zigbee Coordinator"
                elif "tube" in prod_lower and "zigbee" in prod_lower:
                    info.board_name = "Tube's Zigbee Coordinator"
                elif "husbzb" in prod_lower:
                    info.board_name = "Nortek HUSBZB-1"
                elif not info.board_name and product:
                    info.board_name = f"{manufacturer} {product}".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# CPC / Multi-PAN (RCP) detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class CPCMultiPANProbe:
    """
    Probe for Silicon Labs CPC (Co-Processor Communication) protocol.

    Multi-PAN / RCP firmware on EFR32 chips (MG21, MG24) uses CPC over
    HDLC-like framing instead of EZSP/ASH.  This is used when the adapter
    runs as an RCP (Radio Co-Processor) for Thread+Zigbee multi-protocol.

    CPC framing:
      FLAG(0x7E) + ADDRESS(1) + LENGTH(2) + CONTROL(1) + HCS(2) + [DATA...] + FCS(2) + FLAG(0x7E)

    We detect by:
      1. Sending CPC HDLC frames (SABM or Property Get) and checking for valid responses.
    """

    HDLC_FLAG = 0x7E

    # CPC system endpoint commands
    EP_SYSTEM = 0x00
    CMD_PROP_GET = 0x02
    CMD_PROP_SET = 0x03
    CMD_RESET = 0x00

    # CPC system properties
    PROP_PROTOCOL_VERSION = 0x03
    PROP_CAPABILITIES = 0x04
    PROP_SECONDARY_CPC_VERSION = 0x05
    PROP_RX_CAPABILITY = 0x01
    PROP_FC_VALIDATION = 0x02

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls._build_prop_get(cls.PROP_PROTOCOL_VERSION)

    @staticmethod
    def _crc16_ccitt(data: bytes) -> int:
        """CRC-16/CCITT-FALSE used by CPC HDLC."""
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
        """Build a CPC HDLC frame.
        CPC uses a 4-byte header: Address (1), Length (2, LE), Control (1).
        HCS is CRC over header. FCS is CRC over payload only.
        """
        length = len(payload)
        header = struct.pack("<BHB", address, length, control)
        hcs = cls._crc16_ccitt(header)

        # Assemble body up to payload
        frame_body = header + struct.pack("<H", hcs) + payload

        # FCS is computed over payload only in CPC
        fcs = cls._crc16_ccitt(payload)
        frame_body += struct.pack("<H", fcs)

        # HDLC byte-stuff (escape 0x7E and 0x7D)
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
        """Build a CPC system property-get command wrapped in HDLC."""
        # CPC command: endpoint(1) + command_id(1) + property_id(1)
        cpc_payload = bytes([cls.EP_SYSTEM, cls.CMD_PROP_GET, prop_id])
        # HDLC: address = 0 (system), control = UI frame (0x03)
        return cls._build_cpc_frame(address=0x00, control=0x03, payload=cpc_payload)

    @classmethod
    def _parse_hdlc_response(cls, raw: bytes) -> list[dict]:
        """Parse HDLC frames from raw data. Returns list of {address, length, control, payload}."""
        results = []
        in_frame = False
        frame_bytes = bytearray()

        i = 0
        while i < len(raw):
            b = raw[i]
            if b == cls.HDLC_FLAG:
                if in_frame and len(frame_bytes) > 0:
                    # Unstuff
                    unstuffed = bytearray()
                    j = 0
                    while j < len(frame_bytes):
                        if frame_bytes[j] == 0x7D and j + 1 < len(frame_bytes):
                            unstuffed.append(frame_bytes[j + 1] ^ 0x20)
                            j += 2
                        else:
                            unstuffed.append(frame_bytes[j])
                            j += 1

                    # header(4) + hcs(2) + fcs(2) = 8 minimum
                    if len(unstuffed) >= 8:
                        address = unstuffed[0]
                        length = struct.unpack("<H", unstuffed[1:3])[0]
                        control = unstuffed[3]

                        # Validate HCS — reject baud-rate mismatch garbage
                        header = bytes(unstuffed[0:4])
                        expected_hcs = cls._crc16_ccitt(header)
                        actual_hcs = struct.unpack("<H", unstuffed[4:6])[0]
                        if expected_hcs != actual_hcs:
                            logging.debug(
                                "CPC: HCS mismatch (expected %04X, got %04X) — "
                                "likely wrong baud rate", expected_hcs, actual_hcs
                            )
                            i += 1
                            continue

                        # Payload is everything between HCS(4:6) and FCS(-2)
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
        """Detect CPC/Multi-PAN firmware.

        Strategy:
        - Send a CPC property-get for protocol version
        - If we get a valid HDLC response, it's CPC
        - Also try sending an unnumbered HDLC frame (SABM) to see if CPC responds
        """
        # Try 1: CPC property-get
        frame = cls._build_prop_get(cls.PROP_PROTOCOL_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp and cls.HDLC_FLAG in resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames:
                logging.debug("CPC: Got HDLC response, %d frame(s)", len(hdlc_frames))
                return resp

        # Try 2: Send HDLC SABM (Set Asynchronous Balanced Mode)
        # This is the CPC connection establishment frame
        sabm = cls._build_cpc_frame(address=0x00, control=0x3F)  # SABM = 0x3F
        resp = safe_write_read(ser, sabm, read_len=128, delay=0.3)
        if resp and cls.HDLC_FLAG in resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames:
                logging.debug("CPC: Got SABM response, %d frame(s)", len(hdlc_frames))
                return resp

        # Try 3: Send a simple unnumbered poll
        disc = cls._build_cpc_frame(address=0x00, control=0x53)  # DISC
        resp = safe_write_read(ser, disc, read_len=128, delay=0.3)
        if resp and cls.HDLC_FLAG in resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames:
                logging.debug("CPC: Got DISC response")
                return resp

        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        """Query CPC Multi-PAN adapter for version and capability info."""
        info.adapter_family = AdapterFamily.CPC_MULTIPAN
        print("  ┌─ CPC Multi-PAN (RCP) Interrogation ──────────────────")

        # --- Protocol version ---
        print("  │ Query CPC protocol version...", end="", flush=True)
        frame = cls._build_prop_get(cls.PROP_PROTOCOL_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames and hdlc_frames[0]["payload"]:
                payload = hdlc_frames[0]["payload"]
                if len(payload) >= 3:
                    # Response: endpoint(1) + command(1) + version_data
                    ver_data = payload[2:]
                    if len(ver_data) >= 1:
                        info.firmware_version = f"CPC protocol v{ver_data[0]}"
                        print(f" v{ver_data[0]}")
                    else:
                        info.firmware_version = f"CPC protocol (raw: {payload.hex()})"
                        print(f" raw: {payload.hex()}")
                else:
                    print(f" short payload: {payload.hex()}")
            else:
                print(f" got {len(resp)} bytes, no parseable HDLC")
                logging.debug("  │   raw: %s", resp.hex())
        else:
            print(" timeout")

        # --- CPC secondary version ---
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
                        major, minor, patch = ver_data[0], ver_data[1], ver_data[2]
                        info.stack_version = f"CPC {major}.{minor}.{patch}"
                        print(f" {info.stack_version}")
                    elif len(ver_data) >= 1:
                        info.stack_version = f"CPC (raw: {ver_data.hex()})"
                        print(f" raw: {ver_data.hex()}")
                else:
                    print(f" short: {payload.hex()}")
            else:
                print(f" got {len(resp)} bytes, no parse")
        else:
            print(" timeout")

        # --- Capabilities ---
        # PROP_CAPABILITIES is a CPC transport-layer register (encryption,
        # flow-control, GPIO-reset etc.) — NOT a protocol-capability register.
        # Display raw hex only to avoid misleading Zigbee/Thread/Matter labels.
        print("  │ Query capabilities...", end="", flush=True)
        frame = cls._build_prop_get(cls.PROP_CAPABILITIES)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            hdlc_frames = cls._parse_hdlc_response(resp)
            if hdlc_frames and hdlc_frames[0]["payload"]:
                payload = hdlc_frames[0]["payload"]
                cap_data = payload[2:] if len(payload) >= 3 else payload
                if cap_data:
                    raw_caps = int.from_bytes(cap_data[:min(4, len(cap_data))], "little")
                    info.extra["Capabilities"] = f"0x{raw_caps:08X}"
                    print(f" raw=0x{raw_caps:08X}")
                else:
                    print(" empty")
            else:
                print(f" no parse ({len(resp)} bytes)")
        else:
            print(" timeout")

        info.extra["Firmware Type"] = "Multi-PAN RCP (not NCP/EZSP)"

        # Hardware ID from USB
        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")

        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        """Identify hardware from USB descriptor."""
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.hardware_id = vid_pid or info.hardware_id
                product = (p.product or "").lower()
                manufacturer = (p.manufacturer or "").lower()

                if "skyconnect" in product or "nabu" in product:
                    info.board_name = "Nabu Casa SkyConnect (Multi-PAN)"
                elif "sonoff" in product:
                    chip = "MG24" if "mg24" in product else "MG21" if "mg21" in product else ""
                    info.board_name = f"SONOFF Zigbee Dongle {chip} (Multi-PAN)".strip()
                elif "elelabs" in product:
                    info.board_name = "Elelabs (Multi-PAN)"
                elif not info.board_name:
                    prod = p.product or p.description or ""
                    mfg = p.manufacturer or ""
                    info.board_name = f"{mfg} {prod} (Multi-PAN RCP)".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# ConBee / RaspBee detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class ConBeeProbe:
    """
    Probe for Dresden Elektronik ConBee / RaspBee adapters.

    These use a SLIP-like serial protocol with specific command IDs.
    The firmware query command 0x0D returns device/firmware info.
    """

    # deCONZ serial protocol framing
    SLIP_END = 0xC0

    # Command IDs
    CMD_DEVICE_STATE = 0x07
    CMD_VERSION = 0x0D
    CMD_READ_PARAM = 0x0A

    # Parameter IDs for CMD_READ_PARAM
    PARAM_MAC_ADDRESS = 0x01
    PARAM_NWK_PANID = 0x05
    PARAM_NWK_ADDRESS = 0x07
    PARAM_CHANNEL_MASK = 0x09
    PARAM_APS_EXT_PANID = 0x0B
    PARAM_NETWORK_KEY = 0x18

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls._build_frame(cls.CMD_VERSION)

    @classmethod
    def _build_frame(cls, command: int, payload: bytes = b"") -> bytes:
        """Build a deCONZ serial protocol frame."""
        seq = 0x01
        # Frame: command(1) + seq(1) + reserved(1) + frame_length(2) + payload
        frame_len = 5 + len(payload)
        frame = struct.pack("<BBBH", command, seq, 0x00, frame_len) + payload

        # CRC — simple sum mod 65536 inverted
        crc = 0
        for b in frame:
            crc += b
        crc = (~crc + 1) & 0xFFFF
        frame += struct.pack("<H", crc)

        # SLIP encode
        encoded = bytearray([cls.SLIP_END])
        for b in frame:
            if b == 0xC0:
                encoded.extend([0xDB, 0xDC])
            elif b == 0xDB:
                encoded.extend([0xDB, 0xDD])
            else:
                encoded.append(b)
        encoded.append(cls.SLIP_END)
        return bytes(encoded)

    @classmethod
    def _parse_frame(cls, raw: bytes) -> Optional[tuple]:
        """Parse a SLIP-decoded deCONZ response. Returns (command, status, payload) or None."""
        # Find SLIP frame boundaries
        start = raw.find(bytes([cls.SLIP_END]))
        if start < 0:
            return None

        # SLIP decode
        decoded = bytearray()
        escape = False
        for b in raw[start + 1:]:
            if b == cls.SLIP_END:
                break
            if escape:
                if b == 0xDC:
                    decoded.append(0xC0)
                elif b == 0xDD:
                    decoded.append(0xDB)
                else:
                    decoded.append(b)
                escape = False
            elif b == 0xDB:
                escape = True
            else:
                decoded.append(b)

        if len(decoded) < 7:
            return None

        command = decoded[0]
        status = decoded[2]
        frame_len = struct.unpack("<H", decoded[3:5])[0]
        payload = bytes(decoded[5:-2]) if len(decoded) > 7 else b""
        return (command, status, payload)

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        """Send firmware version query and check for valid response."""
        frame = cls._build_frame(cls.CMD_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if not resp:
            return None

        parsed = cls._parse_frame(resp)
        if parsed and parsed[0] == cls.CMD_VERSION and parsed[1] == 0x00:
            logging.debug("ConBee: Got version response")
            return resp

        # Also try device state query as fallback
        frame2 = cls._build_frame(cls.CMD_DEVICE_STATE)
        resp2 = safe_write_read(ser, frame2, read_len=128, delay=0.3)
        if resp2:
            parsed2 = cls._parse_frame(resp2)
            if parsed2 and parsed2[0] == cls.CMD_DEVICE_STATE:
                logging.debug("ConBee: Got device state response")
                return resp2

        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        """Query ConBee/RaspBee for firmware version and MAC."""
        info.adapter_family = AdapterFamily.CONBEE
        print("  ┌─ ConBee/RaspBee Interrogation ───────────────────────")

        # --- Firmware version ---
        print("  │ Query firmware version...", end="", flush=True)
        frame = cls._build_frame(cls.CMD_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and parsed[0] == cls.CMD_VERSION and len(parsed[2]) >= 4:
                payload = parsed[2]
                if len(payload) >= 4:
                    ver_num = struct.unpack("<I", payload[:4])[0]
                    major = (ver_num >> 24) & 0xFF
                    minor = (ver_num >> 16) & 0xFF
                    patch = (ver_num >> 8) & 0xFF
                    platform_id = ver_num & 0xFF
                    info.firmware_version = f"{major}.{minor}.{patch}"

                    platform_names = {
                        0x03: "ConBee",
                        0x04: "RaspBee",
                        0x05: "ConBee II",
                        0x07: "ConBee III",
                        0x06: "RaspBee II",
                    }
                    pname = platform_names.get(platform_id, f"Platform 0x{platform_id:02X}")
                    info.board_name = pname
                    info.stack_version = f"deCONZ firmware {info.firmware_version}"
                    info.extra["Platform ID"] = f"0x{platform_id:02X}"
                    print(f" {pname} firmware {info.firmware_version}")
                    logging.debug("  │   raw: %s", resp.hex())
            else:
                print(f" got {len(resp)} bytes but unparseable")
                logging.debug("  │   raw: %s", resp.hex())
        else:
            print(" timeout")

        # --- MAC Address ---
        print("  │ Query MAC address...", end="", flush=True)
        mac_payload = struct.pack("<BHB", cls.PARAM_MAC_ADDRESS, 0x0000, 0x08)
        frame = cls._build_frame(cls.CMD_READ_PARAM, mac_payload)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and parsed[0] == cls.CMD_READ_PARAM and len(parsed[2]) >= 8:
                mac_bytes = parsed[2][-8:]
                info.eui64 = ":".join(f"{b:02X}" for b in reversed(mac_bytes))
                print(f" {info.eui64}")
            else:
                print(f" got {len(resp)} bytes but unparseable")
        else:
            print(" timeout")

        # Hardware ID from USB info
        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")

        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        """Supplement detection from USB descriptor."""
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.hardware_id = vid_pid or info.hardware_id
                if not info.board_name:
                    prod = (p.product or "").lower()
                    if "conbee" in prod:
                        info.board_name = p.product
                    elif "raspbee" in prod:
                        info.board_name = p.product
                break


# ─────────────────────────────────────────────────────────────────────────────
# Z-Stack (Texas Instruments) detection & interrogation
# ─────────────────────────────────────────────────────────────────────────────

class ZStackProbe:
    """
    Probe for Texas Instruments Z-Stack (CC253x, CC26x2).

    Z-Stack uses the MT (Monitor and Test) serial interface with SOF framing.
    """

    SOF = 0xFE  # Start of frame

    # MT subsystem IDs
    SYS = 0x21       # SYS commands
    SYS_PING = 0x01
    SYS_VERSION = 0x02
    SYS_GET_EXT_ADDR = 0x04

    # Response subsystem bit
    SRSP = 0x60

    @classmethod
    def get_test_payload(cls) -> bytes:
        return cls._build_frame(cls.SYS, cls.SYS_PING)

    @classmethod
    def _build_frame(cls, cmd0: int, cmd1: int, payload: bytes = b"") -> bytes:
        """Build an MT serial frame: SOF + LEN + CMD0 + CMD1 + DATA + FCS."""
        length = len(payload)
        frame = bytes([length, cmd0, cmd1]) + payload
        fcs = 0
        for b in frame:
            fcs ^= b
        return bytes([cls.SOF]) + frame + bytes([fcs])

    @classmethod
    def _parse_frame(cls, raw: bytes) -> Optional[tuple]:
        """Parse an MT response frame. Returns (cmd0, cmd1, payload) or None."""
        # Find SOF
        idx = raw.find(bytes([cls.SOF]))
        if idx < 0 or idx + 4 > len(raw):
            return None

        data = raw[idx:]
        if len(data) < 5:
            return None

        length = data[1]
        cmd0 = data[2]
        cmd1 = data[3]

        if len(data) < 5 + length:
            return None

        payload = data[4:4 + length]

        # Verify FCS
        fcs = 0
        for b in data[1:4 + length]:
            fcs ^= b
        expected_fcs = data[4 + length]
        if fcs != expected_fcs:
            logging.debug("Z-Stack: FCS mismatch (got 0x%02X expected 0x%02X)", fcs, expected_fcs)
            # Still return — some adapters have quirks
            pass

        return (cmd0, cmd1, payload)

    @classmethod
    def detect(cls, ser: serial.Serial) -> Optional[bytes]:
        """Send SYS_PING and check for valid SRSP."""
        frame = cls._build_frame(cls.SYS, cls.SYS_PING)
        resp = safe_write_read(ser, frame, read_len=64, delay=0.25)
        if not resp:
            return None

        parsed = cls._parse_frame(resp)
        if parsed:
            cmd0, cmd1, payload = parsed
            # Response should be SRSP | SYS (0x61), SYS_PING (0x01)
            if cmd0 == (cls.SRSP | cls.SYS) and cmd1 == cls.SYS_PING:
                logging.debug("Z-Stack: Got PING response, capabilities: %s",
                              payload.hex() if payload else "none")
                return resp
            # Some Z-Stack versions respond differently
            if cmd0 & 0x40:  # Any SRSP
                logging.debug("Z-Stack: Got SRSP (cmd0=0x%02X cmd1=0x%02X)", cmd0, cmd1)
                return resp

        return None

    @classmethod
    def interrogate(cls, ser: serial.Serial, info: AdapterInfo):
        """Query Z-Stack adapter for version and EUI-64."""
        info.adapter_family = AdapterFamily.ZSTACK
        print("  ┌─ Z-Stack Interrogation ──────────────────────────────")

        # --- SYS_VERSION ---
        print("  │ Query SYS_VERSION...", end="", flush=True)
        frame = cls._build_frame(cls.SYS, cls.SYS_VERSION)
        resp = safe_write_read(ser, frame, read_len=128, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed:
                cmd0, cmd1, payload = parsed
                if len(payload) >= 5:
                    transport_rev = payload[0]
                    product_id = payload[1]
                    major = payload[2]
                    minor = payload[3]
                    maint = payload[4]
                    info.firmware_version = f"{major}.{minor}.{maint}"
                    info.stack_version = f"Z-Stack {major}.{minor}.{maint}"
                    info.extra["Transport Rev"] = str(transport_rev)
                    info.extra["Product ID"] = f"0x{product_id:02X}"

                    product_names = {
                        0x00: "CC2530",
                        0x01: "CC2531",
                        0x02: "CC2533",
                        0x05: "CC2538",
                        0x04: "CC2652R/P",
                        0x06: "CC1352P",
                    }
                    chip = product_names.get(product_id, f"Unknown TI chip (0x{product_id:02X})")
                    info.extra["Chip"] = chip

                    if major == 3:
                        info.stack_version = f"Z-Stack 3.x (Zigbee 3.0)"
                    elif major >= 1 and minor >= 2:
                        info.stack_version = f"Z-Stack Home {major}.{minor}"

                    print(f" {info.stack_version} on {chip}")
                    logging.debug("  │   raw: %s", resp.hex())
                else:
                    print(f" got {len(resp)} bytes, payload too short ({len(payload)})")
            else:
                print(f" got {len(resp)} bytes but unparseable")
                logging.debug("  │   raw: %s", resp.hex())
        else:
            print(" timeout")

        # --- SYS_GET_EXT_ADDR (EUI-64) ---
        print("  │ Query EUI-64 MAC...", end="", flush=True)
        frame = cls._build_frame(cls.SYS, cls.SYS_GET_EXT_ADDR)
        resp = safe_write_read(ser, frame, read_len=64, delay=0.3)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and len(parsed[2]) >= 8:
                mac_bytes = parsed[2][:8]
                info.eui64 = ":".join(f"{b:02X}" for b in reversed(mac_bytes))
                print(f" {info.eui64}")
            else:
                print(f" got {len(resp)} bytes but unparseable")
        else:
            print(" timeout")

        # --- PING for capabilities ---
        print("  │ Query MT capabilities...", end="", flush=True)
        frame = cls._build_frame(cls.SYS, cls.SYS_PING)
        resp = safe_write_read(ser, frame, read_len=64, delay=0.25)
        if resp:
            parsed = cls._parse_frame(resp)
            if parsed and len(parsed[2]) >= 2:
                caps = struct.unpack("<H", parsed[2][:2])[0]
                cap_list = []
                cap_flags = {
                    0x0001: "SYS", 0x0002: "MAC", 0x0004: "NWK",
                    0x0008: "AF", 0x0010: "ZDO", 0x0020: "SAPI",
                    0x0040: "UTIL", 0x0080: "DEBUG", 0x0100: "APP",
                    0x1000: "ZOAD",
                }
                for flag, name in cap_flags.items():
                    if caps & flag:
                        cap_list.append(name)
                info.extra["MT Capabilities"] = ", ".join(cap_list) if cap_list else f"0x{caps:04X}"
                print(f" {info.extra['MT Capabilities']}")
            else:
                print(f" got {len(resp)} bytes but unparseable")
        else:
            print(" timeout")

        # Hardware ID from USB
        print("  │ Reading USB descriptor...", end="", flush=True)
        cls._identify_hardware(ser, info)
        print(f" {info.board_name or 'unknown'}")

        print("  └─────────────────────────────────────────────────────")

    @classmethod
    def _identify_hardware(cls, ser: serial.Serial, info: AdapterInfo):
        """Supplement from USB descriptor."""
        for p in serial.tools.list_ports.comports():
            if p.device == ser.port:
                vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.hardware_id = vid_pid or info.hardware_id
                prod = (p.product or "").lower()
                mfg = (p.manufacturer or "").lower()

                if "cc2652" in prod or "cc2652" in mfg:
                    info.board_name = "TI CC2652 based coordinator"
                elif "cc2531" in prod:
                    info.board_name = "TI CC2531 USB stick"
                elif "cc1352" in prod or "launchpad" in prod:
                    info.board_name = "TI CC1352/CC2652 LaunchPad"
                elif "sonoff" in prod:
                    info.board_name = "SONOFF Zigbee 3.0 Dongle Plus"
                elif "tubeszb" in prod or "tube" in prod:
                    info.board_name = "Tube's CC2652 Coordinator"
                elif "zzh" in prod:
                    info.board_name = "Electrolama zig-a-zig-ah! (zzh)"
                elif "slzb" in prod:
                    info.board_name = "SMLIGHT SLZB adapter"
                elif not info.board_name:
                    info.board_name = f"{p.manufacturer or ''} {p.product or ''}".strip()
                break


# ─────────────────────────────────────────────────────────────────────────────
# Main Interrogator orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ZigbeeInterrogator:
    """Top-level class that orchestrates detection across protocols and baud rates."""

    # Preferred baud/flow per adapter type (try these first)
    # IMPORTANT: NONE flow control first — most modern adapters (MG24, CC2652,
    # ConBee) do NOT use hardware flow control and RTS/CTS will hang.
    PREFERRED = {
        "ezsp": [(460800, FlowControl.NONE), (115200, FlowControl.NONE),
                 (115200, FlowControl.RTSCTS), (57600, FlowControl.NONE),
                 (57600, FlowControl.RTSCTS)],
        "cpc": [(115200, FlowControl.NONE), (460800, FlowControl.NONE),
                (230400, FlowControl.NONE)],
        "conbee": [(115200, FlowControl.NONE), (38400, FlowControl.NONE)],
        "zstack": [(115200, FlowControl.NONE), (115200, FlowControl.RTSCTS),
                   (57600, FlowControl.NONE)],
    }

    # ── Known USB VID:PID pairs for Zigbee adapters ──────────────────────
    # Maps (VID, PID) → (likely_family, description)
    KNOWN_USB_ZIGBEE = {
        # Silicon Labs CP210x — EZSP adapters
        (0x10C4, 0xEA60): ("ezsp",   "Silicon Labs CP210x UART Bridge"),
        (0x10C4, 0x8A2A): ("ezsp",   "Nortek HUSBZB-1 (Zigbee/Z-Wave)"),
        # Nabu Casa SkyConnect
        (0x1A86, 0x55D4): ("ezsp",   "Nabu Casa SkyConnect (CH9102)"),
        # Dresden Elektronik — ConBee / RaspBee
        (0x1CF1, 0x0030): ("conbee", "Dresden Elektronik ConBee II"),
        (0x0403, 0x6015): ("conbee", "Dresden Elektronik ConBee (FTDI)"),
        (0x1CF1, 0x0031): ("conbee", "Dresden Elektronik ConBee III"),
        # TI CC2531/CC2652 — Z-Stack
        (0x0451, 0x16A8): ("zstack", "TI CC2531 USB"),
        (0x0451, 0x16B3): ("zstack", "TI CC2538"),
        (0x10C4, 0x8B34): ("zstack", "Electrolama zzh (CC2652)"),
        # FTDI generic — could be any adapter
        (0x0403, 0x6001): (None,     "FTDI FT232R (possible Zigbee)"),
        (0x0403, 0x6010): (None,     "FTDI FT2232 (possible Zigbee)"),
        # CH340/CH341 — used by many adapters
        (0x1A86, 0x7523): (None,     "CH340 (possible Zigbee adapter)"),
        (0x1A86, 0x5523): (None,     "CH341 (possible Zigbee adapter)"),
    }

    # VID:PID pairs that are definitely NOT Zigbee — skip entirely
    # (VID, None) = skip all PIDs for that vendor
    KNOWN_NON_ZIGBEE_USB = {
        (0x1D6B, 0x0002),  # Linux Foundation USB 2.0 root hub
        (0x1D6B, 0x0003),  # Linux Foundation USB 3.0 root hub
        (0x8087, None),     # Intel Bluetooth
        (0x0A5C, None),     # Broadcom Bluetooth
        (0x27C6, None),     # Goodix fingerprint
        (0x8086, 0x0B63),   # Intel USB bridge
        (0x046D, None),     # Logitech HID
        (0x04F2, None),     # Chicony webcams
        (0x0BDA, None),     # Realtek (wifi, card readers)
        (0x1BCF, None),     # Sunplus webcams
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results: list[AdapterInfo] = []

    # ── USB-first discovery ────────────────────────────────────────────

    def _is_known_non_zigbee(self, vid: int, pid: int) -> bool:
        """Check if VID:PID is a known non-Zigbee device."""
        if (vid, pid) in self.KNOWN_NON_ZIGBEE_USB:
            return True
        if (vid, None) in self.KNOWN_NON_ZIGBEE_USB:
            return True
        return False

    def _run_lsusb(self) -> list[dict]:
        """Run lsusb and parse output into structured list."""
        devices = []
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                logging.debug("lsusb failed: %s", result.stderr)
                return devices
            for line in result.stdout.strip().splitlines():
                m = re.match(
                    r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s+(.*)",
                    line,
                )
                if m:
                    devices.append({
                        "bus": m.group(1),
                        "device": m.group(2),
                        "vid": int(m.group(3), 16),
                        "pid": int(m.group(4), 16),
                        "description": m.group(5).strip(),
                    })
        except FileNotFoundError:
            logging.debug("lsusb not found — using pyserial fallback")
        except subprocess.TimeoutExpired:
            logging.debug("lsusb timed out")
        return devices

    def _map_usb_to_tty(self, bus: str, device: str, vid: int, pid: int) -> Optional[str]:
        """Map a USB bus/device to its /dev/tty* path using sysfs, pyserial, and by-id."""
        bus_num = int(bus)

        # Method 1: Walk sysfs for matching VID:PID and find tty child
        sys_pattern = f"/sys/bus/usb/devices/{bus_num}-*"
        for usb_path in glob.glob(sys_pattern):
            for root, dirs, files in os.walk(usb_path):
                if "tty" in dirs:
                    tty_dir = os.path.join(root, "tty")
                    try:
                        for tty_name in os.listdir(tty_dir):
                            tty_path = f"/dev/{tty_name}"
                            if os.path.exists(tty_path):
                                # Verify VID:PID match in sysfs ancestor
                                check = root
                                while check and check != "/sys":
                                    vid_file = os.path.join(check, "idVendor")
                                    pid_file = os.path.join(check, "idProduct")
                                    if os.path.exists(vid_file) and os.path.exists(pid_file):
                                        try:
                                            sys_vid = int(open(vid_file).read().strip(), 16)
                                            sys_pid = int(open(pid_file).read().strip(), 16)
                                            if sys_vid == vid and sys_pid == pid:
                                                return tty_path
                                        except (ValueError, IOError):
                                            pass
                                        break
                                    check = os.path.dirname(check)
                    except OSError:
                        pass

                # Also check for ttyUSB* / ttyACM* in filenames
                for fname in files:
                    if fname.startswith(("ttyUSB", "ttyACM")):
                        tty_path = f"/dev/{fname}"
                        if os.path.exists(tty_path):
                            return tty_path

        # Method 2: pyserial match by VID:PID
        for p in serial.tools.list_ports.comports():
            if p.vid == vid and p.pid == pid:
                return p.device

        # Method 3: /dev/serial/by-id symlinks
        by_id_dir = "/dev/serial/by-id"
        if os.path.isdir(by_id_dir):
            vid_hex = f"{vid:04x}"
            pid_hex = f"{pid:04x}"
            for link_name in os.listdir(by_id_dir):
                if vid_hex in link_name.lower() or pid_hex in link_name.lower():
                    real_path = os.path.realpath(os.path.join(by_id_dir, link_name))
                    if os.path.exists(real_path):
                        return real_path

        return None

    def discover_candidates(self) -> list[dict]:
        """
        USB-first discovery: use lsusb to enumerate USB bus, filter for
        serial/UART bridge devices, skip known non-Zigbee hardware,
        and map survivors to /dev/tty* paths.

        Returns list of {port, vid, pid, description, likely_family, priority}.
        Priority: 1 = known Zigbee VID:PID, 2 = generic UART bridge.
        """
        candidates = []
        seen_ports = set()

        col_w = 58  # inner column width for the box drawing

        print(f"\n  ┌─ USB Device Discovery {'─' * (col_w - 24)}┐")

        # ── Step 1: lsusb scan ──
        usb_devices = self._run_lsusb()
        if usb_devices:
            print(f"  │ {'lsusb: ' + str(len(usb_devices)) + ' USB device(s) on bus':<{col_w}}│")
            for dev in usb_devices:
                vid, pid = dev["vid"], dev["pid"]
                desc = dev["description"]

                if self._is_known_non_zigbee(vid, pid):
                    logging.debug("  Skip non-Zigbee: %04X:%04X %s", vid, pid, desc)
                    continue

                known = self.KNOWN_USB_ZIGBEE.get((vid, pid))
                if known:
                    likely_family, known_desc = known
                    priority = 1
                    label = f"✓ {vid:04X}:{pid:04X} {known_desc}"
                else:
                    desc_lower = desc.lower()
                    uart_kw = ["uart", "serial", "bridge", "cp210", "ch340",
                               "ch341", "ftdi", "ft232", "converter", "cdc acm"]
                    if any(kw in desc_lower for kw in uart_kw):
                        likely_family = None
                        priority = 2
                        label = f"? {vid:04X}:{pid:04X} {desc}"
                    else:
                        logging.debug("  Skip non-serial: %04X:%04X %s", vid, pid, desc)
                        continue

                tty = self._map_usb_to_tty(dev["bus"], dev["device"], vid, pid)
                if tty and tty not in seen_ports:
                    seen_ports.add(tty)
                    candidates.append({
                        "port": tty, "vid": vid, "pid": pid,
                        "description": desc,
                        "likely_family": known[0] if known else None,
                        "priority": priority,
                    })
                    print(f"  │ {label:<{col_w}}│")
                    print(f"  │   {'└─ mapped → ' + tty:<{col_w}}│")
                elif not tty:
                    print(f"  │ {label:<{col_w}}│")
                    print(f"  │   {'└─ ⚠ no /dev/tty mapping found':<{col_w}}│")
        else:
            print(f"  │ {'lsusb not available — pyserial fallback':<{col_w}}│")

        # ── Step 2: pyserial supplement (catches anything lsusb missed) ──
        for p in serial.tools.list_ports.comports():
            if p.device in seen_ports:
                continue
            vid = p.vid or 0
            pid = p.pid or 0

            if vid == 0 and pid == 0:
                # Non-USB port — only keep ttyAMA (Raspberry Pi GPIO → RaspBee)
                if "ttyAMA" in p.device:
                    candidates.append({
                        "port": p.device, "vid": 0, "pid": 0,
                        "description": "RPi GPIO UART (possible RaspBee)",
                        "likely_family": "conbee", "priority": 2,
                    })
                    seen_ports.add(p.device)
                    line = f"? {p.device} — GPIO UART (possible RaspBee)"
                    print(f"  │ {line:<{col_w}}│")
                continue

            if self._is_known_non_zigbee(vid, pid):
                continue

            known = self.KNOWN_USB_ZIGBEE.get((vid, pid))
            priority = 1 if known else 2
            candidates.append({
                "port": p.device, "vid": vid, "pid": pid,
                "description": p.product or p.description or "",
                "likely_family": known[0] if known else None,
                "priority": priority,
            })
            seen_ports.add(p.device)
            sym = "✓" if priority == 1 else "?"
            line = f"{sym} {vid:04X}:{pid:04X} → {p.device}"
            print(f"  │ {line:<{col_w}}│")

        candidates.sort(key=lambda c: c["priority"])

        if not candidates:
            print(f"  │ {'⚠ No USB serial / UART bridges found':<{col_w}}│")
        else:
            print(f"  │ {'':<{col_w}}│")
            summary = f"Candidate serial ports: {len(candidates)}"
            print(f"  │ {summary:<{col_w}}│")

        print(f"  └{'─' * col_w}┘")
        return candidates

    def _try_probe(self, port: str, baud: int, flow: FlowControl,
                   probe_cls, name: str) -> Optional[serial.Serial]:
        """Attempt a single protocol probe at given baud/flow. Returns open Serial on success."""
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
                logging.debug("  Probe %s %d/%s failed: %s", name, baud, flow.value, exc)
                if ser:
                    try:
                        ser.close()
                    except Exception:
                        pass
        return None

    # ── Flow control verification ──────────────────────────────────────

    def _test_flow_mode(self, port: str, baud: int, flow: FlowControl,
                        probe_cls, rounds: int = 3) -> dict:
        """Send the protocol's detect command `rounds` times under a specific
        flow control setting and measure success rate, response times, and
        byte counts.  Returns a stats dict."""
        successes = 0
        total_bytes = 0
        resp_times = []

        for _ in range(rounds):
            ser = None
            try:
                ser = open_serial(port, baud, flow)
                flush_port(ser)
                t0 = time.monotonic()
                resp = probe_cls.detect(ser)
                elapsed = time.monotonic() - t0
                resp_times.append(elapsed)

                if resp:
                    successes += 1
                    total_bytes += len(resp)

                ser.close()
            except (serial.SerialException, OSError, TimeoutError):
                resp_times.append(999.0)
                if ser:
                    try:
                        ser.close()
                    except Exception:
                        pass

        avg_time = sum(resp_times) / len(resp_times) if resp_times else 999.0
        return {
            "flow": flow,
            "successes": successes,
            "rounds": rounds,
            "avg_resp_time": avg_time,
            "total_bytes": total_bytes,
        }

    def _check_cts_pin(self, port: str, baud: int) -> Optional[bool]:
        """Read the physical CTS pin state.  Returns True if CTS is asserted,
        False if not, None if unreadable."""
        ser = None
        try:
            ser = open_serial(port, baud, FlowControl.NONE)
            # Assert RTS so the remote side (if it does hw flow) will assert CTS
            ser.rts = True
            time.sleep(0.1)
            cts = ser.cts
            ser.close()
            return cts
        except Exception:
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
            return None

    def _check_xonxoff(self, port: str, baud: int, probe_cls) -> bool:
        """Send an XOFF byte then a protocol command and see if the adapter
        stops responding (indicating it honours software flow control).
        Then send XON and check it resumes."""
        XOFF = b'\x13'
        XON = b'\x11'
        ser = None
        try:
            ser = open_serial(port, baud, FlowControl.NONE)
            flush_port(ser)

            # Send XOFF — if the adapter supports sw flow control it should
            # stop transmitting
            ser.write(XOFF)
            ser.flush()
            time.sleep(0.1)

            # Now send a detect command — if adapter honours XOFF it won't reply
            payload = probe_cls.get_test_payload()

            ser.write(payload)
            ser.flush()
            time.sleep(0.3)
            blocked_resp = ser.read(128)

            # Send XON to resume
            ser.write(XON)
            ser.flush()
            time.sleep(0.1)

            # Re-send command — should now get a response
            flush_port(ser)
            ser.write(payload)
            ser.flush()
            time.sleep(0.3)
            resumed_resp = ser.read(128)

            ser.close()

            # If XOFF blocked the response and XON unblocked it, adapter uses sw flow
            if len(blocked_resp) == 0 and len(resumed_resp) > 0:
                return True
            return False
        except Exception:
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
            return False

    def verify_flow_control(self, port: str, baud: int, probe_cls,
                            initial_flow: FlowControl) -> FlowControl:
        """Empirically determine the correct flow control for a detected adapter.

        Strategy:
        1. Check physical CTS pin state (is HW flow control even wired?)
        2. Test all three flow modes with the real protocol detect command
        3. Test XON/XOFF behaviour
        4. Score and pick the best mode

        Returns the verified FlowControl setting.
        """
        print("  ┌─ Flow Control Verification ──────────────────────────")

        # ── Step 1: Physical CTS pin check ──
        print("  │ Checking CTS pin state...", end="", flush=True)
        cts_state = self._check_cts_pin(port, baud)
        if cts_state is None:
            print(" unreadable")
            cts_info = "unknown"
        elif cts_state:
            print(" ASSERTED (high) — HW flow control possible")
            cts_info = "asserted"
        else:
            print(" NOT asserted (low) — HW flow control unlikely")
            cts_info = "not_asserted"

        # ── Step 2: Test each flow mode empirically ──
        modes_to_test = [FlowControl.NONE, FlowControl.RTSCTS, FlowControl.XONXOFF]
        test_results = {}

        for flow in modes_to_test:
            label = flow.value.upper().ljust(7)
            print(f"  │ Testing {label}...", end="", flush=True)
            stats = self._test_flow_mode(port, baud, flow, probe_cls, rounds=3)
            rate = stats["successes"] / stats["rounds"] * 100
            avg_ms = stats["avg_resp_time"] * 1000
            test_results[flow] = stats
            status = "✓" if stats["successes"] == stats["rounds"] else "✗" if stats["successes"] == 0 else "~"
            print(f" {status} {stats['successes']}/{stats['rounds']} success,"
                  f" avg {avg_ms:.0f}ms, {stats['total_bytes']} bytes")

        # ── Step 3: XON/XOFF behavioural test ──
        print("  │ Testing XON/XOFF behaviour...", end="", flush=True)
        xonxoff_active = self._check_xonxoff(port, baud, probe_cls)
        if xonxoff_active:
            print(" adapter responds to XON/XOFF")
        else:
            print(" adapter ignores XON/XOFF")

        # ── Step 4: Score and decide ──
        print("  │")

        # Scoring: higher is better
        scores = {}
        for flow, stats in test_results.items():
            score = 0
            # Base: success rate (most important)
            score += stats["successes"] * 100

            # Bonus: faster response = better
            if stats["avg_resp_time"] < 0.5:
                score += 20
            elif stats["avg_resp_time"] < 1.0:
                score += 10

            # Bonus: more bytes = healthier conversation
            score += min(stats["total_bytes"], 50)

            # Penalty: if CTS is not asserted, RTS/CTS is probably wrong
            if flow == FlowControl.RTSCTS and cts_info == "not_asserted":
                score -= 150

            # Penalty: if XON/XOFF test shows adapter ignores it, sw flow is wrong
            if flow == FlowControl.XONXOFF and not xonxoff_active:
                score -= 50

            # Bonus: if XON/XOFF test positive and mode is XONXOFF
            if flow == FlowControl.XONXOFF and xonxoff_active:
                score += 50

            # Bonus: if CTS asserted and mode is RTSCTS
            if flow == FlowControl.RTSCTS and cts_info == "asserted":
                score += 30

            scores[flow] = score

        # Pick winner
        best_flow = max(scores, key=scores.get)
        for flow in [FlowControl.NONE, FlowControl.RTSCTS, FlowControl.XONXOFF]:
            marker = " ← BEST" if flow == best_flow else ""
            label = flow.value.upper().ljust(7)
            s = test_results[flow]
            print(f"  │ {label}: score={scores[flow]:+4d}  "
                  f"({s['successes']}/{s['rounds']} ok, "
                  f"{s['avg_resp_time']*1000:.0f}ms avg){marker}")

        if best_flow != initial_flow:
            print(f"  │ ⚡ Corrected: {initial_flow.value} → {best_flow.value}")
        else:
            print(f"  │ ✓ Confirmed: {best_flow.value}")

        print("  └─────────────────────────────────────────────────────")
        return best_flow

    def probe_port(self, port: str, candidate: Optional[dict] = None) -> Optional[AdapterInfo]:
        """Probe a single serial port. If candidate dict is provided, uses USB
        hints to prioritize which protocols to try first."""
        print(f"\n{'─' * 60}")
        print(f"  Probing: {port}")
        if candidate:
            vid = candidate.get("vid", 0)
            pid = candidate.get("pid", 0)
            desc = candidate.get("description", "")
            if vid:
                print(f"  USB ID : {vid:04X}:{pid:04X} — {desc}")
        print(f"{'─' * 60}")

        # Show any additional USB info from pyserial
        _usb_info = None
        for p in serial.tools.list_ports.comports():
            if p.device == port:
                desc_parts = []
                if p.manufacturer:
                    desc_parts.append(f"Mfg: {p.manufacturer}")
                if p.product:
                    desc_parts.append(f"Product: {p.product}")
                if p.serial_number:
                    desc_parts.append(f"Serial: {p.serial_number}")
                if desc_parts:
                    print(f"  Detail : {' | '.join(desc_parts)}")
                _usb_info = p
                break

        # ── USB product string pre-detection for CPC/RCP devices ──
        if _usb_info and _usb_info.product:
            product_lower = _usb_info.product.lower()
            is_mg24_dongle = (
                    "mg24" in product_lower
                    or ("multipan" in product_lower.replace("-", "").replace(" ", ""))
                    or ("rcp" in product_lower and "sonoff" in (_usb_info.manufacturer or "").lower())
            )
            if is_mg24_dongle:
                # MG24 could be running EZSP (stock) or MultiPAN RCP.
                # Quick EZSP probe — if it responds, it's NCP firmware, not RCP.
                # EZSP probe is safe for CPC state (wrong protocol = no response).
                print("  ⚡ MG24 detected — quick EZSP check before assuming MultiPAN...")
                ezsp_ser = self._try_probe(port, 115200, FlowControl.NONE, EZSPProbe, "EZSP")
                if ezsp_ser:
                    ezsp_ser.close()
                    print("  → EZSP responded — running NCP firmware, not MultiPAN")
                    # Fall through to normal probe flow
                else:
                    print("  → No EZSP response — assuming MultiPAN RCP firmware")
                    vid_pid = f"{_usb_info.vid:04X}:{_usb_info.pid:04X}" if _usb_info.vid and _usb_info.pid else ""
                    mfg = (_usb_info.manufacturer or "").lower()
                    chip = "MG24" if "mg24" in product_lower else "MG21" if "mg21" in product_lower else ""
                    if "sonoff" in product_lower or "sonoff" in mfg:
                        board = f"SONOFF Zigbee Dongle {chip} (Multi-PAN)".strip()
                    elif "skyconnect" in product_lower or "nabu" in mfg:
                        board = "Nabu Casa SkyConnect (Multi-PAN)"
                    else:
                        board = f"{_usb_info.manufacturer or ''} {_usb_info.product} (Multi-PAN RCP)".strip()

                    info = AdapterInfo(
                        port=port,
                        baud_rate=115200,
                        flow_control=FlowControl.NONE,
                        adapter_family=AdapterFamily.CPC_MULTIPAN,
                        firmware_version="CPC (detected via USB descriptor)",
                        hardware_id=vid_pid,
                        board_name=board,
                    )
                    info.extra["Firmware Type"] = "Multi-PAN RCP (not NCP/EZSP)"
                    info.extra["Detection Method"] = "USB product string + EZSP negative probe"
                    self.results.append(info)
                    return info

        # ── Determine probe order based on USB hints ──
        likely = candidate.get("likely_family") if candidate else None

        all_protos = [
            ("ezsp",   EZSPProbe),
            ("cpc",    CPCMultiPANProbe),
            ("zstack", ZStackProbe),
            ("conbee", ConBeeProbe),
        ]

        # Re-order so the likely family is probed first
        if likely:
            all_protos.sort(key=lambda x: 0 if x[0] == likely else 1)
            print(f"  Hint   : likely {likely.upper()} — probing that first")

        # Build ordered probe list: preferred bauds for hinted proto first,
        # then remaining combos
        probes_to_try = []

        # Phase 1: preferred baud/flow for the hinted protocol (fast path)
        if likely and likely in self.PREFERRED:
            probe_cls = next(cls for name, cls in all_protos if name == likely)
            for baud, flow in self.PREFERRED[likely]:
                probes_to_try.append((baud, flow, probe_cls, likely.upper()))

        # Phase 2: preferred baud/flow for remaining protocols
        for proto_name, probe_cls in all_protos:
            if proto_name == likely:
                continue
            for baud, flow in self.PREFERRED[proto_name]:
                probes_to_try.append((baud, flow, probe_cls, proto_name.upper()))

        # Phase 3: all other baud rates (only if preferred didn't match)
        for baud in COMMON_BAUD_RATES:
            for flow in [FlowControl.NONE, FlowControl.RTSCTS]:
                for proto_name, probe_cls in all_protos:
                    entry = (baud, flow, probe_cls, proto_name.upper())
                    if entry not in probes_to_try:
                        probes_to_try.append(entry)

        # De-duplicate
        seen = set()
        unique_probes = []
        for entry in probes_to_try:
            key = (entry[0], entry[1], entry[3])
            if key not in seen:
                seen.add(key)
                unique_probes.append(entry)

        total = len(unique_probes)
        for i, (baud, flow, probe_cls, name) in enumerate(unique_probes):
            progress = f"[{i + 1}/{total}]"
            print(f"  {progress} Trying {name} @ {baud} baud, flow={flow.value}...", end="", flush=True)

            ser = self._try_probe(port, baud, flow, probe_cls, name)
            if ser:
                print(" DETECTED!")
                ser.close()  # Close the detection connection

                # ── Verify flow control empirically ──
                verified_flow = self.verify_flow_control(port, baud, probe_cls, flow)

                # ── Re-open with verified settings and interrogate ──
                info = AdapterInfo(port=port, baud_rate=baud, flow_control=verified_flow)
                ser2 = None
                try:
                    ser2 = open_serial(port, baud, verified_flow)
                    flush_port(ser2)
                    probe_cls.interrogate(ser2, info)
                except Exception as exc:
                    logging.warning("Interrogation error: %s", exc)
                finally:
                    if ser2:
                        try:
                            ser2.close()
                        except Exception:
                            pass

                self.results.append(info)
                return info
            else:
                print(" no", flush=True)

        # Nothing matched — it's not a Zigbee device (or uses unknown protocol)
        print("  → No Zigbee adapter detected on this port.")
        info = AdapterInfo(port=port, adapter_family=AdapterFamily.NOT_ZIGBEE)
        # Record USB info anyway
        for p in serial.tools.list_ports.comports():
            if p.device == port:
                info.hardware_id = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else ""
                info.board_name = f"{p.manufacturer or ''} {p.product or p.description or ''}".strip()
                break
        self.results.append(info)
        return info

    def scan_all(self, ports: Optional[list[str]] = None) -> list[AdapterInfo]:
        """Scan all specified (or auto-discovered) serial ports."""
        if ports:
            # Manual port list — wrap in candidate dicts with no hints
            candidates = [{"port": p, "vid": 0, "pid": 0, "description": "",
                           "likely_family": None, "priority": 3} for p in ports]
            print(f"\n  Manual mode: probing {len(ports)} specified port(s)")
        else:
            # Auto-discovery via USB
            candidates = self.discover_candidates()

        if not candidates:
            print("\n  No candidate serial ports found.")
            print("  Ensure your Zigbee adapter is plugged in and visible in `lsusb`.")
            return []

        for cand in candidates:
            try:
                self.probe_port(cand["port"], candidate=cand)
            except Exception as exc:
                logging.error("Error probing %s: %s", cand["port"], exc)

        return self.results

    def print_report(self):
        """Print a formatted summary of all results."""
        zigbee_results = [r for r in self.results if r.adapter_family != AdapterFamily.NOT_ZIGBEE]
        other_results = [r for r in self.results if r.adapter_family == AdapterFamily.NOT_ZIGBEE]

        print(f"\n{'═' * 60}")
        print("  SCAN RESULTS")
        print(f"{'═' * 60}")

        if zigbee_results:
            print(f"\n  ✅ Zigbee adapters found: {len(zigbee_results)}")
            for i, r in enumerate(zigbee_results, 1):
                print(f"\n  ── Adapter #{i} {'─' * 44}")
                print(r.summary())
        else:
            print("\n  ⚠  No Zigbee adapters detected.")

        if other_results:
            print(f"\n  ── Non-Zigbee serial ports ({len(other_results)}) {'─' * 25}")
            for r in other_results:
                board = r.board_name or "unknown device"
                hwid = f" [{r.hardware_id}]" if r.hardware_id else ""
                print(f"    • {r.port} — {board}{hwid}")

        print(f"\n{'═' * 60}")

    def export_json(self) -> str:
        """Export results as JSON."""
        data = []
        for r in self.results:
            d = {
                "port": r.port,
                "adapter_family": r.adapter_family.value,
                "baud_rate": r.baud_rate,
                "flow_control": r.flow_control.value,
                "firmware_version": r.firmware_version,
                "stack_version": r.stack_version,
                "hardware_id": r.hardware_id,
                "eui64": r.eui64,
                "board_name": r.board_name,
                "extra": r.extra,
            }
            data.append(d)
        return json.dumps(data, indent=2)


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

Examples:
  %(prog)s                             Auto-scan all serial ports
  %(prog)s --port /dev/ttyUSB0         Probe a specific port
  %(prog)s --port COM3 --verbose       Verbose output on Windows
  %(prog)s --json --output result.json Export results as JSON
        """,
    )
    parser.add_argument("-p", "--port", type=str, default=None,
                        help="Specific serial port to probe (default: auto-detect all)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose/debug logging")
    parser.add_argument("-j", "--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Write JSON results to file")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT,
                        help=f"Probe timeout in seconds (default: {PROBE_TIMEOUT})")
    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)-8s %(message)s")

    print(BANNER)

    interrogator = ZigbeeInterrogator(verbose=args.verbose)

    # Run scan
    ports = [args.port] if args.port else None
    interrogator.scan_all(ports)

    # Output
    if args.json or args.output:
        json_str = interrogator.export_json()
        if args.output:
            with open(args.output, "w") as f:
                f.write(json_str)
            print(f"\n  JSON results written to: {args.output}")
        if args.json:
            print(json_str)
    else:
        interrogator.print_report()


if __name__ == "__main__":
    main()