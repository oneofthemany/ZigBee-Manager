"""
MultiPAN RCP Manager
=====================
Manages MultiPAN RCP radio via cpcd (Silicon Labs CPC daemon) for concurrent
Zigbee + Thread on a single radio.

Architecture:
  MG24 serial → cpcd (CPC/HDLC, TDM, NVM3 state) → Unix sockets
                  └── /dev/shm/cpcd_0/ep12.sock → PTYTCPBridge → TCP :9999 → bellows
                  └── /dev/shm/cpcd_0/ep13.sock → otbr-agent (Phase 3)

cpcd handles all CPC link-layer complexity at native C speed:
  - NVM3-persisted sequence state across reconnects
  - 192µs TDM slot timing for MultiPAN
  - Retransmit, windowing, and flow control
  - DTR/RTS reset sequence for CP210x boards
  - CPC protocol versioning and encryption negotiation

The PTYTCPBridge relays cpcd's Unix socket endpoint to a TCP port
that bellows can connect to — replacing the external socat dependency
with an in-process asyncio relay.

Integration point: core/service.py ZigbeeService.start()
  - Dongle Jedi detects CPC_MULTIPAN firmware
  - start() launches cpcd → waits for ready → starts PTY bridge
  - self.port is overridden to the EZSP socket
  - Rest of startup proceeds unchanged
"""
import asyncio
import logging
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Optional, Dict, Callable

from .pty_bridge import PTYTCPBridge

logger = logging.getLogger("multipan")


# =========================================================================
# MANAGED DAEMON — generic subprocess wrapper
# =========================================================================


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
# CPCD CONFIG WRITER
# =========================================================================

def _write_cpcd_conf(
        serial_port: str,
        baudrate: int = 115200,
        instance_name: str = "cpcd_0",
        socket_folder: str = "/dev/shm",
        hardflow: bool = False,
        disable_encryption: bool = True,
        reset_sequence: bool = True,
) -> str:
    """
    Write a cpcd.conf file and return its path.

    cpcd reads its config from a YAML-like key: value file.
    We generate a minimal one matching the dongle's parameters.
    """
    conf_dir = Path("/tmp/zmm")
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / "cpcd.conf"

    lines = [
        f"instance_name: {instance_name}",
        f"bus_type: UART",
        f"uart_device_file: {serial_port}",
        f"uart_device_baud: {baudrate}",
        f"uart_hardflow: {'true' if hardflow else 'false'}",
        f"disable_encryption: {'true' if disable_encryption else 'false'}",
        f"reset_sequence: {'true' if reset_sequence else 'false'}",
        f"socket_folder: {socket_folder}",
        f"stdout_trace: true",
        f"file_tracing: false",
    ]

    conf_path.write_text("\n".join(lines) + "\n")
    logger.info(f"cpcd config written to {conf_path}")
    return str(conf_path)


# =========================================================================
# MULTIPAN MANAGER — cpcd + PTYTCPBridge
# =========================================================================

class MultiPanManager:
    """
    Manages MultiPAN RCP via cpcd (Silicon Labs CPC daemon).

    Architecture:
      MG24 serial → cpcd → Unix socket ep12 → PTYTCPBridge → TCP :9999 → bellows

    cpcd handles all CPC link-layer complexity at native C speed:
      - NVM3-persisted sequence state across reconnects
      - 192µs TDM slot timing for MultiPAN
      - Retransmit, windowing, and flow control
      - DTR/RTS reset sequence for CP210x boards

    bellows connects to socket://127.0.0.1:9999 — unchanged.
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

        # cpcd managed daemon
        self._cpcd: Optional[ManagedDaemon] = None

        # PTY↔TCP bridge (replaces socat)
        self._bridge: Optional[PTYTCPBridge] = None

        # otbr-agent (Phase 3)
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
    def is_cpcd_available() -> bool:
        """Check if cpcd binary is installed."""
        return shutil.which("cpcd") is not None

    @staticmethod
    def is_otbr_available() -> bool:
        return shutil.which("otbr-agent") is not None

    def check_prerequisites(self) -> dict:
        cpcd = self.is_cpcd_available()
        otbr = self.is_otbr_available()
        return {
            "cpcd_available": cpcd,
            "otbr_agent": otbr,
            "all_available": cpcd,  # cpcd is the minimum requirement
        }

    # =========================================================================
    # COMMAND BUILDERS
    # =========================================================================

    def _build_cpcd_command(self, conf_path: str) -> list:
        """Build cpcd command line."""
        return ["cpcd", "--conf", conf_path]

    def _build_otbr_command(self) -> list:
        """
        Build otbr-agent command.

        With cpcd as the transport, otbr-agent can connect directly
        via its native spinel+cpc:// protocol — no bridging needed.
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
    # LIFECYCLE
    # =========================================================================

    async def start(
            self,
            serial_port: Optional[str] = None,
            jedi_result: Optional[dict] = None,
    ) -> bool:
        """
        Start the MultiPAN stack.

        Sequence:
          1. Write cpcd.conf for the detected dongle
          2. Start cpcd as a ManagedDaemon
          3. Wait for cpcd's "Connected to Secondary" ready marker
          4. Start PTYTCPBridge to relay cpcd ep12 → TCP :9999
          5. bellows can now connect to socket://127.0.0.1:9999

        Returns True when cpcd is running and the bridge is active.
        """
        self._jedi_result = jedi_result

        # Resolve serial port
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

        # ── Check prerequisites ──────────────────────────────────────
        if not self.is_cpcd_available():
            logger.error(
                "cpcd binary not found. Install Silicon Labs CPC daemon"
            )
            return False

        # ── Resolve parameters ───────────────────────────────────────
        jedi = jedi_result or {}
        baud = int(
            jedi.get("baudrate")
            or jedi.get("baud_rate")
            or self._cpcd_config.get("baudrate")
            or 115200
        )

        hardflow = self._cpcd_config.get("hardflow", False)
        disable_encryption = self._cpcd_config.get("disable_encryption", True)
        reset_sequence = self._cpcd_config.get("reset_sequence", True)
        instance_name = self._cpcd_config.get("instance_name", "cpcd_0")
        socket_folder = self._cpcd_config.get("socket_folder", "/dev/shm")

        # ── Step 1: Write cpcd config ────────────────────────────────
        conf_path = _write_cpcd_conf(
            serial_port=port,
            baudrate=baud,
            instance_name=instance_name,
            socket_folder=socket_folder,
            hardflow=hardflow,
            disable_encryption=disable_encryption,
            reset_sequence=reset_sequence,
        )

        # ── Step 2: Start cpcd ───────────────────────────────────────
        self._cpcd = ManagedDaemon(
            name="cpcd",
            command=self._build_cpcd_command(conf_path),
            ready_marker="Connected to Secondary",
            ready_timeout=30.0,
            max_restarts=3,
            restart_base_delay=5.0,
            ready_markers=[
                "Connected to Secondary",
            ],
            fatal_markers=[
                r"ASSERT.*FATAL",
                r"Secondary Protocol.*doesn't match",
            ],
            require_ready=True,
        )

        cpcd_ok = await self._cpcd.start()
        if not cpcd_ok:
            logger.error("cpcd failed to start or reach ready state")
            await self._stop_all()
            return False

        logger.info("cpcd is running and connected to RCP")

        # Brief settle for cpcd to complete endpoint init
        await asyncio.sleep(1.0)

        # ── Step 3: Start PTY↔TCP bridge ─────────────────────────────
        ezsp_port = self._zigbeed_config.get("ezsp_port", 9999)

        self._bridge = PTYTCPBridge(
            pty_path="/tmp/ttyZigbeeNCP",
            tcp_port=ezsp_port,
        )

        bridge_ok = await self._bridge.start()
        if not bridge_ok:
            logger.error("PTY↔TCP bridge failed to start")
            await self._stop_all()
            return False

        logger.info(
            f"PTY↔TCP bridge active: "
            f"{self._bridge.pty_path} ↔ TCP :{ezsp_port}"
        )

        # ── Optional: otbr-agent (Thread) ────────────────────────────
        otbr_enabled = self._otbr_config.get("enabled", False)
        if otbr_enabled and self.is_otbr_available():
            otbr_daemon = ManagedDaemon(
                name="otbr-agent",
                command=self._build_otbr_command(),
                ready_marker="Thread interface is up",
                ready_timeout=30.0,
                max_restarts=3,
            )
            self._daemons["otbr-agent"] = otbr_daemon
            otbr_ok = await otbr_daemon.start()
            if otbr_ok:
                logger.info("otbr-agent started — Thread network active")
            else:
                logger.warning("otbr-agent failed to start — Thread unavailable")
        elif otbr_enabled:
            logger.warning(
                "otbr-agent enabled in config but binary not found — "
                "Thread support unavailable"
            )

        # ── Done ─────────────────────────────────────────────────────
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
        """Stop cpcd, bridge, and any managed daemons."""
        logger.info("Stopping MultiPAN RCP stack...")
        await self._stop_all()
        logger.info("MultiPAN RCP stack stopped")

    async def _stop_all(self):
        """Stop everything in reverse order."""
        self._running = False

        # Stop managed daemons first (otbr-agent)
        for name in reversed(list(self._daemons.keys())):
            daemon = self._daemons[name]
            if daemon.is_running:
                await daemon.stop()
        self._daemons.clear()

        # Stop PTY bridge
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

        # Stop cpcd last (it owns the serial port)
        if self._cpcd:
            await self._cpcd.stop()
            self._cpcd = None

    def get_status(self) -> dict:
        cpcd_status = None
        if self._cpcd:
            cpcd_status = self._cpcd.get_status()

        bridge_status = None
        if self._bridge:
            bridge_status = self._bridge.get_status()

        return {
            "enabled": True,
            "running": self._running,
            "ezsp_socket": self.ezsp_socket if self._running else None,
            "cpcd": cpcd_status,
            "bridge": bridge_status,
            "daemons": {
                name: daemon.get_status()
                for name, daemon in self._daemons.items()
            },
        }