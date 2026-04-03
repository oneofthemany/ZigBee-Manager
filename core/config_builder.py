"""
Radio configuration builder mixin.
Builds bellows (EZSP), zigpy-znp (ZNP) and zigpy-deconz config dicts
from config.yaml settings with dynamic baud-rate / flow-control detection.

Probing uses ControllerApplication.probe() from each radio library —
the same mechanism ZHA uses. probe() performs the full protocol handshake
(ASH reset for EZSP, ZNP SYS_PING, deCONZ device state request) and
internally iterates over baud-rate candidates defined in each library's
_probe_configs list. We vary flow_control ourselves for EZSP since
bellows' built-in probe doesn't iterate flow_control.
"""
import asyncio
import logging
from modules.ota import build_ota_config

logger = logging.getLogger("core.config")

# Flow-control variants to try for EZSP (bellows _probe_configs handles baud)
EZSP_FLOW_VARIANTS = ["software", "hardware"]

# deCONZ baud rates to try (zigpy-deconz _probe_configs may not cover all)
DECONZ_BAUD_VARIANTS = [38400, 57600, 115200]

# ── Library availability ────────────────────────────────────────────
_BELLOWS_AVAILABLE = False
_ZNP_AVAILABLE = False
_DECONZ_AVAILABLE = False

try:
    from bellows.zigbee.application import ControllerApplication as EzspApp
    _BELLOWS_AVAILABLE = True
except ImportError:
    EzspApp = None

try:
    from zigpy_znp.zigbee.application import ControllerApplication as ZnpApp
    _ZNP_AVAILABLE = True
except ImportError:
    ZnpApp = None

try:
    from zigpy_deconz.zigbee.application import ControllerApplication as DeconzApp
    _DECONZ_AVAILABLE = True
except ImportError:
    DeconzApp = None


