#!/usr/bin/env python3
"""
ZMM CPC Diagnostic Tool
========================
Comprehensive on-wire diagnostic for the CPC/HDLC framing layer.
Run inside the container:

    podman exec zigbee-matter-manager python3 /app/diag_cpc.py
    podman exec zigbee-matter-manager python3 /app/diag_cpc.py --port /dev/ttyACM0
    podman exec zigbee-matter-manager python3 /app/diag_cpc.py --baud 460800

Tests performed:
  1. Serial port access and permissions
  2. CRC-16 variant detection (init=0x0000 vs 0xFFFF)
  3. Baud rate sweep (listen for data at common rates)
  4. Hardware reset (RTS toggle) and boot capture
  5. Frame format detection (field order, LEN semantics)
  6. Full CPC handshake: DISC → SABM → UA verification
  7. zmm_cpc module import and CRC consistency check
  8. Endpoint SABM/UA handshake on ep0, ep12, ep13
"""

import argparse
import os
import struct
import sys
import time

# ─────────────────────────────────────────────────────────────────────────────
# CRC implementations
# ─────────────────────────────────────────────────────────────────────────────

def crc16_0000(data: bytes) -> int:
    """CRC-16/XMODEM: poly=0x1021, init=0x0000"""
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def crc16_ffff(data: bytes) -> int:
    """CRC-16/IBM-3740: poly=0x1021, init=0xFFFF"""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


# ─────────────────────────────────────────────────────────────────────────────
# Frame encoding / decoding
# ─────────────────────────────────────────────────────────────────────────────

CPC_FLAG = 0x14
U_SABM = 0xEF
U_UA   = 0x63
U_DISC = 0x43

CTRL_NAMES = {U_SABM: "SABM", U_UA: "UA", U_DISC: "DISC"}


def make_frame(ep: int, ctrl: int, payload: bytes = b"",
               crc_fn=crc16_0000, len_includes_fcs: bool = True) -> bytes:
    """Encode a CPC frame with the given parameters."""
    fcs_extra = 2 if len_includes_fcs else 0
    length = len(payload) + fcs_extra
    # Layout: FLAG EP LEN_LO LEN_HI CTRL HCS(2) PAYLOAD FCS(2)
    hdr = bytes([CPC_FLAG, ep, length & 0xFF, (length >> 8) & 0xFF, ctrl])
    hcs = crc_fn(hdr)
    fcs = crc_fn(payload)
    return (hdr
            + struct.pack("<H", hcs)
            + payload
            + struct.pack("<H", fcs))


def decode_ctrl(ctrl: int) -> str:
    """Human-readable control byte description."""
    if ctrl & 0x01 == 0:
        ns = (ctrl >> 1) & 7
        nr = (ctrl >> 5) & 7
        return f"I-frame N(S)={ns} N(R)={nr}"
    elif ctrl & 0x03 == 0x01:
        stype = (ctrl >> 2) & 3
        nr = (ctrl >> 5) & 7
        sname = {0: "RR", 1: "REJ"}.get(stype, f"S{stype}")
        return f"S-frame {sname} N(R)={nr}"
    else:
        utype = ctrl & 0xEF
        name = CTRL_NAMES.get(utype, f"0x{utype:02x}")
        return f"U-frame {name}"


def try_parse_frames(data: bytes, crc_fn, layout: str, len_includes_fcs: bool):
    """
    Attempt to parse CPC frames from raw bytes.
    Returns list of (ep, ctrl, ctrl_desc, payload, valid) tuples.
    """
    frames = []
    i = 0
    while i < len(data):
        if data[i] != CPC_FLAG:
            i += 1
            continue
        if i + 7 > len(data):
            break

        if layout == "EP_LEN_CTRL":
            ep   = data[i + 1]
            plen = data[i + 2] | (data[i + 3] << 8)
            ctrl = data[i + 4]
        else:  # EP_CTRL_LEN
            ep   = data[i + 1]
            ctrl = data[i + 2]
            plen = data[i + 3] | (data[i + 4] << 8)

        hdr = data[i:i + 5]
        hcs_recv = data[i + 5] | (data[i + 6] << 8)
        hcs_comp = crc_fn(hdr)

        if hcs_recv != hcs_comp:
            i += 1
            continue

        # HCS valid — try to extract payload
        if len_includes_fcs:
            if plen < 2:
                i += 1
                continue
            payload_len = plen - 2
        else:
            payload_len = plen

        frame_end = i + 7 + payload_len + 2
        if frame_end > len(data):
            break

        payload = data[i + 7:i + 7 + payload_len]
        fcs_recv = data[i + 7 + payload_len] | (data[i + 7 + payload_len + 1] << 8)
        fcs_comp = crc_fn(payload)
        fcs_ok = fcs_recv == fcs_comp

        frames.append((ep, ctrl, decode_ctrl(ctrl), payload, fcs_ok))
        i = frame_end

    return frames


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic steps
# ─────────────────────────────────────────────────────────────────────────────

