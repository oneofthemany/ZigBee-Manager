"""
System Monitor - Background Hardware Telemetry Collector
=========================================================
Collects CPU, memory, temperature, disk, and process metrics
at configurable intervals and writes to DuckDB via telemetry_db.

Also provides threshold-based alerting via WebSocket toast notifications.

Alert thresholds (configurable):
  cpu_percent:   > 90% sustained 3 samples
  mem_percent:   > 85%
  cpu_temp:      > 80°C
  disk_percent:  > 90%
  swap_percent:  > 50%
"""

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.system_monitor")

# Default collection interval (seconds)
DEFAULT_INTERVAL = 30

# Default alert thresholds
DEFAULT_THRESHOLDS = {
    "cpu_percent":  {"warn": 80, "critical": 95, "sustain": 3},
    "mem_percent":  {"warn": 80, "critical": 90, "sustain": 1},
    "cpu_temp":     {"warn": 75, "critical": 85, "sustain": 2},
    "gpu_temp":     {"warn": 80, "critical": 90, "sustain": 2},
    "disk_percent": {"warn": 85, "critical": 95, "sustain": 1},
    "swap_percent": {"warn": 40, "critical": 70, "sustain": 1},
}

# Alert cooldown — don't re-alert for same metric within this window
ALERT_COOLDOWN = 300  # 5 minutes


def _read_cpu_temp() -> Optional[float]:
    """Read CPU temperature from thermal zones (Linux)."""
    # Try common thermal zone paths
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",  # Most ARM SBCs
        "/sys/class/thermal/thermal_zone1/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",     # x86
    ]
    for path in paths:
        try:
            with open(path) as f:
                return int(f.read().strip()) / 1000.0
        except (FileNotFoundError, ValueError, PermissionError):
            continue
    return None


def _read_gpu_temp() -> Optional[float]:
    """Read GPU temperature if available."""
    # RK3588 Mali GPU
    paths = [
        "/sys/class/thermal/thermal_zone1/temp",
        "/sys/class/thermal/thermal_zone2/temp",
        "/sys/devices/virtual/thermal/thermal_zone1/temp",
    ]
    for path in paths:
        try:
            with open(path) as f:
                val = int(f.read().strip()) / 1000.0
                if val > 0:
                    return val
        except (FileNotFoundError, ValueError, PermissionError):
            continue

    # NVIDIA GPU
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    return None