class ConfigBuilderMixin:
    """Methods for building radio-specific configuration and probing radio type."""

    def _is_socket_path(self) -> bool:
        """Check if port is a TCP socket URI (MultiPAN zigbeed mode)."""
        return isinstance(self.port, str) and self.port.startswith("socket://")

    # =====================================================================
    # RADIO PROBING — uses ControllerApplication.probe() (ZHA approach)
    # =====================================================================

    async def _probe_radio_type(self) -> dict:
        """
        Detect radio type AND working serial parameters.

        Uses ControllerApplication.probe() from each library — the same
        mechanism ZHA uses. Returns dict with radio_type, baudrate,
        flow_control.
        """
        configured = self._config.get('radio_type', 'auto')

        # ── Explicit radio type → skip probing ──
        if configured and configured.upper() in ('EZSP', 'ZNP', 'DECONZ'):
            return self._explicit_radio_result(configured.upper())

        # ── Socket paths are always EZSP (zigbeed / MultiPAN) ──
        if self._is_socket_path():
            return {"radio_type": "EZSP", "baudrate": 0, "flow_control": "none"}

        # ── Auto-detect: EZSP → ZNP → deCONZ ──
        result = await self._probe_ezsp()
        if result:
            return result

        result = await self._probe_znp()
        if result:
            return result

        result = await self._probe_deconz()
        if result:
            return result

        raise RuntimeError(
            f"No compatible Zigbee radio found on {self.port}. "
            f"Libraries: bellows={_BELLOWS_AVAILABLE}, "
            f"zigpy-znp={_ZNP_AVAILABLE}, zigpy-deconz={_DECONZ_AVAILABLE}"
        )

    def _explicit_radio_result(self, radio_type: str) -> dict:
        """Build probe result for explicitly configured radio type."""
        section_map = {"EZSP": "ezsp", "ZNP": "znp", "DECONZ": "deconz"}
        defaults_map = {
            "EZSP":   {"baudrate": 115200, "flow_control": "software"},
            "ZNP":    {"baudrate": 115200, "flow_control": "none"},
            "DECONZ": {"baudrate": 38400,  "flow_control": "none"},
        }
        section = self._config.get(section_map[radio_type], {})
        defaults = defaults_map[radio_type]

        # Resolve 'auto' / None to defaults — never pass strings through
        raw_baud = section.get('baudrate')
        raw_flow = section.get('flow_control')
        is_auto_baud = raw_baud is None or str(raw_baud).lower() == 'auto'
        is_auto_flow = raw_flow is None or str(raw_flow).lower() == 'auto'

        return {
            "radio_type": radio_type,
            "baudrate": int(defaults["baudrate"]) if is_auto_baud else int(raw_baud),
            "flow_control": defaults["flow_control"] if is_auto_flow else raw_flow,
        }

    async def _probe_ezsp(self) -> dict | None:
        """
        Probe EZSP using bellows ControllerApplication.probe().

        probe() internally iterates bellows' _probe_configs which vary
        baudrate (115200, 460800, 57600). We iterate flow_control
        ourselves since bellows doesn't vary that.

        On success, probe() returns the working device config dict
        (including the baudrate that worked).
        """
        if not _BELLOWS_AVAILABLE:
            logger.debug("bellows not installed — skipping EZSP probe")
            return None

        for flow in EZSP_FLOW_VARIANTS:
            logger.info(
                f"Probing {self.port} for EZSP with {flow} flow control "
                f"(bellows will try multiple baud rates)..."
            )
            try:
                device_config = {
                    "path": self.port,
                    "baudrate": 115200,  # probe() overrides via _probe_configs
                    "flow_control": flow,
                }
                result = await asyncio.wait_for(
                    EzspApp.probe(device_config),
                    timeout=30.0,
                )
                if result:
                    # result is True or a dict with the working device config
                    if isinstance(result, dict):
                        detected_baud = int(result.get("baudrate", 115200))
                    else:
                        detected_baud = 115200  # bellows default
                    logger.info(
                        f"✅ EZSP radio detected @ {detected_baud} baud "
                        f"/ {flow} flow control"
                    )
                    return {
                        "radio_type": "EZSP",
                        "baudrate": detected_baud,
                        "flow_control": flow,
                    }
                else:
                    logger.info(f"  EZSP probe returned False for {flow}")
            except Exception as e:
                logger.info(f"  EZSP probe failed with {flow}: {e}")
            # Brief pause between flow control variants
            await asyncio.sleep(1.0)
        return None

    async def _probe_znp(self) -> dict | None:
        """
        Probe ZNP using zigpy-znp ControllerApplication.probe().

        probe() internally handles baud rate iteration.
        """
        if not _ZNP_AVAILABLE:
            logger.debug("zigpy-znp not installed — skipping ZNP probe")
            return None

        logger.info(
            f"Probing {self.port} for ZNP "
            f"(zigpy-znp will try multiple baud rates)..."
        )
        try:
            device_config = {
                "path": self.port,
                "baudrate": 115200,
            }
            result = await asyncio.wait_for(
                ZnpApp.probe(device_config),
                timeout=30.0,
            )
            if result:
                if isinstance(result, dict):
                    detected_baud = int(result.get("baudrate", 115200))
                else:
                    detected_baud = 115200
                logger.info(f"✅ ZNP radio detected @ {detected_baud} baud")
                return {
                    "radio_type": "ZNP",
                    "baudrate": detected_baud,
                    "flow_control": "none",
                }
            else:
                logger.info("  ZNP probe returned False")
        except Exception as e:
            logger.info(f"  ZNP probe failed: {e}")
        return None

    async def _probe_deconz(self) -> dict | None:
        """
        Probe deCONZ using zigpy-deconz ControllerApplication.probe().
        """
        if not _DECONZ_AVAILABLE:
            logger.debug("zigpy-deconz not installed — skipping deCONZ probe")
            return None

        for baud in DECONZ_BAUD_VARIANTS:
            logger.info(f"Probing {self.port} for deCONZ @ {baud}...")
            try:
                device_config = {
                    "path": self.port,
                    "baudrate": baud,
                }
                result = await asyncio.wait_for(
                    DeconzApp.probe(device_config),
                    timeout=15.0,
                )
                if result:
                    logger.info(f"✅ deCONZ radio detected @ {baud} baud")
                    return {
                        "radio_type": "DECONZ",
                        "baudrate": baud,
                        "flow_control": "none",
                    }
            except Exception as e:
                logger.info(f"  deCONZ probe failed @ {baud}: {e}")
            await asyncio.sleep(0.5)
        return None

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
            "topology_scan_period": self._config.get('topology_scan_interval', 0),
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf

    def _build_znp_config(self, network_key, detected: dict = None) -> dict:
        """Build ZNP config from zigbee.znp section + probe results."""
        baud, _ = self._resolve_serial_params(
            "znp", detected,
            default_baud=115200, default_flow="none",
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
            "topology_scan_period": self._config.get('topology_scan_interval', 0),
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
            "topology_scan_period": self._config.get('topology_scan_interval', 0),
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf