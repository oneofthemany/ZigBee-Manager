#!/usr/bin/env python3
"""
Dongle Jedi — Zigbee Coordinator Auto-Detection Module
=======================================================
Wraps the standalone Zigbee Serial Interrogator as an importable module
with async progress streaming for the web-based setup wizard.

Usage from main.py / API:
    from modules.dongle_jedi import DongleJedi

    jedi = DongleJedi()
    results = await jedi.scan_async(progress_callback=my_callback)
"""

import asyncio
import json
import logging
import os
import functools
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Awaitable, List

logger = logging.getLogger("modules.dongle_jedi")

# ---------------------------------------------------------------------------
# Import the interrogator classes from the standalone script.
# The script lives alongside this module (copied during deploy).
# We do a careful import to avoid the CLI entry-point executing.
# ---------------------------------------------------------------------------

try:
    import serial
    import serial.tools.list_ports
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False
    logger.warning("pyserial not installed — dongle detection unavailable")


# Forward-declare; populated by lazy import
_interrogator_module = None


def _ensure_interrogator():
    """Lazy-import the interrogator to avoid import-time serial probing."""
    global _interrogator_module
    if _interrogator_module is not None:
        return _interrogator_module

    import importlib.util
    # Look for dongle_jedi_core.py next to this file
    here = os.path.dirname(os.path.abspath(__file__))
    core_path = os.path.join(here, "dongle_jedi_core.py")

    if not os.path.exists(core_path):
        raise FileNotFoundError(
            f"dongle_jedi_core.py not found at {core_path}. "
            "Copy the standalone interrogator script there."
        )

    spec = importlib.util.spec_from_file_location("dongle_jedi_core", core_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _interrogator_module = mod
    return mod


# ---------------------------------------------------------------------------
# Progress event types
# ---------------------------------------------------------------------------

class ScanPhase:
    DISCOVERY = "discovery"         # USB enumeration
    PROBING = "probing"             # Serial protocol probing
    FLOW_VERIFY = "flow_verify"     # Flow control verification
    INTERROGATION = "interrogation" # Extracting adapter details
    COMPLETE = "complete"           # Scan finished


@dataclass
class ScanProgress:
    """Progress event sent to the frontend via WebSocket."""
    phase: str
    message: str
    port: str = ""
    progress_pct: int = 0           # 0-100
    detail: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "phase": self.phase,
            "message": self.message,
            "port": self.port,
            "progress_pct": self.progress_pct,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


# ---------------------------------------------------------------------------
# Quick USB enumeration (no serial probing — fast, safe for UI)
# ---------------------------------------------------------------------------

def list_serial_ports() -> list[dict]:
    """
    List all serial ports visible to pyserial.
    Returns lightweight dicts safe for JSON serialisation.
    This does NOT open any ports — it's pure USB enumeration.
    """
    if not PYSERIAL_AVAILABLE:
        return []

    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "port": p.device,
            "description": p.description or "",
            "manufacturer": p.manufacturer or "",
            "product": p.product or "",
            "vid": f"{p.vid:04X}" if p.vid else "",
            "pid": f"{p.pid:04X}" if p.pid else "",
            "serial_number": p.serial_number or "",
            "hwid": p.hwid or "",
        })
    return ports


# ---------------------------------------------------------------------------
# Main wrapper class
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[ScanProgress], Awaitable[None]]


