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
    # USB RESET — force RCP to re-send SABMs
    # =========================================================================

    @staticmethod
    def _usb_reset(port: str) -> bool:
        """
        Perform a USB-level device reset via USBDEVFS_RESET ioctl.

        This forces the MG24 to power-cycle at the USB level, causing the
        CPC firmware to restart and re-send its SABM handshake sequence.
        Much more reliable than serial-level bootloader commands because
        it guarantees a clean firmware restart without DTR/RTS side effects.

        Requires /dev/bus/usb to be mounted into the container (build.sh
        already does this).

        Returns True if reset succeeded, False if it could not be performed
        (missing sysfs, permissions, etc.) — caller should fall back to
        serial reset.
        """
        import fcntl
        import glob

        real_port = os.path.realpath(port)
        dev_name = os.path.basename(real_port)

        # Find the USB device path via sysfs
        # /sys/class/tty/ttyUSB0/device -> ../../ttyUSB0
        # Walk up to find the USB device with busnum/devnum
        sysfs_tty = f"/sys/class/tty/{dev_name}"
        if not os.path.exists(sysfs_tty):
            logger.debug(f"USB reset: sysfs path {sysfs_tty} not found")
            return False

        try:
            # Walk up the device tree to find the USB device
            device_path = os.path.realpath(os.path.join(sysfs_tty, "device"))
            usb_device_path = device_path

            # Walk up until we find busnum and devnum
            for _ in range(10):
                busnum_path = os.path.join(usb_device_path, "busnum")
                devnum_path = os.path.join(usb_device_path, "devnum")
                if os.path.exists(busnum_path) and os.path.exists(devnum_path):
                    break
                usb_device_path = os.path.dirname(usb_device_path)
            else:
                logger.debug("USB reset: could not find busnum/devnum in sysfs tree")
                return False

            with open(busnum_path) as f:
                busnum = int(f.read().strip())
            with open(devnum_path) as f:
                devnum = int(f.read().strip())

            usb_dev_path = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"

            if not os.path.exists(usb_dev_path):
                logger.debug(f"USB reset: {usb_dev_path} not found")
                return False

            # USBDEVFS_RESET ioctl number
            USBDEVFS_RESET = 0x5514

            logger.info(f"USB reset: resetting {usb_dev_path} (bus {busnum}, dev {devnum})")
            fd = os.open(usb_dev_path, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)

            logger.info(f"USB reset: {usb_dev_path} reset successfully")
            return True

        except PermissionError:
            logger.warning("USB reset: permission denied — /dev/bus/usb may not be mounted")
            return False
        except Exception as e:
            logger.warning(f"USB reset: failed — {e}")
            return False

    # =========================================================================
    # SERIAL RESET (fallback if USB reset unavailable)
    # =========================================================================

    @staticmethod
    def _reset_serial_state(port: str, baudrate: int = 115200) -> bool:
        """
        Ensure the MG24 is in application mode, not Gecko Bootloader.

        If a previous reset_sequence put the chip into bootloader (DTR+RTS),
        sending '2' (the bootloader "run" command) boots the application.
        Safe to send even if already in application mode (ignored as garbage).

        IMPORTANT: We explicitly prevent pyserial from toggling DTR/RTS on
        close, as that can re-enter the Gecko Bootloader and cause a race
        with CpcCore's serial open.
        """
        import serial as pyserial
        import time

        try:
            ser = pyserial.Serial(port, baudrate, timeout=1)
            ser.reset_input_buffer()

            # Ensure DTR and RTS are deasserted (low) — safe state for MG24
            ser.dtr = False
            ser.rts = False

            ser.write(b"2\r\n")
            time.sleep(0.5)

            ser.write(b"\n")
            time.sleep(0.3)
            ser.write(b"2\r\n")
            time.sleep(1.5)

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # Prevent DTR/RTS toggle on close — pyserial's default close()
            # can briefly assert DTR+RTS which re-enters the Gecko Bootloader
            # on MG24, causing the RCP to miss its CPC boot window.
            ser.dtr = False
            ser.rts = False

            ser.close()
            logger.info(f"Bootloader exit command sent for {port}")
            return True
        except Exception as e:
            logger.warning(f"Bootloader exit failed: {e}")
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

        Strategy: Start CpcCore FIRST (so the serial reader is active and
        ready to receive frames), THEN trigger a USB-level reset to force
        the RCP to reboot and send its SABM handshake.  This eliminates
        the race condition where SABMs arrive while the port is closed
        between pyserial and CpcCore.

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

        # ── Step 1: Ensure chip is out of bootloader ─────────────────────
        # Use serial reset to send bootloader exit command.  This is a
        # best-effort step — if the chip is already in application mode,
        # the "2\r\n" bytes are harmless CPC garbage (bad CRC, discarded).
        logger.info("Sending bootloader exit command (pre-flight)...")
        self._reset_serial_state(port, baudrate=baud)

        # Brief settle — let the chip finish booting into CPC firmware.
        # The RCP will start sending SABMs here, but we haven't opened
        # the port yet.  That's OK — we'll force a re-send via USB reset
        # in Step 3 below.
        await asyncio.sleep(1)

        # ── Step 2: Start CpcCore — opens serial, binds TCP ports ────────
        # CpcCore's serial reader is now active and will capture any
        # SABM frames that arrive on the wire.
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

        # ── Step 3: Force RCP to re-send SABMs ──────────────────────────
        # Now that CpcCore is listening, trigger a USB-level reset.
        # This causes the MG24 to power-cycle and re-run its CPC init
        # sequence, sending fresh SABM frames that CpcCore will catch.
        logger.info("Triggering USB reset to force RCP SABM re-handshake...")
        usb_reset_ok = self._usb_reset(port)

        if not usb_reset_ok:
            # USB reset not available (no /dev/bus/usb, permissions, etc.)
            # Fall back to a serial-level RTS toggle to nudge the chip.
            # This is less reliable but may work on some configurations.
            logger.warning(
                "USB reset unavailable — falling back to RTS toggle. "
                "If handshake fails, ensure /dev/bus/usb is mounted."
            )
            # Send a brief break condition to provoke a CPC reset
            try:
                import serial as pyserial
                ser = pyserial.Serial(port, baud, timeout=0.5)
                ser.dtr = False
                ser.rts = False
                # Quick RTS pulse — some MG24 firmware versions use this
                # as a soft-reset trigger without entering bootloader
                ser.rts = True
                await asyncio.sleep(0.05)
                ser.rts = False
                ser.close()
                logger.info("RTS pulse sent as fallback reset")
            except Exception as e:
                logger.warning(f"RTS fallback also failed: {e}")

        # ── Step 4: Wait for ep12 to reach OPEN ─────────────────────────
        # The USB reset causes the RCP to reboot (~2-5s) and then send
        # SABM on ep0, ep12 (and ep13 if Thread firmware).
        # CpcCore's router will respond with UA automatically.
        # Give it up to 30s for the full sequence.
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
                "RCP may not have sent SABM.  Stopping."
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