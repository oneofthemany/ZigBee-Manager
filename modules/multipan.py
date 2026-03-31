"""
MultiPAN RCP Manager
=====================
Manages MultiPAN RCP radio via zmm_cpc (Rust CPC/HDLC core) for concurrent
Zigbee + Thread on a single radio.

Phase 2 stack:
  MG24 serial → zmm_cpc (CpcCore) → TCP :9999 (ep12) → bellows/zigpy
                                   → TCP :9998 (ep13) → OT (Phase 3)

CpcCore owns the serial port, implements CPC/HDLC framing, endpoint
multiplexing, and exposes each CPC endpoint as a TCP listener.  bellows
connects to socket://127.0.0.1:9999 as before — the downstream Zigbee
startup path is unchanged.

otbr-agent (Thread) requires Phase 3 — it expects cpcd's Unix
SOCK_SEQPACKET sockets which zmm_cpc does not yet provide.

Startup sequence:
  1. RTS reset — toggle RTS to reboot the MG24 chip
  2. _drain_and_reset_link() — read any stale I-frames the RCP sends
     from a previous session, acknowledge them with correct N(R) so
     the RCP stops retransmitting, then send DISC on all endpoints to
     tear down the old CPC link state, then send SABM on ep0 and wait
     for UA to confirm the link is clean.
  3. Close pyserial, brief settle
  4. CpcCore.start() — opens serial port, binds TCP listeners, spawns
     the Tokio router.  The RCP is now in a clean state and will
     respond to SABMs from the router (reactive or proactive).

Integration point: core/service.py ZigbeeService.start()
  - Dongle Jedi detects CPC_MULTIPAN firmware
  - _probe_with_jedi() returns probe result with adapter_family
  - start() launches MultiPanManager BEFORE building bellows config
  - self.port is overridden to the EZSP socket
  - Rest of startup proceeds unchanged
"""
import asyncio
import logging
import os
import signal
import shutil
import struct
from typing import Optional, Dict, Callable

from zmm_cpc import CpcCore

logger = logging.getLogger("multipan")


# =========================================================================
# MANAGED DAEMON — generic subprocess wrapper (kept for otbr-agent Phase 3)
# =========================================================================

import re


