"""
MultiPAN RCP Manager
=====================
Orchestrates Silicon Labs CPC stack daemons (cpcd, zigbeed, socat, otbr-agent)
as managed subprocesses for concurrent Zigbee + Thread on a single RCP radio.

Follows the same managed-subprocess pattern as EmbeddedMatterServer.

Startup order is critical:
  1. cpcd       — owns serial port, speaks CPC to RCP firmware
  2. zigbeed    — connects to cpcd, runs EmberZNet stack host-side
  3. socat      — bridges zigbeed's PTY to a TCP socket for bellows
  4. otbr-agent — connects to cpcd, runs OpenThread stack + border routing

bellows/zigpy then connects to the socat TCP socket (socket://localhost:{port}).

IMPORTANT: This module does NOT touch the existing Zigbee startup path.
When MultiPAN is active, the only visible change to core.py is that
self.port becomes "socket://localhost:9999" instead of "/dev/ttyACM0".
Everything downstream — probe_radio_type(), _build_ezsp_config(),
ControllerApplication.new() — works unchanged because they already
handle socket paths.

Integration point: core/service.py ZigbeeService.start()
  - Dongle Jedi detects CPC_MULTIPAN firmware
  - _probe_with_jedi() returns probe result with adapter_family
  - start() launches MultiPanManager BEFORE building bellows config
  - self.port is overridden to the zigbeed EZSP socket
  - Rest of startup proceeds unchanged
"""
import asyncio
import logging
import os
import signal
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Dict, Callable

from modules.pty_bridge import PTYTCPBridge

logger = logging.getLogger("multipan")


# =========================================================================
# MANAGED DAEMON — generic subprocess wrapper
# =========================================================================

# multipan.py
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

        # multi-ready & fatal support
        # Normalize ready markers as plain strings (case-sensitive match by default)
        self._ready_markers = set(ready_markers or [])
        if ready_marker:
            self._ready_markers.add(ready_marker)

        # Compile fatal markers as case-insensitive regexes for flexibility
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
        """Start the daemon. Returns True when process is running (and optionally ready)."""
        if self.is_running:
            logger.warning(f"[{self.name}] Already running (PID {self.pid})")
            return True

        self._shutdown = False
        self._restart_count = 0
        self._ready_event.clear()
        return await self._spawn()

    async def _spawn(self) -> bool:
        """Spawn the subprocess."""
        # Cancel any existing monitor to prevent double-monitoring
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info(f"[{self.name}] Starting: {' '.join(self.command)}")

        try:
            # Build environment
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

            # Start log reader + monitor
            self._monitor_task = asyncio.create_task(self._monitor())

            # Wait for ready marker or brief stability check
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
                # Brief stability check — did it crash immediately?
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
        """Stream stdout to logger and handle restarts on crash/fatal markers."""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                # 1) Check for any ready marker (support multiple)
                if self._ready_markers:
                    for marker in self._ready_markers:
                        if marker and marker in text:
                            self._ready_event.set()
                            break

                # 2) Fatal marker detection (regex, case-insensitive)
                if self._fatal_regexes and any(rx.search(text) for rx in self._fatal_regexes):
                    logger.error(f"[{self.name}] Fatal: {text}")
                    self._fatal_seen = True
                    # Terminate the process and stop monitoring immediately
                    try:
                        if self._process and self._process.returncode is None:
                            self._process.terminate()
                    except Exception:
                        pass
                    # No more streaming; let the restart logic decide
                    break

                # 3) Route to appropriate log level
                text_upper = text.upper()
                if "ERROR" in text_upper or "CRITICAL" in text_upper:
                    logger.error(f"[{self.name}] {text}")
                elif "WARN" in text_upper:
                    logger.warning(f"[{self.name}] {text}")
                elif "DEBUG" in text_upper:
                    logger.debug(f"[{self.name}] {text}")
                else:
                    logger.info(f"[{self.name}] {text}")

            # Process ended — wait for exit code
            if self._process:
                returncode = await self._process.wait()
                logger.warning(f"[{self.name}] Exited with code {returncode}")

            self._running = False

            # 4) Decide about restart
            if self._shutdown:
                return

            # If we saw a fatal marker and restarts are disabled for fatal, bail out
            if self._fatal_seen and not self.restart_on_fatal:
                logger.error(f"[{self.name}] Fatal condition encountered — not restarting")
                return

            # Regular bounded backoff restart path
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
                    self._fatal_seen = False  # reset fatal flag before respawn
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
        """Stop the daemon gracefully: SIGTERM → timeout → SIGKILL."""
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
                pass  # Already dead
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
# MULTIPAN MANAGER — orchestrates cpcd + zigbeed + socat + otbr-agent
# =========================================================================