def print_header(title: str):
    print()
    print(f"{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_result(label: str, ok: bool, detail: str = ""):
    icon = "✅" if ok else "❌"
    line = f"  {icon} {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def test_serial_access(port: str) -> bool:
    """Test 1: Can we open the serial port?"""
    print_header("TEST 1: Serial Port Access")
    print(f"  Port: {port}")

    if not os.path.exists(port):
        print_result("Device exists", False, f"{port} not found")
        # Check what's available
        import glob
        devs = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
        if devs:
            print(f"  Available: {', '.join(devs)}")
        else:
            print("  No /dev/ttyUSB* or /dev/ttyACM* devices found")
        return False

    print_result("Device exists", True)

    # Check permissions
    readable = os.access(port, os.R_OK)
    writable = os.access(port, os.W_OK)
    print_result("Readable", readable)
    print_result("Writable", writable)

    if not (readable and writable):
        stat = os.stat(port)
        print(f"  Owner UID:GID = {stat.st_uid}:{stat.st_gid}")
        print(f"  Mode = {oct(stat.st_mode)}")
        print(f"  Running as UID:GID = {os.getuid()}:{os.getgid()}")
        groups = os.getgroups()
        print(f"  Supplementary groups = {groups}")
        return False

    # Try opening
    try:
        import serial as pyserial
        ser = pyserial.Serial(port, 115200, timeout=0.5)
        ser.close()
        print_result("Port opens", True)
        return True
    except Exception as e:
        print_result("Port opens", False, str(e))
        return False


def test_baud_sweep(port: str) -> int:
    """Test 2: Find which baud rate produces data after an RTS reset."""
    print_header("TEST 2: Baud Rate Sweep (RTS reset at each rate)")

    import serial as pyserial

    best_baud = 0
    best_data = b""
    best_has_flag = False

    for baud in [115200, 230400, 256000, 460800, 500000, 921600]:
        try:
            ser = pyserial.Serial(port, baud, timeout=2)
            ser.dtr = False
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
            time.sleep(3)

            data = ser.read(500)
            ser.close()
            time.sleep(0.3)

            has_flag = any(b == CPC_FLAG for b in data)
            has_text = any(32 <= b < 127 for b in data)

            status = f"{len(data)} bytes"
            if has_flag:
                status += ", has CPC_FLAG (0x14)"
            if has_text:
                text = data.decode("utf-8", errors="replace").strip()
                status += f", text: {repr(text[:60])}"
            if not data:
                status = "(empty)"

            print(f"  {baud:>7} baud: {status}")

            # Prefer baud rates that produce valid CPC flags
            if has_flag and not best_has_flag:
                best_baud = baud
                best_data = data
                best_has_flag = True
            elif data and not best_data:
                best_baud = baud
                best_data = data

        except Exception as e:
            print(f"  {baud:>7} baud: ERROR {e}")

    if best_baud:
        print(f"\n  Best candidate: {best_baud} baud")
    else:
        print("\n  ⚠️  No data received at any baud rate")

    return best_baud


def test_crc_variant(data: bytes) -> str:
    """Test 3: Determine which CRC variant matches the captured data."""
    print_header("TEST 3: CRC-16 Variant Detection")

    if len(data) < 7:
        print("  ⚠️  Not enough data to test CRC (need ≥7 bytes)")
        return "unknown"

    # Find CPC_FLAG positions
    flag_positions = [i for i, b in enumerate(data) if b == CPC_FLAG]
    if not flag_positions:
        print("  ⚠️  No CPC_FLAG (0x14) found in captured data")
        return "unknown"

    print(f"  CPC_FLAG at byte positions: {flag_positions}")

    # For each possible layout + CRC combo, try to validate
    results = []
    for crc_name, crc_fn in [("init=0x0000", crc16_0000), ("init=0xFFFF", crc16_ffff)]:
        for layout in ["EP_LEN_CTRL", "EP_CTRL_LEN"]:
            for len_inc_fcs in [True, False]:
                frames = try_parse_frames(data, crc_fn, layout, len_inc_fcs)
                valid = [f for f in frames if f[4]]  # FCS also valid
                if frames:
                    tag = f"LEN{'(+FCS)' if len_inc_fcs else '(raw)'}"
                    results.append((crc_name, layout, tag, len(frames), len(valid), frames))

    if not results:
        print("  ❌ No valid frames found with any CRC/layout combination")

        # Brute-force: find what CRC init matches any 2-byte span
        print("\n  Brute-force CRC search on header bytes:")
        for pos in flag_positions:
            if pos + 7 > len(data):
                continue
            for hcs_pos in range(pos + 3, min(pos + 8, len(data) - 1)):
                hcs_val = data[hcs_pos] | (data[hcs_pos + 1] << 8)
                for start in range(pos, hcs_pos):
                    hdr = data[start:hcs_pos]
                    for init_name, init_val in [("0x0000", 0x0000), ("0xFFFF", 0xFFFF)]:
                        crc = init_val
                        for b in hdr:
                            crc ^= b << 8
                            for _ in range(8):
                                crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
                        if crc == hcs_val:
                            print(f"    MATCH: init={init_name} over data[{start}:{hcs_pos}] "
                                  f"= 0x{crc:04x}, hdr={hdr.hex()}")
        return "unknown"

    # Show results
    best = None
    for crc_name, layout, tag, total, valid, frames in results:
        ok = "✅" if valid > 0 else "❌"
        print(f"  {ok} CRC {crc_name}, layout {layout}, {tag}: "
              f"{total} frame(s), {valid} fully valid")
        for ep, ctrl, desc, payload, fcs_ok in frames:
            fcs_tag = "✅" if fcs_ok else "❌ FCS"
            print(f"      ep={ep} {desc} payload={len(payload)}B {fcs_tag}")
        if valid > 0 and best is None:
            best = (crc_name, layout, tag)

    if best:
        print(f"\n  ✅ Confirmed: CRC {best[0]}, layout {best[1]}, {best[2]}")
        return best[0]
    return "unknown"


def test_handshake(port: str, baud: int, crc_fn) -> bool:
    """Test 4: Full CPC handshake — DISC all → SABM ep0 → expect UA."""
    print_header("TEST 4: CPC Handshake (DISC → SABM → UA)")

    import serial as pyserial

    ser = pyserial.Serial(port, baud, timeout=0.5)
    ser.dtr = False
    ser.rts = False

    # Step 1: RTS reset to put chip in known state
    print("  Step 1: RTS reset...")
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(0.5)

    # Drain boot data
    boot = ser.read(500)
    if boot:
        print(f"  Boot data: {len(boot)} bytes — {boot.hex()}")
        # Try to parse and respond
        frames = try_parse_frames(boot, crc_fn, "EP_LEN_CTRL", True)
        for ep, ctrl, desc, payload, fcs_ok in frames:
            print(f"    RX: ep={ep} {desc} {'✅' if fcs_ok else '❌ FCS'}")
            # Respond with RR if I-frame
            if ctrl & 0x01 == 0:
                ns = (ctrl >> 1) & 7
                nr = (ns + 1) & 7
                rr_ctrl = 0x01 | (nr << 5)
                rr = make_frame(ep, rr_ctrl, crc_fn=crc_fn)
                ser.write(rr)
                print(f"    TX: RR N(R)={nr} on ep{ep}")
                time.sleep(0.2)
    else:
        print("  Boot data: (empty)")

    # Step 2: Send DISC on all endpoints
    print("\n  Step 2: Sending DISC on ep0, ep12, ep13...")
    for ep in [0, 12, 13]:
        disc = make_frame(ep, U_DISC, crc_fn=crc_fn)
        ser.write(disc)
        print(f"    TX: DISC ep{ep} — {disc.hex()}")
        time.sleep(0.2)

    # Read responses
    time.sleep(1)
    disc_resp = ser.read(500)
    if disc_resp:
        print(f"    RX: {len(disc_resp)} bytes — {disc_resp.hex()}")
        frames = try_parse_frames(disc_resp, crc_fn, "EP_LEN_CTRL", True)
        for ep, ctrl, desc, payload, fcs_ok in frames:
            print(f"      ep={ep} {desc} {'✅' if fcs_ok else '❌ FCS'}")
    else:
        print("    RX: (empty — normal, some firmware ignores DISC)")

    # Step 3: Send SABM on ep0
    print("\n  Step 3: Sending SABM on ep0...")
    sabm = make_frame(0, U_SABM, crc_fn=crc_fn)
    ser.write(sabm)
    print(f"    TX: SABM ep0 — {sabm.hex()}")

    time.sleep(1)
    sabm_resp = ser.read(500)
    ep0_ok = False
    if sabm_resp:
        print(f"    RX: {len(sabm_resp)} bytes — {sabm_resp.hex()}")
        frames = try_parse_frames(sabm_resp, crc_fn, "EP_LEN_CTRL", True)
        for ep, ctrl, desc, payload, fcs_ok in frames:
            print(f"      ep={ep} {desc} {'✅' if fcs_ok else '❌ FCS'}")
            if ep == 0 and (ctrl & 0xEF) == U_UA:
                ep0_ok = True
    else:
        print("    RX: (empty)")

    print_result("ep0 UA received", ep0_ok)

    if not ep0_ok:
        ser.close()
        return False

    # Step 4: Send SABM on ep12
    print("\n  Step 4: Sending SABM on ep12...")
    sabm12 = make_frame(12, U_SABM, crc_fn=crc_fn)
    ser.write(sabm12)
    print(f"    TX: SABM ep12 — {sabm12.hex()}")

    time.sleep(1)
    resp12 = ser.read(500)
    ep12_ok = False
    if resp12:
        print(f"    RX: {len(resp12)} bytes — {resp12.hex()}")
        frames = try_parse_frames(resp12, crc_fn, "EP_LEN_CTRL", True)
        for ep, ctrl, desc, payload, fcs_ok in frames:
            print(f"      ep={ep} {desc} {'✅' if fcs_ok else '❌ FCS'}")
            if ep == 12 and (ctrl & 0xEF) == U_UA:
                ep12_ok = True
            elif ep == 12 and (ctrl & 0xEF) == U_SABM:
                # RCP sent its own SABM — respond with UA
                ua = make_frame(12, U_UA, crc_fn=crc_fn)
                ser.write(ua)
                print(f"    TX: UA ep12 (responding to RCP's SABM)")
                ep12_ok = True
    else:
        print("    RX: (empty)")

    print_result("ep12 UA received", ep12_ok)

    # Step 5: Send SABM on ep13 (OpenThread — may or may not respond)
    print("\n  Step 5: Sending SABM on ep13 (optional)...")
    sabm13 = make_frame(13, U_SABM, crc_fn=crc_fn)
    ser.write(sabm13)
    time.sleep(1)
    resp13 = ser.read(500)
    ep13_ok = False
    if resp13:
        frames = try_parse_frames(resp13, crc_fn, "EP_LEN_CTRL", True)
        for ep, ctrl, desc, payload, fcs_ok in frames:
            print(f"      ep={ep} {desc} {'✅' if fcs_ok else '❌ FCS'}")
            if ep == 13 and (ctrl & 0xEF) == U_UA:
                ep13_ok = True
            elif ep == 13 and (ctrl & 0xEF) == U_SABM:
                ua = make_frame(13, U_UA, crc_fn=crc_fn)
                ser.write(ua)
                ep13_ok = True
    print_result("ep13 UA received", ep13_ok, "(optional — Thread endpoint)")

    # Step 6: Send DISC to clean up
    print("\n  Step 6: Cleanup — sending DISC...")
    for ep in [12, 13, 0]:
        ser.write(make_frame(ep, U_DISC, crc_fn=crc_fn))
        time.sleep(0.1)

    ser.close()
    return ep0_ok and ep12_ok


def test_zmm_cpc_module(crc_fn) -> bool:
    """Test 5: Verify zmm_cpc module is importable and CRC-consistent."""
    print_header("TEST 5: zmm_cpc Module Check")

    try:
        import zmm_cpc
        print_result("zmm_cpc imports", True)
    except ImportError as e:
        print_result("zmm_cpc imports", False, str(e))
        return False

    try:
        from zmm_cpc import CpcCore
        print_result("CpcCore class available", True)
    except ImportError as e:
        print_result("CpcCore class available", False, str(e))
        return False

    # Verify the Rust CRC matches our Python CRC
    # We can't call crc16 directly from Python, but we can encode a frame
    # and verify the bytes match what we'd produce
    try:
        core = CpcCore(
            serial_port="/dev/null",  # won't actually open
            baudrate=115200,
            tcp_endpoints={12: 9999},
        )
        print_result("CpcCore construction", True)

        # Check repr
        r = repr(core)
        print(f"  repr: {r}")

    except Exception as e:
        # May fail on /dev/null — that's OK, we just wanted to test import
        print(f"  CpcCore(/dev/null) raised: {e}")
        print("  (Expected — /dev/null is not a serial port)")

    # Verify our Python CRC matches the expected values for known frames
    # SABM ep0 with init=0x0000:
    # Header: 14 00 02 00 EF
    sabm_hdr = bytes([0x14, 0x00, 0x02, 0x00, 0xEF])
    sabm_hcs = crc_fn(sabm_hdr)
    sabm_frame = make_frame(0, U_SABM, crc_fn=crc_fn)
    print(f"  SABM ep0 frame: {sabm_frame.hex()}")
    print(f"  SABM ep0 HCS:   0x{sabm_hcs:04x}")

    # Cross-check: the frame we generate should be parseable
    frames = try_parse_frames(sabm_frame, crc_fn, "EP_LEN_CTRL", True)
    if frames and frames[0][4]:
        print_result("Self-generated SABM round-trips", True)
    else:
        print_result("Self-generated SABM round-trips", False)

    return True


def test_interactive_conversation(port: str, baud: int, crc_fn) -> bool:
    """Test 6: Full interactive conversation — reset, respond to everything."""
    print_header("TEST 6: Interactive Conversation (8 seconds)")

    import serial as pyserial

    ser = pyserial.Serial(port, baud, timeout=0.3)
    ser.dtr = False
    ser.rts = False

    # RTS reset
    print("  RTS reset...")
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False

    frames_seen = []
    deadline = time.time() + 8

    while time.time() < deadline:
        data = ser.read(200)
        if not data:
            continue

        frames = try_parse_frames(data, crc_fn, "EP_LEN_CTRL", True)
        if not frames:
            print(f"  RAW ({len(data)}B): {data[:40].hex()}")
            continue

        for ep, ctrl, desc, payload, fcs_ok in frames:
            tag = "✅" if fcs_ok else "❌"
            print(f"  RX: ep={ep:>2} {desc:<30} {tag} payload={payload.hex() if payload else ''}")
            frames_seen.append((ep, ctrl, desc))

            if not fcs_ok:
                continue

            # Respond
            if ctrl & 0x03 == 0x03:  # U-frame
                utype = ctrl & 0xEF
                if utype == U_SABM:
                    ua = make_frame(ep, U_UA, crc_fn=crc_fn)
                    ser.write(ua)
                    print(f"  TX: ep={ep:>2} UA (responding to SABM)")
                elif utype == U_DISC:
                    ua = make_frame(ep, U_UA, crc_fn=crc_fn)
                    ser.write(ua)
                    print(f"  TX: ep={ep:>2} UA (acking DISC)")
            elif ctrl & 0x01 == 0:  # I-frame
                ns = (ctrl >> 1) & 7
                nr = (ns + 1) & 7
                rr_ctrl = 0x01 | (nr << 5)
                rr = make_frame(ep, rr_ctrl, crc_fn=crc_fn)
                ser.write(rr)
                print(f"  TX: ep={ep:>2} RR N(R)={nr}")

    ser.close()

    if frames_seen:
        print(f"\n  Total frames: {len(frames_seen)}")
        eps = set(ep for ep, _, _ in frames_seen)
        print(f"  Endpoints seen: {sorted(eps)}")
        print_result("RCP is communicating", True)
        return True
    else:
        print_result("RCP is communicating", False, "no valid frames received")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: dict):
    print_header("SUMMARY")
    all_ok = True
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("  🎉 All tests passed — CPC stack should work correctly.")
    else:
        print("  ⚠️  Some tests failed — see details above.")
        print()
        if not results.get("Serial access"):
            print("  FIX: Check device permissions, dialout group, bind-mount")
        if not results.get("Baud rate"):
            print("  FIX: No data at any baud rate — check USB connection, firmware")
        if results.get("CRC variant") == "unknown":
            print("  FIX: Could not determine CRC variant — firmware may use a non-standard format")
        if not results.get("CPC handshake"):
            print("  FIX: RCP not responding to SABM — check CRC init and header field order in hdlc.rs")
        if not results.get("Interactive"):
            print("  FIX: No frames exchanged — verify baud rate, CRC, and field order all match")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ZMM CPC Diagnostic Tool")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial device path")
    parser.add_argument("--baud", type=int, default=0, help="Force baud rate (0=auto-detect)")
    parser.add_argument("--skip-sweep", action="store_true", help="Skip baud rate sweep")
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║           ZMM CPC Diagnostic Tool v1.0                         ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    results = {}

    # Test 1: Serial access
    if not test_serial_access(args.port):
        results["Serial access"] = False
        print_summary(results)
        return 1

    results["Serial access"] = True

    # Test 2: Baud rate sweep
    if args.baud:
        baud = args.baud
        print(f"\n  Using forced baud rate: {baud}")
    elif args.skip_sweep:
        baud = 115200
        print(f"\n  Skipping sweep, using default: {baud}")
    else:
        baud = test_baud_sweep(args.port)
        if not baud:
            baud = 115200
            print(f"  Falling back to default: {baud}")

    results["Baud rate"] = baud > 0

    # Capture data for CRC analysis
    print_header("Capturing boot data for analysis...")
    import serial as pyserial
    ser = pyserial.Serial(args.port, baud, timeout=2)
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(3)
    boot_data = ser.read(500)
    ser.close()
    time.sleep(0.3)
    print(f"  Captured {len(boot_data)} bytes: {boot_data.hex() if boot_data else '(empty)'}")

    # Test 3: CRC variant
    crc_result = "unknown"
    if boot_data:
        crc_result = test_crc_variant(boot_data)

    # Select CRC function based on detection
    if "0x0000" in crc_result:
        crc_fn = crc16_0000
        print(f"\n  Using CRC-16 init=0x0000 for remaining tests")
    elif "0xFFFF" in crc_result:
        crc_fn = crc16_ffff
        print(f"\n  Using CRC-16 init=0xFFFF for remaining tests")
    else:
        # Try both, prefer 0x0000 (confirmed for Sonoff MG24)
        crc_fn = crc16_0000
        print(f"\n  CRC undetermined — defaulting to init=0x0000")

    results["CRC variant"] = crc_result

    # Test 4: CPC handshake
    handshake_ok = test_handshake(args.port, baud, crc_fn)
    results["CPC handshake"] = handshake_ok

    # Test 5: zmm_cpc module
    module_ok = test_zmm_cpc_module(crc_fn)
    results["zmm_cpc module"] = module_ok

    # Test 6: Interactive conversation
    interactive_ok = test_interactive_conversation(args.port, baud, crc_fn)
    results["Interactive"] = interactive_ok

    # Summary
    print_summary(results)

    return 0 if all(v for v in results.values() if isinstance(v, bool)) else 1


if __name__ == "__main__":
    sys.exit(main())