class ManagedDaemon:
    def __init__(
            self,
            name: str,
            command: list,
            ready_marker: str | None = None,
            ready_timeout: float = 30.0,
            max_restarts: int = 5,
            restart_base_delay: float = 5.0,
            env: dict | None = None,
            *,
            ready_markers: list[str] | None = None,
            fatal_markers: list[str] | None = None,
            restart_on_fatal: bool = False,
            require_ready: bool = False,
    ):
        self.name = name
        self.command = command
        self.ready_marker = ready_marker
        self.ready_timeout = ready_timeout
        self.max_restarts = max_restarts
        self.restart_base_delay = restart_base_delay
        self.env = env

        self._ready_markers = set(ready_markers or [])
        if ready_marker:
            self._ready_markers.add(ready_marker)

        self._fatal_regexes = [re.compile(p, re.IGNORECASE) for p in (fatal_markers or [])]
        self._fatal_seen = False
        self.restart_on_fatal = restart_on_fatal
        self.require_ready = require_ready

        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._running = False
        self._shutdown = False
        self._restart_count = 0
        self._ready_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> Optional[int]:
        if self._process and self._process.returncode is None:
            return self._process.pid
        return None

    async def start(self) -> bool:
        if self.is_running:
            logger.warning(f"[{self.name}] Already running (PID {self.pid})")
            return True

        self._shutdown = False
        self._restart_count = 0
        self._ready_event.clear()
        return await self._spawn()

    async def _spawn(self) -> bool:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info(f"[{self.name}] Starting: {' '.join(self.command)}")

        try:
            proc_env = os.environ.copy()
            if self.env:
                proc_env.update(self.env)

            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=os.setpgrp,
                env=proc_env,
            )
            self._running = True
            logger.info(f"[{self.name}] Started (PID {self._process.pid})")

            self._monitor_task = asyncio.create_task(self._monitor())

            if self.ready_marker:
                try:
                    await asyncio.wait_for(
                        self._ready_event.wait(),
                        timeout=self.ready_timeout
                    )
                    logger.info(f"[{self.name}] Ready")
                except asyncio.TimeoutError:
                    if self.require_ready:
                        logger.error(
                            f"[{self.name}] Ready marker '{self.ready_marker}' not seen "
                            f"within {self.ready_timeout}s — aborting"
                        )
                        await self.stop()
                        return False
                    logger.warning(
                        f"[{self.name}] Ready marker '{self.ready_marker}' not seen "
                        f"within {self.ready_timeout}s — proceeding anyway"
                    )
            else:
                await asyncio.sleep(2)
                if self._process.returncode is not None:
                    logger.error(
                        f"[{self.name}] Exited immediately with code "
                        f"{self._process.returncode}"
                    )
                    self._running = False
                    return False

            return True

        except FileNotFoundError:
            logger.error(
                f"[{self.name}] Binary not found: {self.command[0]}. "
                f"Install with: sudo apt-get install {self.command[0]}"
            )
            self._running = False
            return False
        except Exception as e:
            logger.error(f"[{self.name}] Failed to start: {e}")
            self._running = False
            return False

    async def _monitor(self):
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                if self._ready_markers:
                    for marker in self._ready_markers:
                        if marker and marker in text:
                            self._ready_event.set()
                            break

                if self._fatal_regexes and any(rx.search(text) for rx in self._fatal_regexes):
                    logger.error(f"[{self.name}] Fatal: {text}")
                    self._fatal_seen = True
                    try:
                        if self._process and self._process.returncode is None:
                            self._process.terminate()
                    except Exception:
                        pass
                    break

                text_upper = text.upper()
                if "ERROR" in text_upper or "CRITICAL" in text_upper:
                    logger.error(f"[{self.name}] {text}")
                elif "WARN" in text_upper:
                    logger.warning(f"[{self.name}] {text}")
                elif "DEBUG" in text_upper:
                    logger.debug(f"[{self.name}] {text}")
                else:
                    logger.info(f"[{self.name}] {text}")

            if self._process:
                returncode = await self._process.wait()
                logger.warning(f"[{self.name}] Exited with code {returncode}")

            self._running = False

            if self._shutdown:
                return

            if self._fatal_seen and not self.restart_on_fatal:
                logger.error(f"[{self.name}] Fatal condition encountered — not restarting")
                return

            if self._restart_count < self.max_restarts:
                self._restart_count += 1
                delay = min(self.restart_base_delay * self._restart_count, 30.0)
                logger.info(
                    f"[{self.name}] Restarting in {delay:.0f}s "
                    f"(attempt {self._restart_count}/{self.max_restarts})"
                )
                await asyncio.sleep(delay)
                if not self._shutdown:
                    self._ready_event.clear()
                    self._fatal_seen = False
                    await self._spawn()
            else:
                logger.error(
                    f"[{self.name}] Exceeded max restarts ({self.max_restarts}), giving up"
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.name}] Monitor error: {e}")
            self._running = False

    async def stop(self):
        self._shutdown = True
        self._running = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            logger.info(f"[{self.name}] Stopping (PID {self._process.pid})...")
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10)
                    logger.info(f"[{self.name}] Stopped gracefully")
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.name}] SIGTERM timeout, sending SIGKILL")
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    await self._process.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"[{self.name}] Error stopping: {e}")

        self._process = None

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self.is_running,
            "pid": self.pid,
            "restart_count": self._restart_count,
        }


# =========================================================================
# CPC FRAMING HELPERS (Python-side, for pre-CpcCore link reset)
# =========================================================================

CPC_FLAG = 0x14
_U_SABM = 0xEF
_U_UA   = 0x63
_U_DISC = 0x43


def _cpc_crc16(data: bytes) -> int:
    """CRC-16 poly=0x1021 init=0x0000 (confirmed for Sonoff MG24 RCP)."""
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def _cpc_make_frame(ep: int, ctrl: int, payload: bytes = b"") -> bytes:
    """Encode a CPC frame. Layout: FLAG EP LEN_LO LEN_HI CTRL HCS(2) PAYLOAD FCS(2)."""
    fcs_len = 2
    length = len(payload) + fcs_len  # LEN includes FCS
    hdr = bytes([CPC_FLAG, ep, length & 0xFF, (length >> 8) & 0xFF, ctrl])
    hcs = _cpc_crc16(hdr)
    fcs = _cpc_crc16(payload)
    return hdr + struct.pack("<H", hcs) + payload + struct.pack("<H", fcs)


def _cpc_parse_frames(data: bytes):
    """
    Parse CPC frames from raw bytes.
    Yields (ep, ctrl, payload) for each valid frame.
    """
    i = 0
    while i < len(data):
        if data[i] != CPC_FLAG:
            i += 1
            continue
        if i + 7 > len(data):
            break

        ep   = data[i + 1]
        plen = data[i + 2] | (data[i + 3] << 8)
        ctrl = data[i + 4]

        # Validate HCS
        hdr = data[i:i + 5]
        hcs_recv = data[i + 5] | (data[i + 6] << 8)
        if _cpc_crc16(hdr) != hcs_recv:
            i += 1
            continue

        if plen < 2:
            i += 1
            continue

        payload_len = plen - 2
        frame_end = i + 7 + payload_len + 2
        if frame_end > len(data):
            break

        payload = data[i + 7:i + 7 + payload_len]
        fcs_recv = data[i + 7 + payload_len] | (data[i + 7 + payload_len + 1] << 8)
        if _cpc_crc16(payload) != fcs_recv:
            i += 1
            continue

        yield (ep, ctrl, payload)
        i = frame_end


# =========================================================================
# MULTIPAN MANAGER — CpcCore + optional otbr-agent
# =========================================================================

class MultiPanManager:
    """
    Manages MultiPAN RCP via zmm_cpc CpcCore (Rust CPC/HDLC).

    CpcCore replaces cpcd + zigbeed + PTYTCPBridge.  It owns the serial
    port, speaks CPC/HDLC, and exposes CPC endpoints as TCP listeners
    (ep12 → :9999 for bellows, ep13 → :9998 for OT).

    bellows connects to socket://127.0.0.1:9999 — unchanged from Phase 1.
    """

    def __init__(
            self,
            zigbee_config: dict,
            multipan_config: Optional[dict] = None,
            event_emitter: Optional[Callable] = None,
    ):
        self._config = multipan_config or {}
        self._zigbee_config = zigbee_config
        self._emit = event_emitter
        self._running = False

        # Sub-configs
        self._cpcd_config = self._config.get("cpcd", {})
        self._zigbeed_config = self._config.get("zigbeed", {})
        self._otbr_config = self._config.get("otbr", {})

        # Dongle Jedi probe result (set by start())
        self._jedi_result: Optional[dict] = None

        # Phase 2: Rust CPC core replaces cpcd + zigbeed + PTYTCPBridge
        self._cpc_core: Optional[CpcCore] = None

        # otbr-agent still uses ManagedDaemon (Phase 3)
        self._daemons: Dict[str, ManagedDaemon] = {}

    @property
    def ezsp_socket(self) -> str:
        """The socket URL for bellows/zigpy."""
        port = self._zigbeed_config.get("ezsp_port", 9999)
        return f"socket://127.0.0.1:{port}"

    @property
    def is_running(self) -> bool:
        return self._running

    # =========================================================================
    # PREREQUISITE CHECKS
    # =========================================================================

    @staticmethod
    def is_otbr_available() -> bool:
        return shutil.which("otbr-agent") is not None

    def check_prerequisites(self) -> dict:
        """Check prerequisites.  CpcCore is a Python module — always available
        if the import at the top of this file succeeded."""
        otbr = self.is_otbr_available()
        return {
            "core_available": True,   # zmm_cpc is imported
            "otbr_agent": otbr,
            "all_available": otbr,
        }

    # =========================================================================
    # COMMAND BUILDERS (otbr-agent only — Phase 3)
    # =========================================================================

    def _build_otbr_command(self) -> list:
        """
        Build otbr-agent command.

        NOTE: otbr-agent currently requires cpcd Unix sockets (spinel+cpc://).
        zmm_cpc exposes TCP endpoints, not Unix sockets.  otbr-agent
        integration is deferred to Phase 3 when zmm_cpc adds Unix socket
        support or otbr-agent gains TCP transport.
        """
        thread_iface = self._otbr_config.get("thread_interface", "wpan0")
        backbone_iface = self._otbr_config.get("backbone_interface", "eth0")
        nat64 = self._otbr_config.get("nat64", False)

        radio_url = "spinel+cpc://cpcd_0?iid=2&iid-list=0"

        cmd = [
            "otbr-agent",
            "-I", thread_iface,
            "-B", backbone_iface,
            f"--radio-url={radio_url}",
        ]

        if not nat64:
            cmd.append("--disable-nat64")

        return cmd

    # =========================================================================
    # LINK DRAIN AND RESET
    # =========================================================================

    @staticmethod
    def _drain_and_reset_link(port: str, baudrate: int = 115200) -> bool:
        """
        Reset the CPC link to a clean state before CpcCore takes over.

        The MG24 RCP persists its CPC link state across serial reconnects.
        If a previous session (cpcd, zmm_cpc, or even Dongle Jedi probe)
        left the link in an established state, the RCP will immediately
        start retransmitting unacknowledged I-frames instead of accepting
        new SABM handshakes.

        The critical insight is that the RCP's retransmitted I-frames contain
        N(R) — the sequence number the RCP expects from US next.  We must
        adopt this as our N(S) starting point.  Similarly, we must send
        RR with N(R) = (RCP's N(S) + 1) to acknowledge the stale frame.

        The RCP will not accept DISC or SABM until we speak its language —
        matching sequence numbers on both sides.

        This method:
          1. RTS toggle — attempt hardware reset
          2. Read boot burst — extract RCP's N(R) (our required N(S))
             and RCP's N(S) (to build correct RR)
          3. Send RR with correct N(R) to ack stale I-frames
          4. Send DISC on ep0/12/13 — must be accepted now that seqs match
          5. Wait for UA responses to DISC
          6. Send SABM on ep0 — verify the RCP accepts a fresh handshake
          7. Clean disconnect for CpcCore to take over
        """
        import serial as pyserial
        import time

        try:
            ser = pyserial.Serial(port, baudrate, timeout=0.5)
            ser.dtr = False
            ser.rts = False

            # ── Step 1: RTS hardware reset ─────────────────────────────
            logger.info("Link reset: RTS toggle...")
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
            time.sleep(0.5)

            # ── Step 2: Read boot burst and extract RCP sequence state ──
            logger.info("Link reset: draining stale frames...")

            # Track per-endpoint: what N(S) the RCP is sending, and what
            # N(R) it expects from us.
            rcp_ns_per_ep = {}   # ep → last N(S) seen from RCP
            rcp_nr_from_us = {}  # ep → N(R) the RCP expects from us (our N(S))
            acked_eps = set()

            for attempt in range(15):
                data = ser.read(500)
                if not data:
                    if attempt > 2 and acked_eps:
                        break  # Got frames and now silence — good
                    time.sleep(0.3)
                    continue

                for ep, ctrl, payload in _cpc_parse_frames(data):
                    if ctrl & 0x01 == 0:  # I-frame
                        ns = (ctrl >> 1) & 0x07
                        nr = (ctrl >> 5) & 0x07
                        rcp_ns_per_ep[ep] = ns
                        rcp_nr_from_us[ep] = nr

                        # Send RR acknowledging this frame
                        rr_nr = (ns + 1) & 0x07
                        rr = _cpc_make_frame(ep, 0x01 | (rr_nr << 5))
                        ser.write(rr)
                        ser.flush()
                        acked_eps.add(ep)

                        logger.info(
                            f"Link reset: ep{ep} I-frame N(S)={ns} N(R)={nr} "
                            f"→ RR N(R)={rr_nr} (RCP expects our N(S)={nr})"
                        )
                    elif ctrl & 0x03 == 0x03:  # U-frame
                        utype = ctrl & 0xEF
                        if utype == _U_SABM:
                            # RCP is sending fresh SABM — respond UA
                            ser.write(_cpc_make_frame(ep, _U_UA))
                            ser.flush()
                            logger.info(
                                f"Link reset: RCP sent SABM on ep{ep}, "
                                f"responded UA"
                            )
                            acked_eps.add(ep)

                time.sleep(0.15)

            if acked_eps:
                logger.info(
                    f"Link reset: acknowledged frames on eps {sorted(acked_eps)}"
                )
                if rcp_nr_from_us:
                    logger.info(
                        f"Link reset: RCP sequence state — "
                        + ", ".join(
                            f"ep{ep}: expects our N(S)={nr}"
                            for ep, nr in sorted(rcp_nr_from_us.items())
                        )
                    )
            else:
                logger.info("Link reset: no stale frames (clean state)")

            # ── Step 3: Send DISC on all endpoints ─────────────────────
            # The RCP now knows we've acked its stale I-frames.
            # Send DISC to tear down old link state.
            logger.info("Link reset: sending DISC on ep0, ep12, ep13...")
            for ep in [0, 12, 13]:
                disc = _cpc_make_frame(ep, _U_DISC)
                ser.write(disc)
                ser.flush()
                time.sleep(0.05)

            # ── Step 4: Drain responses to DISC ────────────────────────
            # The RCP should respond with UA to each DISC.  It may also
            # send more I-frames if our earlier RR didn't fully clear its
            # retransmit queue.
            time.sleep(0.3)
            disc_ua_count = 0
            for _ in range(8):
                resp = ser.read(500)
                if not resp:
                    break
                for ep, ctrl, payload in _cpc_parse_frames(resp):
                    if (ctrl & 0xEF) == _U_UA:
                        disc_ua_count += 1
                        logger.info(
                            f"Link reset: got UA on ep{ep} (DISC accepted)"
                        )
                    elif ctrl & 0x01 == 0:  # More I-frames
                        ns = (ctrl >> 1) & 0x07
                        rr_nr = (ns + 1) & 0x07
                        ser.write(_cpc_make_frame(ep, 0x01 | (rr_nr << 5)))
                        ser.flush()
                        logger.info(
                            f"Link reset: acked residual I-frame on ep{ep}"
                        )
                    elif (ctrl & 0xEF) == _U_SABM:
                        # RCP initiated SABM after our DISC cleared state
                        ser.write(_cpc_make_frame(ep, _U_UA))
                        ser.flush()
                        logger.info(
                            f"Link reset: RCP sent SABM on ep{ep} post-DISC, "
                            f"responded UA"
                        )
                time.sleep(0.15)

            if disc_ua_count > 0:
                logger.info(
                    f"Link reset: {disc_ua_count} DISC(s) accepted by RCP"
                )

            # ── Step 5: Verify — send SABM ep0, expect UA ─────────────
            logger.info("Link reset: verifying with SABM on ep0...")
            sabm = _cpc_make_frame(0, _U_SABM)
            ser.write(sabm)
            ser.flush()
            time.sleep(0.8)

            verified = False
            for _ in range(5):
                resp = ser.read(500)
                if not resp:
                    break
                for ep, ctrl, payload in _cpc_parse_frames(resp):
                    if ep == 0 and (ctrl & 0xEF) == _U_UA:
                        logger.info(
                            "Link reset: ✅ ep0 UA received — link is clean"
                        )
                        verified = True
                    elif ep == 0 and (ctrl & 0xEF) == _U_SABM:
                        ser.write(_cpc_make_frame(0, _U_UA))
                        ser.flush()
                        logger.info(
                            "Link reset: ✅ RCP initiated SABM on ep0, "
                            "responded UA"
                        )
                        verified = True
                    elif ctrl & 0x01 == 0:  # Still I-frames
                        ns = (ctrl >> 1) & 0x07
                        ser.write(
                            _cpc_make_frame(ep, 0x01 | (((ns + 1) & 7) << 5))
                        )
                        ser.flush()
                if verified:
                    break
                time.sleep(0.2)

            if not verified:
                logger.warning(
                    "Link reset: ep0 UA not received after SABM — "
                    "RCP may need power cycle"
                )

            # ── Step 6: Clean disconnect — DISC ep0 ────────────────────
            disc = _cpc_make_frame(0, _U_DISC)
            ser.write(disc)
            ser.flush()
            time.sleep(0.3)
            ser.read(500)  # drain any response

            # Hold DTR/RTS low before close
            ser.dtr = False
            ser.rts = False
            ser.close()

            logger.info("Link reset: complete — port released for CpcCore")
            return verified

        except Exception as e:
            logger.warning(f"Link reset failed: {e}")
            return False

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    async def start(
            self,
            serial_port: Optional[str] = None,
            jedi_result: Optional[dict] = None,
    ) -> bool:
        """
        Start the MultiPAN stack.

        The startup sequence handles the critical problem that the MG24 RCP
        persists CPC link state across serial reconnects.  If a previous
        session left the link established, the RCP retransmits stale I-frames
        and ignores new SABMs.  _drain_and_reset_link() clears this state
        before CpcCore takes over.

        Args:
            serial_port: Override serial port (e.g. from Dongle Jedi result).
            jedi_result: Full Dongle Jedi probe result dict.

        Returns True when CpcCore is running and ep12 is OPEN
        (bellows can connect to socket://127.0.0.1:9999).
        """
        self._jedi_result = jedi_result

        # Resolve serial port: explicit arg → Jedi → config → default
        port = (
                serial_port
                or (jedi_result or {}).get("port")
                or self._cpcd_config.get("serial_port")
                or self._zigbee_config.get("port", "/dev/ttyACM0")
        )

        logger.info(f"Starting MultiPAN RCP stack on {port}...")

        if self._emit:
            try:
                await self._emit("log", {
                    "level": "INFO",
                    "message": f"Starting MultiPAN RCP stack on {port}...",
                    "ieee": None,
                })
            except Exception:
                pass

        jedi = jedi_result or {}
        baud = int(
            jedi.get("baudrate")
            or jedi.get("baud_rate")
            or self._cpcd_config.get("baudrate")
            or 115200
        )

        # ── Step 1: Drain stale link state and reset ─────────────────
        # This is the critical step that makes the RCP accept fresh SABMs.
        # Runs synchronously via pyserial before CpcCore takes the port.
        logger.info("Draining stale CPC link state...")
        link_ok = self._drain_and_reset_link(port, baudrate=baud)
        if link_ok:
            logger.info("CPC link reset verified — RCP ready for fresh handshake")
        else:
            logger.warning("CPC link reset unverified — proceeding anyway")

        # ── Step 2: Brief settle ─────────────────────────────────────
        # Let the port fully release and the chip settle after DISC
        await asyncio.sleep(1)

        # ── Step 3: Start CpcCore ────────────────────────────────────
        # The RCP's link state is now CLOSED.  CpcCore will either:
        #  - Catch the RCP's fresh SABM burst (reactive path), or
        #  - Send proactive SABM after 3s grace (active path)
        ezsp_port = self._zigbeed_config.get("ezsp_port", 9999)

        tcp_endpoints = {12: ezsp_port}
        if self._otbr_config.get("enabled", False):
            ot_port = self._otbr_config.get("tcp_port", 9998)
            tcp_endpoints[13] = ot_port

        try:
            self._cpc_core = CpcCore(
                serial_port=port,
                baudrate=baud,
                tcp_endpoints=tcp_endpoints,
            )
            self._cpc_core.start()
        except Exception as e:
            logger.error(f"CpcCore failed to start: {e}")
            return False

        logger.info("CpcCore started — awaiting CPC handshake...")

        # ── Step 4: Wait for ep12 to reach OPEN ─────────────────────
        loop = asyncio.get_event_loop()
        ep12_open = await loop.run_in_executor(
            None,
            self._cpc_core.wait_endpoint_open,
            12,    # ep_id
            30.0,  # timeout_secs
        )

        if not ep12_open:
            logger.error(
                "CpcCore ep12 did not reach OPEN within 30s — "
                "RCP did not respond to SABM.  Stopping."
            )
            self._cpc_core.stop()
            self._cpc_core = None
            return False

        logger.info(f"CpcCore ep12 OPEN — EZSP available on {self.ezsp_socket}")

        # ── otbr-agent — Thread support (Phase 3, not yet compatible) ──
        otbr_enabled = self._otbr_config.get("enabled", False)
        if otbr_enabled:
            logger.warning(
                "otbr-agent requires cpcd Unix sockets (spinel+cpc://) which "
                "zmm_cpc does not yet provide.  Thread support deferred to Phase 3."
            )

        self._running = True
        logger.info(
            f"MultiPAN stack started — EZSP socket: {self.ezsp_socket}"
        )

        if self._emit:
            try:
                await self._emit("log", {
                    "level": "INFO",
                    "message": f"MultiPAN RCP active — EZSP: {self.ezsp_socket}",
                    "ieee": None,
                })
            except Exception:
                pass

        return True

    async def stop(self):
        """Stop CpcCore and any managed daemons."""
        logger.info("Stopping MultiPAN RCP stack...")
        await self._stop_all()
        logger.info("MultiPAN RCP stack stopped")

    async def _stop_all(self):
        """Stop CpcCore + daemons in reverse order."""
        self._running = False

        # Stop any managed daemons first (otbr-agent, Phase 3)
        for name in reversed(list(self._daemons.keys())):
            daemon = self._daemons[name]
            if daemon.is_running:
                await daemon.stop()
        self._daemons.clear()

        # Stop CpcCore (sends DISC on all endpoints, joins runtime thread)
        if self._cpc_core:
            self._cpc_core.stop()
            self._cpc_core = None

    def get_status(self) -> dict:
        cpc_status = None
        if self._cpc_core:
            try:
                cpc_status = self._cpc_core.status()
            except Exception:
                cpc_status = {"error": "status() failed"}

        return {
            "enabled": True,
            "running": self._running,
            "ezsp_socket": self.ezsp_socket if self._running else None,
            "cpc_core": cpc_status,
            "daemons": {
                name: daemon.get_status()
                for name, daemon in self._daemons.items()
            },
        }