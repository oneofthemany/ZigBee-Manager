"""
Matter Server Manager — runs python-matter-server as a managed subprocess.

The CHIP SDK requires owning the main thread and cannot coexist in-process
with another asyncio event loop (uvicorn). This module spawns it as a child
process and monitors it, giving you single-service management without Docker.

Install:
    pip install python-matter-server[server] --break-system-packages

The subprocess exposes ws://localhost:{port}/ws which the MatterBridge
connects to.
"""

import asyncio
import logging
import os
import signal
import shutil
from typing import Optional

logger = logging.getLogger("matter_server")


class EmbeddedMatterServer:
    """
    Manages python-matter-server as a child process.

    Despite the name 'Embedded', it runs as a subprocess — but is fully
    managed by ZigBee Manager's lifecycle (start/stop with the service).
    """

    def __init__(
            self,
            storage_path: str = "./data/matter",
            port: int = 5580,
            vendor_id: int = 0xFFF1,
            fabric_id: int = 1,
            bluetooth_adapter: Optional[int] = None,
            log_level: str = "info",
    ):
        self.storage_path = os.path.abspath(storage_path)
        self.port = port
        self.vendor_id = vendor_id
        self.fabric_id = fabric_id
        self.bluetooth_adapter = bluetooth_adapter
        self.log_level = log_level
        self._process: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._shutdown = False
        self._restart_count = 0
        self._max_restarts = 5

    @property
    def is_available(self) -> bool:
        """Check if python-matter-server is installed."""
        return shutil.which("matter-server") is not None or self._check_module()

    @staticmethod
    def _check_module() -> bool:
        """Check if the module can be run via python -m."""
        try:
            import matter_server.server  # noqa: F401
            return True
        except ImportError:
            return False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def ws_url(self) -> str:
        """WebSocket URL for the bridge to connect to."""
        return f"ws://localhost:{self.port}/ws"

    def _build_command(self) -> list:
        """Build the command line for the subprocess."""
        # Prefer the entry point, fall back to python -m
        if shutil.which("matter-server"):
            cmd = ["matter-server"]
        else:
            import sys
            cmd = [sys.executable, "-m", "matter_server.server"]

        cmd.extend([
            "--storage-path", self.storage_path,
            "--port", str(self.port),
            "--vendorid", str(self.vendor_id),
            "--fabricid", str(self.fabric_id),
            "--log-level", self.log_level,
        ])

        if self.bluetooth_adapter is not None:
            cmd.extend(["--bluetooth-adapter", str(self.bluetooth_adapter)])

        return cmd

    async def start(self) -> bool:
        """
        Start the Matter server subprocess.
        Returns True once the process is running.
        """
        if not self.is_available:
            logger.error(
                "python-matter-server not installed. "
                "Install with: pip install 'python-matter-server[server]' --break-system-packages"
            )
            return False

        if self._running:
            logger.warning("Matter server already running")
            return True

        # Ensure storage directory exists
        os.makedirs(self.storage_path, exist_ok=True)

        self._shutdown = False
        self._restart_count = 0

        return await self._spawn()

    async def _spawn(self) -> bool:
        """Spawn the subprocess."""
        cmd = self._build_command()
        logger.info(f"Starting Matter server: {' '.join(cmd)}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Don't let it inherit our signal handlers
                preexec_fn=os.setpgrp,
            )

            self._running = True
            logger.info(f"✅ Matter server started (PID {self._process.pid}) on port {self.port}")

            # Start log reader + health monitor
            self._monitor_task = asyncio.create_task(self._monitor())

            # Wait briefly and check it didn't crash immediately
            await asyncio.sleep(2)
            if self._process.returncode is not None:
                logger.error(f"Matter server exited immediately with code {self._process.returncode}")
                self._running = False
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to start Matter server subprocess: {e}")
            self._running = False
            return False

    async def _monitor(self):
        """Read subprocess output and restart on crash."""
        try:
            # Stream stdout/stderr to our logger
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    # Route to appropriate log level
                    if "ERROR" in text or "CRITICAL" in text:
                        logger.error(f"[matter-server] {text}")
                    elif "WARNING" in text:
                        logger.warning(f"[matter-server] {text}")
                    elif "DEBUG" in text:
                        logger.debug(f"[matter-server] {text}")
                    else:
                        logger.info(f"[matter-server] {text}")

            # Process ended
            if self._process:
                returncode = await self._process.wait()
                logger.warning(f"Matter server exited with code {returncode}")

            self._running = False

            # Auto-restart if not shutting down
            if not self._shutdown and self._restart_count < self._max_restarts:
                self._restart_count += 1
                delay = min(5 * self._restart_count, 30)
                logger.info(f"Restarting Matter server in {delay}s (attempt {self._restart_count}/{self._max_restarts})")
                await asyncio.sleep(delay)
                if not self._shutdown:
                    await self._spawn()
            elif not self._shutdown:
                logger.error(f"Matter server exceeded max restarts ({self._max_restarts}), giving up")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Matter server monitor error: {e}")
            self._running = False

    async def stop(self):
        """Stop the Matter server subprocess."""
        self._shutdown = True
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            logger.info(f"Stopping Matter server (PID {self._process.pid})...")
            try:
                # Send SIGTERM to the process group
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10)
                    logger.info("Matter server stopped gracefully")
                except asyncio.TimeoutError:
                    logger.warning("Matter server didn't stop gracefully, sending SIGKILL")
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    await self._process.wait()
            except ProcessLookupError:
                pass  # Already dead
            except Exception as e:
                logger.error(f"Error stopping Matter server: {e}")

        self._process = None
        logger.info("Matter server manager stopped")

    def get_status(self) -> dict:
        """Return status for API/UI."""
        return {
            "available": self.is_available,
            "running": self._running,
            "pid": self._process.pid if self._process and self._process.returncode is None else None,
            "port": self.port,
            "ws_url": self.ws_url,
            "storage_path": self.storage_path,
            "restart_count": self._restart_count,
        }