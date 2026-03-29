"""
PTY ↔ TCP Bridge
=================
In-process asyncio replacement for socat in the MultiPAN stack.

Replaces:
    socat PTY,link=/tmp/ttyZigbeeNCP,raw,echo=0,mode=660 \
          TCP-LISTEN:9999,reuseaddr,fork

Creates a PTY pair, symlinks the slave to the configured path
(so zigbeed can open it), and listens on a TCP port (so bellows
can connect). Data is relayed bidirectionally.

Advantages over socat:
  - No external binary dependency
  - Per-frame logging and byte counters for diagnostics
  - Graceful lifecycle tied to MultiPanManager
  - Future: frame inspection for TDM scheduling hints

Latency: TCP relay adds ~50-100µs per hop via asyncio —
well within bellows' 500ms+ ASH retransmit timers.
"""
import asyncio
import logging
import os
import pty
import tty
import termios
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger("multipan.bridge")


class PTYTCPBridge:
    """
    Bridges a PTY (for zigbeed) to a TCP listener (for bellows).

    Lifecycle:
        bridge = PTYTCPBridge(pty_path="/tmp/ttyZigbeeNCP", tcp_port=9999)
        await bridge.start()
        ...
        await bridge.stop()
    """

    def __init__(
            self,
            pty_path: str = "/tmp/ttyZigbeeNCP",
            tcp_port: int = 9999,
            tcp_host: str = "127.0.0.1",
    ):
        self.pty_path = pty_path
        self.tcp_port = tcp_port
        self.tcp_host = tcp_host

        # PTY file descriptors
        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None

        # TCP server
        self._server: Optional[asyncio.AbstractServer] = None
        self._tcp_writer: Optional[asyncio.StreamWriter] = None
        self._tcp_lock = asyncio.Lock()

        # Tasks
        self._pty_reader_task: Optional[asyncio.Task] = None
        self._running = False

        # Metrics
        self.bytes_pty_to_tcp = 0
        self.bytes_tcp_to_pty = 0
        self.frames_pty_to_tcp = 0
        self.frames_tcp_to_pty = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> bool:
        """Create PTY, start TCP listener, begin relay."""
        try:
            # ── Create PTY pair ──
            self._master_fd, self._slave_fd = pty.openpty()

            # Set master to raw mode (no echo, no line buffering)
            tty.setraw(self._master_fd)

            # Get slave device path and symlink to configured path
            slave_name = os.ttyname(self._slave_fd)

            # Remove stale symlink
            link = Path(self.pty_path)
            if link.exists() or link.is_symlink():
                link.unlink()

            os.symlink(slave_name, self.pty_path)
            os.chmod(self.pty_path, stat.S_IRUSR | stat.S_IWUSR |
                     stat.S_IRGRP | stat.S_IWGRP)  # 0o660

            logger.info(
                f"PTY created: {slave_name} → {self.pty_path} "
                f"(master fd={self._master_fd})"
            )

            # ── Start TCP listener ──
            self._server = await asyncio.start_server(
                self._handle_tcp_client,
                self.tcp_host,
                self.tcp_port,
                reuse_address=True,
            )
            logger.info(f"TCP listener on {self.tcp_host}:{self.tcp_port}")

            # ── Start PTY→TCP reader ──
            self._running = True
            self._pty_reader_task = asyncio.create_task(
                self._pty_read_loop(),
                name="pty-bridge-reader",
            )

            return True

        except Exception as e:
            logger.error(f"PTY-TCP bridge failed to start: {e}")
            await self.stop()
            return False

    async def stop(self):
        """Tear down bridge: close PTY, TCP server, all tasks."""
        self._running = False

        # Cancel PTY reader
        if self._pty_reader_task and not self._pty_reader_task.done():
            self._pty_reader_task.cancel()
            try:
                await self._pty_reader_task
            except asyncio.CancelledError:
                pass

        # Close TCP client
        async with self._tcp_lock:
            if self._tcp_writer:
                try:
                    self._tcp_writer.close()
                    await self._tcp_writer.wait_closed()
                except Exception:
                    pass
                self._tcp_writer = None

        # Close TCP server
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Close PTY fds
        for fd in (self._master_fd, self._slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master_fd = None
        self._slave_fd = None

        # Clean up symlink
        link = Path(self.pty_path)
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass

        logger.info(
            f"PTY-TCP bridge stopped "
            f"(PTY→TCP: {self.bytes_pty_to_tcp} bytes/{self.frames_pty_to_tcp} frames, "
            f"TCP→PTY: {self.bytes_tcp_to_pty} bytes/{self.frames_tcp_to_pty} frames)"
        )

    async def _handle_tcp_client(
            self,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
    ):
        """Handle a new TCP connection from bellows."""
        peer = writer.get_extra_info("peername")
        logger.info(f"TCP client connected: {peer}")

        # Only one client at a time (bellows)
        async with self._tcp_lock:
            if self._tcp_writer:
                try:
                    self._tcp_writer.close()
                    await self._tcp_writer.wait_closed()
                except Exception:
                    pass
            self._tcp_writer = writer

        # TCP→PTY relay loop
        try:
            while self._running:
                data = await reader.read(4096)
                if not data:
                    break

                if self._master_fd is not None:
                    os.write(self._master_fd, data)
                    self.bytes_tcp_to_pty += len(data)
                    self.frames_tcp_to_pty += 1

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            f"TCP→PTY: {len(data)} bytes "
                            f"[{data[:16].hex()}{'...' if len(data) > 16 else ''}]"
                        )

        except (ConnectionResetError, BrokenPipeError):
            logger.info(f"TCP client disconnected: {peer}")
        except Exception as e:
            logger.warning(f"TCP→PTY relay error: {e}")
        finally:
            async with self._tcp_lock:
                if self._tcp_writer is writer:
                    self._tcp_writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"TCP client closed: {peer}")

    async def _pty_read_loop(self):
        """Read from PTY master fd and forward to TCP client."""
        loop = asyncio.get_running_loop()

        try:
            while self._running and self._master_fd is not None:
                try:
                    # Use executor for blocking os.read — PTY reads block
                    data = await loop.run_in_executor(
                        None, self._pty_read_blocking
                    )
                except OSError as e:
                    if not self._running:
                        break
                    logger.warning(f"PTY read error: {e}")
                    await asyncio.sleep(0.1)
                    continue

                if not data:
                    continue

                self.bytes_pty_to_tcp += len(data)
                self.frames_pty_to_tcp += 1

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        f"PTY→TCP: {len(data)} bytes "
                        f"[{data[:16].hex()}{'...' if len(data) > 16 else ''}]"
                    )

                async with self._tcp_lock:
                    if self._tcp_writer and not self._tcp_writer.is_closing():
                        try:
                            self._tcp_writer.write(data)
                            await self._tcp_writer.drain()
                        except (ConnectionResetError, BrokenPipeError):
                            logger.warning("PTY→TCP: client disconnected during write")
                            self._tcp_writer = None
                        except Exception as e:
                            logger.warning(f"PTY→TCP write error: {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"PTY read loop error: {e}")

    def _pty_read_blocking(self) -> bytes:
        """Blocking read from PTY master — runs in executor thread."""
        if self._master_fd is None:
            return b""
        try:
            return os.read(self._master_fd, 4096)
        except OSError:
            if not self._running:
                return b""
            raise

    def get_status(self) -> dict:
        """Status for API/UI."""
        return {
            "name": "pty-bridge",
            "running": self._running,
            "pid": None,  # In-process, no PID
            "restart_count": 0,
            "pty_path": self.pty_path,
            "tcp_port": self.tcp_port,
            "tcp_client_connected": self._tcp_writer is not None,
            "bytes_pty_to_tcp": self.bytes_pty_to_tcp,
            "bytes_tcp_to_pty": self.bytes_tcp_to_pty,
            "frames_pty_to_tcp": self.frames_pty_to_tcp,
            "frames_tcp_to_pty": self.frames_tcp_to_pty,
        }