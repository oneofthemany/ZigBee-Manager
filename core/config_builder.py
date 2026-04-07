"""
Radio configuration builder mixin.
Builds bellows (EZSP), zigpy-znp (ZNP) and zigpy-deconz config dicts
from config.yaml settings with dynamic baud-rate / flow-control detection.

Auto-detection uses the Dongle Jedi serial interrogator — it sends actual
protocol frames (EZSP ASH reset, ZNP SYS_PING, deCONZ read firmware)
and correctly identifies adapter family, baud rate, and flow control.

This replaces the broken ControllerApplication.probe() approach which
has timeout/port-locking issues across different bellows versions.
"""
import asyncio
import logging
import time
from modules.ota import build_ota_config

logger = logging.getLogger("core.config")

# ── Adapter family → radio_type mapping ─────────────────────────────
# Must match the families returned by dongle_jedi_core.AdapterFamily
FAMILY_TO_RADIO = {
    "Silicon Labs EZSP (Ember)": "EZSP",
    "Silicon Labs CPC Multi-PAN (RCP)": "EZSP",
    "Texas Instruments Z-Stack": "ZNP",
    "Dresden Elektronik ConBee/RaspBee": "DECONZ",
}

FAMILY_TO_FLOW_KEY = {
    "rtscts": "hardware",
    "xonxoff": "software",
    "none": None,
    "": None,
}