def _read_system_uptime() -> int:
    """Read system uptime in seconds."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


def _get_top_processes(sort_key: str, limit: int = 5) -> List[Dict]:
    """
    Get top processes by a given metric.
    sort_key: 'swap', 'rss', 'cpu'
    """
    procs = []
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        try:
            with open(f"/proc/{pid}/status") as f:
                status = {}
                for line in f:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        status[parts[0].strip()] = parts[1].strip()

            name = status.get("Name", "?")
            vm_swap = int(status.get("VmSwap", "0 kB").split()[0])
            vm_rss = int(status.get("VmRSS", "0 kB").split()[0])

            procs.append({
                "pid": pid,
                "name": name,
                "swap_kb": vm_swap,
                "rss_kb": vm_rss,
            })
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            continue

    if sort_key == "swap":
        procs.sort(key=lambda p: p["swap_kb"], reverse=True)
    elif sort_key == "rss":
        procs.sort(key=lambda p: p["rss_kb"], reverse=True)

    return procs[:limit]


def _format_kb(kb: int) -> str:
    """Format KB to human-readable."""
    if kb >= 1048576:
        return f"{kb / 1048576:.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.0f} MB"
    return f"{kb} KB"


def _collect_diagnostics(metric: str, latest_metrics: Dict) -> Dict[str, Any]:
    """
    Collect diagnostic context for a specific metric alert.
    Returns: { "top_consumers": [...], "cause": "...", "fixes": [...] }
    """
    diag: Dict[str, Any] = {"top_consumers": [], "cause": "", "fixes": []}

    try:
        if metric == "swap_percent":
            top = _get_top_processes("swap", 5)
            top = [p for p in top if p["swap_kb"] > 0]
            diag["top_consumers"] = [
                {"name": f"{p['name']} (PID {p['pid']})", "value": _format_kb(p["swap_kb"])}
                for p in top
            ]

            # Detect common patterns
            matter_count = sum(1 for p in top if "python" in p["name"].lower() or "matter" in p["name"].lower())
            biggest = top[0] if top else None

            if biggest and biggest["swap_kb"] > 500000:
                diag["cause"] = f"'{biggest['name']}' is consuming {_format_kb(biggest['swap_kb'])} of swap"
            if matter_count > 2:
                diag["cause"] = f"Multiple matter-server processes detected ({matter_count}) — likely orphaned instances from restarts"

            diag["fixes"] = [
                "Check for orphaned processes: ps aux | grep matter_server",
                "Kill orphans: sudo pkill -9 -f 'matter_server.server'",
                "Restart service cleanly: sudo systemctl restart zigbee-matter-manager",
                "Reduce swap pressure: free pagecache with 'echo 3 | sudo tee /proc/sys/vm/drop_caches'",
                "If persistent, increase RAM or reduce loaded services",
            ]

        elif metric == "mem_percent":
            top = _get_top_processes("rss", 5)
            diag["top_consumers"] = [
                {"name": f"{p['name']} (PID {p['pid']})", "value": _format_kb(p["rss_kb"])}
                for p in top
            ]

            biggest = top[0] if top else None
            if biggest and biggest["rss_kb"] > 1048576:
                diag["cause"] = f"'{biggest['name']}' is using {_format_kb(biggest['rss_kb'])} of RAM"

            diag["fixes"] = [
                "Check for memory leaks: watch 'ps aux --sort=-rss | head -10'",
                "Restart the service to release accumulated memory",
                "Reduce Ollama container memory limit if AI is not actively in use",
                "Consider adding swap space if RAM is genuinely insufficient",
            ]

        elif metric == "cpu_percent":
            load = latest_metrics.get("load_1m", 0)
            cores = os.cpu_count() or 1
            diag["cause"] = f"Load average {load:.2f} across {cores} cores ({load/cores*100:.0f}% utilisation)"

            # Read top CPU from /proc/stat is complex; use load as proxy
            diag["fixes"] = [
                f"Current load: {load:.2f} (1m), target: < {cores}.0 for healthy operation",
                "Check top consumers: top -bn1 | head -15",
                "If Ollama is active, inference is CPU-intensive — this is expected during rule generation",
                "Reduce background scan intervals (spectrum, topology) in config.yaml",
                "If sustained, consider reducing Ollama --cpus limit",
            ]

        elif metric in ("cpu_temp", "gpu_temp"):
            temp = latest_metrics.get(metric, 0)
            # Check for thermal throttling
            throttled = False
            try:
                with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
                    cur_freq = int(f.read().strip())
                with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq") as f:
                    max_freq = int(f.read().strip())
                if cur_freq < max_freq * 0.8:
                    throttled = True
                    diag["cause"] = f"CPU is thermal throttling ({cur_freq/1000:.0f} MHz / {max_freq/1000:.0f} MHz max)"
            except Exception:
                pass

            if not throttled:
                diag["cause"] = f"Temperature elevated at {temp:.0f}°C but not yet throttling"

            diag["fixes"] = [
                "Ensure adequate airflow around the board",
                "Check if a heatsink is properly attached",
                "Consider adding an active fan (PWM or always-on)",
                "Reduce CPU-intensive tasks: lower Ollama --cpus, increase scan intervals",
                "If in an enclosure, ensure ventilation holes are not blocked",
            ]

        elif metric == "disk_percent":
            # Find large directories
            large_dirs = []
            check_paths = [
                ("Logs", "/opt/zigbee_manager/logs"),
                ("Telemetry DB", "/opt/zigbee_manager/data"),
                ("Backups", "/opt/zigbee_manager/backups"),
                ("Ollama models", "/root/.ollama"),
                ("Matter data", "/opt/zigbee_manager/data/matter"),
            ]
            for label, path in check_paths:
                try:
                    total = 0
                    for dirpath, _, filenames in os.walk(path):
                        for f in filenames:
                            try:
                                total += os.path.getsize(os.path.join(dirpath, f))
                            except OSError:
                                pass
                    if total > 10 * 1024 * 1024:  # > 10MB
                        large_dirs.append({"name": label, "value": _format_kb(total // 1024)})
                except (PermissionError, FileNotFoundError):
                    pass

            diag["top_consumers"] = sorted(large_dirs, key=lambda x: x["value"], reverse=True)[:5]
            diag["cause"] = "Disk space is running low"

            diag["fixes"] = [
                "Prune telemetry data: curl -X POST http://localhost:8000/api/telemetry/db/prune",
                "Rotate logs: sudo logrotate -f /etc/logrotate.d/zigbee-matter-manager",
                "Clean old backups: ls -la /opt/zigbee_manager/backups/",
                "Remove unused Ollama models: podman exec ollama ollama rm <model>",
                "Check journal size: journalctl --disk-usage",
            ]

    except Exception as e:
        diag["cause"] = f"Diagnostic collection error: {e}"

    return diag


def collect_metrics() -> Dict[str, Any]:
    """
    Collect a single snapshot of system metrics.
    Uses /proc directly where possible to avoid psutil dependency.
    """
    metrics = {}

    # ── CPU ──
    try:
        # Load average
        load1, load5, load15 = os.getloadavg()
        metrics["load_1m"] = round(load1, 2)
        metrics["load_5m"] = round(load5, 2)
        metrics["load_15m"] = round(load15, 2)

        # CPU percent from /proc/stat (instantaneous isn't meaningful,
        # so we use load_1m / nproc as a proxy)
        nproc = os.cpu_count() or 1
        metrics["cpu_percent"] = round(min(load1 / nproc * 100, 100), 1)

        # CPU frequency
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
                metrics["cpu_freq"] = round(int(f.read().strip()) / 1000, 0)  # MHz
        except (FileNotFoundError, ValueError):
            metrics["cpu_freq"] = None
    except Exception:
        metrics["cpu_percent"] = None
        metrics["load_1m"] = None
        metrics["load_5m"] = None
        metrics["load_15m"] = None

    # ── Memory ──
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = int(parts[1].strip().split()[0]) * 1024  # Convert KB to bytes
                    meminfo[key] = val

        mem_total = meminfo.get("MemTotal", 0)
        mem_avail = meminfo.get("MemAvailable", 0)
        mem_used = mem_total - mem_avail
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free = meminfo.get("SwapFree", 0)
        swap_used = swap_total - swap_free

        metrics["mem_total"] = mem_total
        metrics["mem_used"] = mem_used
        metrics["mem_percent"] = round(mem_used / max(mem_total, 1) * 100, 1)
        metrics["swap_used"] = swap_used
        metrics["swap_percent"] = round(swap_used / max(swap_total, 1) * 100, 1) if swap_total > 0 else 0
    except Exception:
        pass

    # ── Disk ──
    try:
        stat = os.statvfs("/opt/zigbee_manager" if os.path.isdir("/opt/zigbee_manager") else "/")
        disk_total = stat.f_blocks * stat.f_frsize
        disk_free = stat.f_bfree * stat.f_frsize
        disk_used = disk_total - disk_free
        metrics["disk_total"] = disk_total
        metrics["disk_used"] = disk_used
        metrics["disk_percent"] = round(disk_used / max(disk_total, 1) * 100, 1)
    except Exception:
        pass

    # ── Temperatures ──
    metrics["cpu_temp"] = _read_cpu_temp()
    metrics["gpu_temp"] = _read_gpu_temp()

    # ── System uptime ──
    metrics["uptime_secs"] = _read_system_uptime()

    # ── Process stats ──
    try:
        pid = os.getpid()
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    metrics["process_rss"] = int(line.split()[1]) * 1024  # KB to bytes
                elif line.startswith("Threads:"):
                    metrics["process_threads"] = int(line.split()[1])
    except Exception:
        pass

    return metrics


class SystemMonitor:
    """
    Background task that collects system metrics and writes to DuckDB.
    Fires WebSocket alerts when thresholds are breached.
    """

    def __init__(self, interval: int = DEFAULT_INTERVAL,
                 event_callback: Optional[Callable] = None,
                 thresholds: Optional[Dict] = None):
        self.interval = interval
        self._event_callback = event_callback
        self._thresholds = thresholds or DEFAULT_THRESHOLDS
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Alert state tracking
        self._alert_counters: Dict[str, int] = {}      # metric → consecutive breach count
        self._last_alert_time: Dict[str, float] = {}    # metric → last alert timestamp
        self._active_alerts: Dict[str, str] = {}        # metric → current severity

        # Latest snapshot (for API)
        self.latest: Dict[str, Any] = {}

    def start(self):
        """Start the background collection loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info(f"System monitor started (interval={self.interval}s)")

    def stop(self):
        """Stop the background collection loop."""
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        """Main collection loop."""
        # Initial delay — let the app fully start
        await asyncio.sleep(10)

        while self._running:
            try:
                metrics = await asyncio.get_event_loop().run_in_executor(
                    None, collect_metrics
                )
                self.latest = metrics

                # Write to DuckDB
                try:
                    from modules.telemetry_db import write_system_metrics
                    write_system_metrics(metrics)
                except Exception as e:
                    logger.debug(f"Telemetry write failed: {e}")

                # Check thresholds
                await self._check_thresholds(metrics)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"System monitor error: {e}")

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def _check_thresholds(self, metrics: Dict[str, Any]):
        """Evaluate metrics against thresholds and fire alerts."""
        now = time.time()

        for metric_name, thresholds in self._thresholds.items():
            value = metrics.get(metric_name)
            if value is None:
                self._alert_counters.pop(metric_name, None)
                continue

            warn_threshold = thresholds.get("warn", float("inf"))
            critical_threshold = thresholds.get("critical", float("inf"))
            sustain = thresholds.get("sustain", 1)

            severity = None
            if value >= critical_threshold:
                severity = "critical"
            elif value >= warn_threshold:
                severity = "warning"

            if severity:
                self._alert_counters[metric_name] = self._alert_counters.get(metric_name, 0) + 1

                if self._alert_counters[metric_name] >= sustain:
                    last_alert = self._last_alert_time.get(metric_name, 0)
                    if now - last_alert > ALERT_COOLDOWN or severity != self._active_alerts.get(metric_name):
                        await self._fire_alert(metric_name, value, severity, thresholds)
                        self._last_alert_time[metric_name] = now
                        self._active_alerts[metric_name] = severity
            else:
                # Value is within normal range
                if metric_name in self._active_alerts:
                    # Was alerting, now recovered
                    await self._fire_recovery(metric_name, value)
                    del self._active_alerts[metric_name]
                self._alert_counters.pop(metric_name, None)

    async def _fire_alert(self, metric: str, value: float, severity: str,
                          thresholds: Dict):
        """Send alert via WebSocket with diagnostic context and fix suggestions."""
        labels = {
            "cpu_percent": "CPU usage",
            "mem_percent": "Memory usage",
            "cpu_temp": "CPU temperature",
            "gpu_temp": "GPU temperature",
            "disk_percent": "Disk usage",
            "swap_percent": "Swap usage",
        }
        units = {
            "cpu_percent": "%", "mem_percent": "%", "cpu_temp": "°C",
            "gpu_temp": "°C", "disk_percent": "%", "swap_percent": "%",
        }
        label = labels.get(metric, metric)
        unit = units.get(metric, "")
        threshold = thresholds.get("critical" if severity == "critical" else "warn", "?")

        message = f"{label} at {value:.1f}{unit} (threshold: {threshold}{unit})"
        logger.warning(f"System alert [{severity}]: {message}")

        # Collect diagnostics in executor (reads /proc, may be slow)
        diagnostics = {}
        try:
            diagnostics = await asyncio.get_event_loop().run_in_executor(
                None, _collect_diagnostics, metric, self.latest
            )
        except Exception as e:
            logger.debug(f"Diagnostic collection failed: {e}")

        if self._event_callback:
            try:
                await self._event_callback("system_alert", {
                    "severity": severity,
                    "metric": metric,
                    "value": round(value, 1),
                    "threshold": threshold,
                    "message": message,
                    "diagnostics": diagnostics,
                })
            except Exception as e:
                logger.debug(f"Alert callback failed: {e}")

    async def _fire_recovery(self, metric: str, value: float):
        """Send recovery notification."""
        labels = {
            "cpu_percent": "CPU usage", "mem_percent": "Memory usage",
            "cpu_temp": "CPU temperature", "gpu_temp": "GPU temperature",
            "disk_percent": "Disk usage", "swap_percent": "Swap usage",
        }
        label = labels.get(metric, metric)
        logger.info(f"System recovered: {label} back to normal ({value:.1f})")

        if self._event_callback:
            try:
                await self._event_callback("system_alert_clear", {
                    "metric": metric,
                    "value": round(value, 1),
                    "message": f"{label} returned to normal",
                })
            except Exception as e:
                logger.debug(f"Recovery callback failed: {e}")

    def get_current(self) -> Dict[str, Any]:
        """Get the latest metrics snapshot."""
        return {**self.latest, "active_alerts": dict(self._active_alerts)}

    def get_thresholds(self) -> Dict:
        """Get current alert thresholds."""
        return dict(self._thresholds)

    def update_thresholds(self, updates: Dict):
        """Update alert thresholds."""
        for metric, values in updates.items():
            if metric in self._thresholds:
                self._thresholds[metric].update(values)