class MultiPanManager:
    """
    Orchestrates cpcd + zigbeed + socat + otbr-agent for MultiPAN RCP.

    Manages daemon lifecycle in correct dependency order and provides
    a single EZSP socket URL for bellows/zigpy to connect to.

    IMPORTANT: This class does not modify the Zigbee startup path.
    It only provides the socket URL — the existing _probe_radio_type()
    and _build_ezsp_config() handle socket paths transparently.
    """

    def __init__(
            self,
            zigbee_config: dict,
            multipan_config: Optional[dict] = None,
            event_emitter: Optional[Callable] = None,
    ):
        # multipan section from config.yaml, or auto-generated defaults
        self._config = multipan_config or {}
        self._zigbee_config = zigbee_config
        self._emit = event_emitter
        self._daemons: Dict[str, ManagedDaemon] = {}
        self._running = False
        self._generated_config_dir: Optional[str] = None

        # Sub-configs with defaults
        self._cpcd_config = self._config.get("cpcd", {})
        self._zigbeed_config = self._config.get("zigbeed", {})
        self._otbr_config = self._config.get("otbr", {})

        # Dongle Jedi probe result (set by start())
        self._jedi_result: Optional[dict] = None

        self._pty_bridge: Optional['PTYTCPBridge'] = None

    @property
    def ezsp_socket(self) -> str:
        """The socket URL for bellows/zigpy to connect to zigbeed via socat."""
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
        return shutil.which("cpcd") is not None

    @staticmethod
    def is_zigbeed_available() -> bool:
        return shutil.which("zigbeed") is not None

    @staticmethod
    def is_socat_available() -> bool:
        return shutil.which("socat") is not None

    @staticmethod
    def is_otbr_available() -> bool:
        return shutil.which("otbr-agent") is not None

    def check_prerequisites(self) -> dict:
        """Check all required binaries are installed."""
        cpcd = self.is_cpcd_available()
        zigbeed = self.is_zigbeed_available()
        socat = self.is_socat_available()
        otbr = self.is_otbr_available()

        return {
            "cpcd": cpcd,
            "zigbeed": zigbeed,
            "socat": socat,
            "otbr_agent": otbr,
            "core_available": cpcd and zigbeed,
            "all_available": cpcd and zigbeed and socat and otbr,
        }

    # =========================================================================
    # CONFIG FILE GENERATION
    # =========================================================================

    async def _wait_for_file(self, path: str, timeout: float = 20.0) -> bool:
        """Wait until a file/socket path exists."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if os.path.exists(path):
                return True
            await asyncio.sleep(0.5)
        return False

    def _ensure_config_dir(self) -> str:
        """Create a temporary directory for generated config files."""
        if not self._generated_config_dir:
            self._generated_config_dir = tempfile.mkdtemp(prefix="multipan_")
            logger.info(f"Generated config dir: {self._generated_config_dir}")
        return self._generated_config_dir

    def _generate_cpcd_config(self, serial_port: str) -> str:
        """
        Generate a cpcd.conf if the system one doesn't exist.

        cpcd needs a config file — it doesn't accept serial port via CLI
        in all versions. We generate a minimal one.

        Value priority: Jedi probe result → multipan.cpcd config → defaults.
        """
        system_config = self._cpcd_config.get("config_file", "/usr/local/etc/cpcd.conf")
        if os.path.exists(system_config):
            logger.info(f"[cpcd] Using existing config: {system_config}")
            return system_config

        jedi = self._jedi_result or {}

        baudrate = (
                jedi.get("baudrate") or jedi.get("baud_rate")
                or self._cpcd_config.get("baudrate")
                or 115200
        )

        flow_control = (
                jedi.get("flow_control")
                or self._cpcd_config.get("flow_control")
                or "none"
        )

        # Map flow control naming to cpcd's uart_hardflow boolean
        fc_map = {"hardware": "true", "rtscts": "true", "none": "false",
                  "software": "false", "xonxoff": "false"}
        fc_value = fc_map.get(str(flow_control).lower(), "false")

        config_dir = self._ensure_config_dir()
        config_path = os.path.join(config_dir, "cpcd.conf")

        config_content = f"""\
# Auto-generated by ZigBee Matter Manager MultiPAN
instance_name: cpcd_0
bus_type: UART
uart_device_file: {serial_port}
uart_device_baud: {baudrate}
uart_hardflow: {fc_value}
disable_encryption: true
reset_sequence: true
"""

        with open(config_path, "w") as f:
            f.write(config_content)

        logger.info(
            f"[cpcd] Generated config at {config_path} "
            f"(baud={baudrate}, flow={flow_control}→hardflow={fc_value})"
        )
        return config_path

    # =========================================================================
    # COMMAND BUILDERS
    # =========================================================================

    def _build_cpcd_command(self, serial_port: str) -> list:
        """Build cpcd command with config file."""
        config_path = self._generate_cpcd_config(serial_port)
        return ["cpcd", "-c", config_path]

    def _build_zigbeed_command(self) -> list:
        """Build zigbeed command."""
        config_file = self._zigbeed_config.get(
            "config_file", "/usr/local/etc/zigbeed.conf"
        )
        cmd = ["zigbeed"]
        if os.path.exists(config_file):
            cmd.extend(["-c", config_file])
        return cmd

    def _build_socat_command(self) -> list:
        """Create a PTY for zigbeed and bridge it to a TCP listener.

        zigbeed will open the PTY path (ezsp-interface in zigbeed.conf).
        Our app connects to TCP 127.0.0.1:<ezsp_port>.
        """
        ezsp_port = self._zigbeed_config.get("ezsp_port", 9999)
        # IMPORTANT: use the same path zigbeed.conf uses
        pty_path = self._zigbeed_config.get("pty_path", "/tmp/ttyZigbeeNCP")

        # Create PTY and listen on TCP. Either direction is fine; this variant
        # creates the PTY first and then listens on TCP:
        return [
            "socat",
            f"PTY,link={pty_path},raw,echo=0,mode=660",
            f"TCP-LISTEN:{ezsp_port},reuseaddr,fork",
        ]

    def _build_otbr_command(self) -> list:
        thread_iface = self._otbr_config.get("thread_interface", "wpan0")
        backbone_iface = self._otbr_config.get("backbone_interface", None)

        if not backbone_iface:
            import subprocess
            try:
                result = subprocess.run(
                    ["ip", "-o", "route", "show", "to", "default"],
                    capture_output=True, text=True, timeout=5
                )
                parts = result.stdout.strip().split()
                if "dev" in parts:
                    backbone_iface = parts[parts.index("dev") + 1]
                    logger.info(f"[otbr-agent] Auto-detected backbone interface: {backbone_iface}")
            except Exception as e:
                logger.warning(f"[otbr-agent] Failed to auto-detect backbone interface: {e}")

            if not backbone_iface:
                backbone_iface = "eth0"
                logger.warning(f"[otbr-agent] Falling back to default backbone interface: {backbone_iface}")

        radio_url = "spinel+cpc://cpcd_0?iid=2&iid-list=0"

        return [
            "otbr-agent",
            "-I", thread_iface,
            "-B", backbone_iface,
            "-d", "7",
            "-s",
            radio_url,
        ]

    # =========================================================================
    # SERIAL RESET
    # =========================================================================

    @staticmethod
    def _reset_serial_state(port: str, baudrate: int = 115200) -> bool:
        """
        Ensure the MG24 is in application mode, not Gecko Bootloader.

        If cpcd's reset_sequence puts the chip into bootloader (DTR+RTS),
        sending '2' (the bootloader "run" command) boots the application.
        Safe to send even if already in application mode (ignored as garbage).
        """
        import serial as pyserial
        import time

        try:
            ser = pyserial.Serial(port, baudrate, timeout=1)
            ser.reset_input_buffer()

            # Send Gecko Bootloader "run" command to exit bootloader
            # Command '2' = boot into application firmware
            # Harmless if chip is already running application (CPC ignores it)
            ser.write(b"2\r\n")
            time.sleep(0.5)

            # Also try sending a newline to trigger bootloader menu
            # then "2" to select run — covers both menu and direct mode
            ser.write(b"\n")
            time.sleep(0.3)
            ser.write(b"2\r\n")
            time.sleep(1.5)

            ser.reset_input_buffer()
            ser.reset_output_buffer()
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
        Start the MultiPAN stack in dependency order.

        Args:
            serial_port: Override serial port (e.g. from Dongle Jedi result).
                         Falls back to cpcd config, then /dev/ttyACM0.
            jedi_result: Full Dongle Jedi probe result dict. Contains
                         port, baud_rate, flow_control, adapter_family, etc.
                         Used to generate cpcd.conf with proven-correct values.

        Returns True when cpcd + zigbeed + socat are running and the EZSP
        socket is ready for bellows to connect.
        """
        # Store Jedi result for config generation
        self._jedi_result = jedi_result

        # Resolve serial port: explicit arg → Jedi → config → default
        port = (
                serial_port
                or (jedi_result or {}).get("port")
                or self._cpcd_config.get("serial_port")
                or self._zigbee_config.get("port", "/dev/ttyACM0")
        )

        prereqs = self.check_prerequisites()
        if not prereqs["core_available"]:
            missing = []
            if not prereqs["cpcd"]:
                missing.append("cpcd")
            if not prereqs["zigbeed"]:
                missing.append("zigbeed")
            if not prereqs["socat"]:
                missing.append("socat")
            logger.error(
                f"MultiPAN prerequisites not met. Missing: {', '.join(missing)}. "
                f"Install with: sudo apt-get install {' '.join(missing)}"
            )
            return False

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

        # Reset chip via RTS-only (avoids bootloader entry from DTR+RTS)
        logger.info("Resetting chip via RTS-only toggle...")
        jedi = jedi_result or {}
        baud = int(
            jedi.get("baudrate")
            or jedi.get("baud_rate")
            or self._cpcd_config.get("baudrate")
            or 115200
        )
        self._reset_serial_state(port, baudrate=baud)
        await asyncio.sleep(2)

        # ── 1. cpcd — must be first, owns serial port ──────────────────
        # No serial reset needed — USB pre-detection in Dongle Jedi
        # avoids CPC wire probing, so the state machine is already clean.
        cpcd = ManagedDaemon(
            name="cpcd",
            command=self._build_cpcd_command(port),
            ready_marker="Daemon startup was successful",
            ready_timeout=30.0,
            require_ready=True,
        )
        self._daemons["cpcd"] = cpcd

        if not await cpcd.start():
            logger.error("Failed to start cpcd — cannot proceed with MultiPAN")
            await self._stop_all()
            return False

        # Brief settle time for CPC endpoints to initialise
        await asyncio.sleep(1)

        # 2) PTY↔TCP bridge — replaces socat, in-process
        pty_path = self._zigbeed_config.get("pty_path", "/tmp/ttyZigbeeNCP")
        ezsp_port = self._zigbeed_config.get("ezsp_port", 9999)
        self._pty_bridge = PTYTCPBridge(
            pty_path=pty_path,
            tcp_port=ezsp_port,
        )
        if not await self._pty_bridge.start():
            logger.error("Failed to start PTY-TCP bridge — stopping cpcd")
            await self._stop_all()
            return False

        # 3) zigbeed
        zigbeed = ManagedDaemon(
            name="zigbeed",
            command=self._build_zigbeed_command(),
            ready_markers=["EZSP"],
            ready_timeout=30.0,
            fatal_markers=[
                r"CPC endpoint open failed",
                r"Init\(\) at .*spinel_driver\.cpp",
            ],
            restart_on_fatal=False,
        )
        self._daemons["zigbeed"] = zigbeed
        if not await zigbeed.start():
            logger.error("Failed to start zigbeed — stopping bridge + cpcd")
            await self._stop_all()
            return False

        # ── 4. otbr-agent — Thread support (optional, non-blocking) ────
        otbr_enabled = self._otbr_config.get("enabled", True)
        if otbr_enabled and self.is_otbr_available():
            otbr = ManagedDaemon(
                name="otbr-agent",
                command=self._build_otbr_command(),
                ready_marker="Thread interface is up",
                ready_timeout=30.0,
                max_restarts=0,
                restart_on_fatal=False,
            )
            self._daemons["otbr-agent"] = otbr

            if not await otbr.start():
                logger.warning(
                    "Failed to start otbr-agent — Thread will not be available, "
                    "but Zigbee will still work via MultiPAN"
                )
                # Don't fail the whole stack for optional Thread support
            else:
                # Restore previously saved Thread network credentials
                asyncio.create_task(self._restore_thread_network())
        elif not otbr_enabled:
            logger.info("OTBR not enabled in config — Thread support disabled")
        elif not self.is_otbr_available():
            logger.info("otbr-agent not installed — Thread support unavailable")

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

    async def _wait_for_tcp_socket(
            self, host: str, port: int, timeout: float = 10
    ) -> bool:
        """Wait for a TCP socket to accept connections."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=2.0,
                )
                writer.close()
                await writer.wait_closed()
                logger.info(f"EZSP socket accepting connections on {host}:{port}")
                return True
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(0.5)
        return False

    async def stop(self):
        """Stop all daemons in reverse dependency order."""
        logger.info("Stopping MultiPAN RCP stack...")
        await self._stop_all()
        logger.info("MultiPAN RCP stack stopped")

    async def _restore_thread_network(self):
        """
        Restore previously saved Thread credentials after otbr-agent is ready.
        Runs as a fire-and-forget task — failure is non-fatal.
        """
        try:
            # Brief settle time for otbr-agent D-Bus interface
            await asyncio.sleep(3)

            from otbr_routes import restore_thread_dataset
            restored = await restore_thread_dataset()
            if restored:
                if self._emit:
                    try:
                        await self._emit("log", {
                            "level": "INFO",
                            "message": "Thread network restored from saved credentials",
                            "ieee": None,
                        })
                    except Exception:
                        pass
            else:
                logger.info("No stored Thread dataset to restore")
        except Exception as e:
            logger.warning(f"Thread dataset restore failed (non-fatal): {e}")

    async def _stop_all(self):
        """Stop all daemons in reverse order."""
        # Stop PTY bridge first (it's in-process, not a daemon)
        if self._pty_bridge:
            await self._pty_bridge.stop()
            self._pty_bridge = None
        self._running = False
        for name in reversed(list(self._daemons.keys())):
            daemon = self._daemons[name]
            if daemon.is_running:
                await daemon.stop()
        self._daemons.clear()

        # Clean up generated configs
        if self._generated_config_dir and os.path.exists(self._generated_config_dir):
            try:
                shutil.rmtree(self._generated_config_dir)
            except Exception:
                pass
            self._generated_config_dir = None

    def get_status(self) -> dict:
        return {
            "enabled": True,
            "running": self._running,
            "ezsp_socket": self.ezsp_socket if self._running else None,
            "prerequisites": self.check_prerequisites(),
            "bridge": self._pty_bridge.get_status() if self._pty_bridge else None,
            "daemons": {
                name: daemon.get_status()
                for name, daemon in self._daemons.items()
            },
        }