class DongleJedi:
    """
    High-level async wrapper around ZigbeeInterrogator.

    Runs the blocking serial I/O in a thread pool and streams progress
    events back to the frontend via an async callback.
    """

    def __init__(self):
        self._last_results: list = []
        self._scanning = False

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    @property
    def last_results(self) -> list:
        return self._last_results

    # ------------------------------------------------------------------
    # Async scan entry point
    # ------------------------------------------------------------------

    async def scan_async(
            self,
            port: Optional[str] = None,
            progress_cb: Optional[ProgressCallback] = None,
    ) -> list[dict]:
        """
        Run a full dongle scan asynchronously.

        Args:
            port: Specific port to probe, or None for auto-discovery.
            progress_cb: Async callback receiving ScanProgress events.

        Returns:
            List of result dicts (same schema as interrogator.export_json()).
        """
        if self._scanning:
            raise RuntimeError("Scan already in progress")

        self._scanning = True
        self._last_results = []

        try:
            mod = _ensure_interrogator()
            loop = asyncio.get_running_loop()

            # We'll capture print output from the interrogator by monkey-patching
            # its print calls via a custom stdout, and also hook the progress.
            results = await loop.run_in_executor(
                None,
                functools.partial(
                    self._run_scan_blocking, mod, port, progress_cb, loop
                )
            )

            self._last_results = results

            if progress_cb:
                zigbee_results = [r for r in results if r.get("adapter_family") != "Non-Zigbee serial device"]
                await progress_cb(ScanProgress(
                    phase=ScanPhase.COMPLETE,
                    message=f"Scan complete — {len(zigbee_results)} Zigbee adapter(s) found",
                    progress_pct=100,
                    detail={"results": results, "zigbee_count": len(zigbee_results)},
                ))

            return results

        except Exception as e:
            logger.error(f"Dongle scan failed: {e}", exc_info=True)
            if progress_cb:
                await progress_cb(ScanProgress(
                    phase=ScanPhase.COMPLETE,
                    message=f"Scan failed: {e}",
                    progress_pct=100,
                    detail={"error": str(e)},
                ))
            return []
        finally:
            self._scanning = False

    # ------------------------------------------------------------------
    # Blocking scan (runs in executor thread)
    # ------------------------------------------------------------------

    def _run_scan_blocking(
            self,
            mod,
            port: Optional[str],
            progress_cb: Optional[ProgressCallback],
            loop: asyncio.AbstractEventLoop,
    ) -> list[dict]:
        """
        Blocking wrapper that runs the interrogator and posts progress
        events back to the async loop.
        """
        def post_progress(phase, message, port_name="", pct=0, detail=None):
            if progress_cb:
                evt = ScanProgress(
                    phase=phase, message=message,
                    port=port_name, progress_pct=pct, detail=detail
                )
                asyncio.run_coroutine_threadsafe(progress_cb(evt), loop)

        post_progress(ScanPhase.DISCOVERY, "Enumerating USB devices...", pct=5)

        interrogator = mod.ZigbeeInterrogator(verbose=False)

        # Phase 1: Discovery
        if port:
            candidates = [{"port": port, "vid": 0, "pid": 0, "description": "",
                           "likely_family": None, "priority": 3}]
            post_progress(
                ScanPhase.DISCOVERY,
                f"Manual port: {port}",
                port_name=port, pct=10
            )
        else:
            candidates = interrogator.discover_candidates()
            post_progress(
                ScanPhase.DISCOVERY,
                f"Found {len(candidates)} candidate serial port(s)",
                pct=15,
                detail={"candidates": [
                    {"port": c["port"], "description": c.get("description", ""),
                     "vid": c.get("vid", 0), "pid": c.get("pid", 0),
                     "likely_family": c.get("likely_family")}
                    for c in candidates
                ]},
            )

        if not candidates:
            post_progress(
                ScanPhase.COMPLETE,
                "No candidate serial ports found",
                pct=100,
                detail={"error": "no_ports"},
            )
            return []

        # Phase 2+3: Probe each candidate
        total = len(candidates)
        results = []

        for idx, cand in enumerate(candidates):
            port_name = cand["port"]
            base_pct = 15 + int((idx / total) * 80)

            post_progress(
                ScanPhase.PROBING,
                f"Probing {port_name}...",
                port_name=port_name,
                pct=base_pct,
                detail={
                    "candidate_index": idx + 1,
                    "candidate_total": total,
                    "vid": cand.get("vid", 0),
                    "pid": cand.get("pid", 0),
                    "likely_family": cand.get("likely_family"),
                },
            )

            try:
                info = interrogator.probe_port(port_name, candidate=cand)
                if info:
                    family = info.adapter_family.value
                    is_zigbee = info.adapter_family != mod.AdapterFamily.NOT_ZIGBEE

                    result_dict = {
                        "port": info.port,
                        "adapter_family": family,
                        "baud_rate": info.baud_rate,
                        "flow_control": info.flow_control.value,
                        "firmware_version": info.firmware_version,
                        "stack_version": info.stack_version,
                        "hardware_id": info.hardware_id,
                        "eui64": info.eui64,
                        "board_name": info.board_name,
                        "extra": info.extra,
                    }
                    results.append(result_dict)

                    if is_zigbee:
                        post_progress(
                            ScanPhase.INTERROGATION,
                            f"✅ {family} detected on {port_name}",
                            port_name=port_name,
                            pct=base_pct + 5,
                            detail=result_dict,
                        )
                    else:
                        post_progress(
                            ScanPhase.PROBING,
                            f"No Zigbee adapter on {port_name}",
                            port_name=port_name,
                            pct=base_pct + 5,
                        )
            except Exception as exc:
                logger.warning(f"Error probing {port_name}: {exc}")
                post_progress(
                    ScanPhase.PROBING,
                    f"Error probing {port_name}: {exc}",
                    port_name=port_name,
                    pct=base_pct + 5,
                )

        return results

    # ------------------------------------------------------------------
    # Apply detected config to config.yaml
    # ------------------------------------------------------------------

    @staticmethod
    def apply_config(
            result: dict,
            config_path: str = "./config/config.yaml",
    ) -> dict:
        """
        Write the detected adapter settings into config.yaml.

        Args:
            result: A single adapter result dict from the scan.
            config_path: Path to config.yaml.

        Returns:
            Updated zigbee config section.
        """
        import yaml

        if not os.path.exists(config_path):
            config = {}
        else:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}

        zigbee = config.setdefault("zigbee", {})

        # Map adapter family → radio_type for zigpy
        family_to_radio = {
            "Silicon Labs EZSP (Ember)": "ezsp",
            "Silicon Labs CPC Multi-PAN (RCP)": "auto",  # Must auto-detect to trigger MultiPAN stack
            "Texas Instruments Z-Stack": "znp",
            "Dresden Elektronik ConBee/RaspBee": "deconz",
        }

        family = result.get("adapter_family", "")
        radio_type = family_to_radio.get(family, "auto")

        # Update config
        zigbee["port"] = result["port"]
        zigbee["radio_type"] = radio_type

        # Set baud rate in the radio-specific section
        baud = result.get("baud_rate", 0)
        flow = result.get("flow_control", "none")

        if radio_type == "ezsp":
            ezsp = zigbee.setdefault("ezsp", {})
            if baud:
                ezsp["baudrate"] = baud
            ezsp["flow_control"] = "hardware" if flow == "rtscts" else "software" if flow == "xonxoff" else "none"
        elif radio_type == "znp":
            znp = zigbee.setdefault("znp", {})
            if baud:
                znp["baudrate"] = baud
        elif radio_type == "deconz":
            # deconz section for future use
            pass

        # Set coordinator_type for the app's internal routing
        coordinator_type_map = {
            "ezsp": "ember",
            "znp": "znp",
            "deconz": "deconz",
        }
        zigbee["coordinator_type"] = coordinator_type_map.get(radio_type, "ember")

        # Write back
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Config updated: port={result['port']}, radio={radio_type}, baud={baud}, flow={flow}")
        return zigbee


    @staticmethod
    def apply_integration_config(
            mode: str,
            mqtt_settings: dict = None,
            config_path: str = "./config/config.yaml",
    ) -> dict:
        """
        Write integration mode and MQTT settings to config.yaml.

        Args:
            mode: 'standalone' or 'homeassistant'
            mqtt_settings: MQTT broker config (only used for homeassistant mode)
            config_path: Path to config.yaml

        Returns:
            Updated mqtt config section.
        """
        import yaml

        if not os.path.exists(config_path):
            config = {}
        else:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}

        mqtt = config.setdefault("mqtt", {})

        if mode == "standalone":
            mqtt["enabled"] = False
            # Clear broker details so they don't cause confusion
            mqtt.pop("broker_host", None)
            mqtt.pop("broker_port", None)
            mqtt.pop("username", None)
            mqtt.pop("password", None)
            logger.info("Integration mode: standalone (MQTT disabled)")

        elif mode == "homeassistant":
            mqtt["enabled"] = True
            if mqtt_settings:
                if mqtt_settings.get("broker_host"):
                    mqtt["broker_host"] = mqtt_settings["broker_host"]
                if mqtt_settings.get("broker_port"):
                    mqtt["broker_port"] = int(mqtt_settings["broker_port"])
                if mqtt_settings.get("username"):
                    mqtt["username"] = mqtt_settings["username"]
                if mqtt_settings.get("password"):
                    mqtt["password"] = mqtt_settings["password"]
                if mqtt_settings.get("base_topic"):
                    mqtt["base_topic"] = mqtt_settings["base_topic"]
                if mqtt_settings.get("discovery_prefix"):
                    mqtt["discovery_prefix"] = mqtt_settings["discovery_prefix"]

            logger.info(
                f"Integration mode: Home Assistant "
                f"(MQTT → {mqtt.get('broker_host', '?')}:{mqtt.get('broker_port', 1883)})"
            )

        # Write back
        with open(config_path, "w") as f:
            config["setup_completed"] = True
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return mqtt

    # ------------------------------------------------------------------
    # Check if setup is needed
    # ------------------------------------------------------------------

    @staticmethod
    def needs_setup(config_path: str = "./config/config.yaml") -> dict:
        import yaml

        if not os.path.exists(config_path):
            return {
                "needs_setup": True,
                "reason": "no_config",
                "current_port": "",
            }

        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}

        # Primary gate: setup wizard writes this flag on completion
        if not config.get("setup_completed", False):
            return {
                "needs_setup": True,
                "reason": "setup_not_completed",
                "current_port": config.get("zigbee", {}).get("port", ""),
            }

        # Don't let setup be considered complete until an admin user exists
        try:
            from modules.auth import get_auth_manager
            auth = get_auth_manager()
            if auth is not None:
                has_admin = any(
                    (not u.disabled) and ("admins" in u.groups or "admin" in u.extra_scopes)
                    for u in auth.users.values()
                )
                if not has_admin:
                    return {
                        "needs_setup": True,
                        "reason": "no_admin_user",
                        "current_port": config.get("zigbee", {}).get("port", ""),
                    }
        except Exception:
            pass

        # Secondary: port must still exist
        zigbee = config.get("zigbee", {})
        port = zigbee.get("port", "")

        if not port:
            return {
                "needs_setup": True,
                "reason": "no_port_configured",
                "current_port": "",
            }

        if not port.startswith("socket://") and not os.path.exists(port):
            return {
                "needs_setup": True,
                "reason": "port_missing",
                "current_port": port,
            }

        return {
            "needs_setup": False,
            "reason": "configured",
            "current_port": port,
        }