class ConfigBuilderMixin:
    """Methods for building radio-specific configuration and probing radio type."""

    def _is_socket_path(self) -> bool:
        """Check if port is a TCP socket URI (MultiPAN zigbeed mode)."""
        return isinstance(self.port, str) and self.port.startswith("socket://")

    # =====================================================================
    # RADIO PROBING — uses Dongle Jedi serial interrogator
    # =====================================================================

    async def _probe_radio_type(self, progress_cb=None) -> dict:

        """
        Detect radio type AND working serial parameters.

        Priority:
          1. Explicit radio_type in config.yaml → skip probing
          2. Socket path → always EZSP
          3. Auto → Dongle Jedi scans the serial port

        Returns dict with radio_type, baudrate, flow_control.
        """
        configured = self._config.get('radio_type', 'auto')

        # ── Explicit radio type → skip probing ──
        if configured and configured.upper() in ('EZSP', 'ZNP', 'DECONZ'):
            return self._explicit_radio_result(configured.upper())

        # ── Socket paths are always EZSP (zigbeed / MultiPAN) ──
        if self._is_socket_path():
            return {"radio_type": "EZSP", "baudrate": 0, "flow_control": "none"}

        # ── Auto-detect using Dongle Jedi ──
        return await self._probe_with_jedi(progress_cb=progress_cb)

    def _explicit_radio_result(self, radio_type: str) -> dict:
        """Build probe result for explicitly configured radio type."""
        section_map = {"EZSP": "ezsp", "ZNP": "znp", "DECONZ": "deconz"}
        defaults_map = {
            "EZSP":   {"baudrate": 115200, "flow_control": "software"},
            "ZNP":    {"baudrate": 115200, "flow_control": None},
            "DECONZ": {"baudrate": 38400,  "flow_control": None},
        }
        section = self._config.get(section_map[radio_type], {})
        defaults = defaults_map[radio_type]

        raw_baud = section.get('baudrate')
        raw_flow = section.get('flow_control')
        is_auto_baud = raw_baud is None or str(raw_baud).lower() == 'auto'
        is_auto_flow = raw_flow is None or str(raw_flow).lower() == 'auto'

        return {
            "radio_type": radio_type,
            "baudrate": int(defaults["baudrate"]) if is_auto_baud else int(raw_baud),
            "flow_control": defaults["flow_control"] if is_auto_flow else raw_flow,
        }

    async def _probe_with_jedi(self, progress_cb=None) -> dict:
        """
        Use Dongle Jedi to probe the serial port.

        DongleJedi runs the blocking serial interrogator in a thread pool.
        It sends actual EZSP/ZNP/deCONZ protocol frames and returns the
        adapter family, working baud rate, and flow control.
        """
        logger.info(f"Auto-detecting radio on {self.port} using Dongle Jedi...")
        t0 = time.monotonic()

        try:
            from modules.dongle_jedi import DongleJedi

            jedi = DongleJedi()

            # Progress callback: log + optional external broadcast
            async def log_progress(progress):
                logger.info(f"  [Jedi] {progress.message}")
                if progress_cb:
                    try:
                        await progress_cb(progress)
                    except Exception:
                        pass  # Don't let broadcast errors kill the probe

            results = await jedi.scan_async(
                port=self.port,
                progress_cb=log_progress,
            )

            elapsed = time.monotonic() - t0

            # Filter to Zigbee adapters only
            zigbee_results = [
                r for r in results
                if r.get("adapter_family", "") in FAMILY_TO_RADIO
            ]

            if not zigbee_results:
                logger.warning(
                    f"Dongle Jedi found no Zigbee adapter on {self.port} "
                    f"({elapsed:.1f}s). Results: {results}"
                )
                raise RuntimeError(
                    f"No Zigbee adapter detected on {self.port}. "
                    f"Dongle Jedi scanned but found no compatible radio."
                )

            # Use the first (highest priority) result
            best = zigbee_results[0]
            family = best.get("adapter_family", "")
            radio_type = FAMILY_TO_RADIO.get(family, "EZSP")
            baud = int(best.get("baud_rate", 0)) or 115200
            raw_flow = best.get("flow_control", "none")
            flow = FAMILY_TO_FLOW_KEY.get(raw_flow, raw_flow)

            logger.info(
                f"✅ Dongle Jedi detected: {family} on {self.port} "
                f"@ {baud} baud / {flow} flow control ({elapsed:.1f}s)"
            )

            # Also log extra details if available
            if best.get("firmware_version"):
                logger.info(f"  Firmware: {best['firmware_version']}")
            if best.get("eui64"):
                logger.info(f"  EUI64: {best['eui64']}")
            if best.get("board_name"):
                logger.info(f"  Board: {best['board_name']}")

            return {
                "radio_type": radio_type,
                "baudrate": baud,
                "flow_control": flow,
                "adapter_family": family,
            }

        except ImportError:
            logger.error(
                "Dongle Jedi not available (dongle_jedi module or "
                "dongle_jedi_core.py missing). Cannot auto-detect radio."
            )
            raise RuntimeError(
                f"Cannot auto-detect radio on {self.port}: "
                f"Dongle Jedi module not available. "
                f"Set radio_type explicitly in config.yaml."
            )
        except RuntimeError:
            raise  # Re-raise our own RuntimeErrors
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error(f"Dongle Jedi scan failed ({elapsed:.1f}s): {e}")
            raise RuntimeError(
                f"Auto-detection failed on {self.port}: {e}. "
                f"Set radio_type explicitly in config.yaml."
            )

    # =====================================================================
    # BAUD / FLOW RESOLUTION
    # =====================================================================

    def _resolve_serial_params(
            self, section_key: str, detected: dict | None,
            default_baud: int, default_flow: str,
    ) -> tuple:
        """
        Resolve final baudrate and flow_control.

        Priority:
          1. Explicit config.yaml value (unless 'auto')
          2. Probe-detected value
          3. Hardcoded default

        Returns (baudrate: int, flow_control: str)
        """
        settings = self._config.get(section_key, {})

        # Detected values from probe — must be int
        if detected and isinstance(detected.get("baudrate"), int) and detected["baudrate"] > 0:
            det_baud = detected["baudrate"]
            det_flow = detected.get("flow_control", default_flow)
        else:
            det_baud = default_baud
            det_flow = default_flow

        # Config values
        cfg_baud = settings.get('baudrate')
        cfg_flow = settings.get('flow_control')

        # 'auto' / None → use detected
        baud_is_auto = cfg_baud is None or str(cfg_baud).lower() == 'auto'
        flow_is_auto = cfg_flow is None or str(cfg_flow).lower() == 'auto'

        # Always ensure baudrate is int
        final_baud = int(det_baud if baud_is_auto else cfg_baud)
        final_flow = det_flow if flow_is_auto else cfg_flow

        # Warn on mismatches
        if not baud_is_auto and final_baud != det_baud:
            logger.warning(
                f"Config {section_key}.baudrate ({final_baud}) differs from "
                f"detected ({det_baud}) — using config value. "
                f"Set {section_key}.baudrate to 'auto' if connection fails."
            )
        if not flow_is_auto and final_flow != det_flow:
            logger.warning(
                f"Config {section_key}.flow_control ({final_flow}) differs from "
                f"detected ({det_flow}) — using config value. "
                f"Set {section_key}.flow_control to 'auto' if connection fails."
            )

        logger.info(
            f"{section_key.upper()} serial: "
            f"baudrate={final_baud}, flow_control={final_flow}"
        )
        if final_flow == "none":
            final_flow = None

        return final_baud, final_flow

    # =====================================================================
    # CONFIG BUILDERS
    # =====================================================================

    def _build_ezsp_config(self, ezsp_conf: dict, network_key,
                           detected: dict = None) -> dict:
        """Build EZSP config from zigbee.ezsp section + probe results."""
        if self._is_socket_path():
            device_conf = {"path": self.port}
            logger.info(f"Using socket connection: {self.port}")
        else:
            baud, flow = self._resolve_serial_params(
                "ezsp", detected,
                default_baud=115200, default_flow="software",
            )
            device_conf = {
                "path": self.port,
                "baudrate": baud,
                "flow_control": flow,
            }

        ota_config = build_ota_config(self._config)

        conf = {
            "device": device_conf,
            "database_path": "zigbee.db",
            "ezsp_config": ezsp_conf,
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 120) or 120,
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf

    def _build_znp_config(self, network_key, detected: dict = None) -> dict:
        """Build ZNP config from zigbee.znp section + probe results."""
        baud, _ = self._resolve_serial_params(
            "znp", detected,
            default_baud=115200, default_flow=None,
        )

        ota_config = build_ota_config(self._config)

        conf = {
            "device": {
                "path": self.port,
                "baudrate": baud,
            },
            "database_path": "zigbee.db",
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 120) or 120,
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf

    def _build_deconz_config(self, network_key, detected: dict = None) -> dict:
        """Build deCONZ config from zigbee.deconz section + probe results."""
        baud, _ = self._resolve_serial_params(
            "deconz", detected,
            default_baud=38400, default_flow="none",
        )

        ota_config = build_ota_config(self._config)

        conf = {
            "device": {
                "path": self.port,
                "baudrate": baud,
            },
            "database_path": "zigbee.db",
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 120) or 120,
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf