"""
Radio configuration builder mixin.
Builds bellows (EZSP) and zigpy-znp config dicts from config.yaml settings.
"""
import logging
from modules.ota import build_ota_config

logger = logging.getLogger("core.config")


class ConfigBuilderMixin:
    """Methods for building radio-specific configuration and probing radio type."""

    def _is_socket_path(self) -> bool:
        """Check if port is a TCP socket URI (MultiPAN zigbeed mode)."""
        return isinstance(self.port, str) and self.port.startswith("socket://")

    async def _probe_radio_type(self) -> str:
        """Detect whether the coordinator is EZSP or ZNP."""
        import asyncio
        import bellows.uart

        configured = self._config.get('radio_type', 'auto')
        if configured and configured.upper() in ('EZSP', 'ZNP'):
            return configured.upper()

        # Socket paths are always EZSP (zigbeed / MultiPAN)
        if self._is_socket_path():
            return "EZSP"

        logger.info(f"Probing {self.port} for EZSP radio...")
        try:
            protocol = await asyncio.wait_for(
                bellows.uart.connect({
                    "path": self.port,
                    "baudrate": 115200,
                    "flow_control": "hardware"
                }, None),
                timeout=5.0
            )
            logger.info("✅ EZSP radio detected")
            try:
                protocol.close()
            except:
                pass
            await asyncio.sleep(1.0)
            del protocol
        except Exception as e:
            logger.info(f"Not EZSP: {e}")
            # If not EZSP, try ZNP
            logger.info(f"Probing {self.port} for ZNP radio...")
            try:
                import zigpy_znp.api
                api = zigpy_znp.api.ZNP(zigpy_znp.config.CONFIG_SCHEMA({
                    "device": {"path": self.port, "baudrate": 115200}
                }))
                await asyncio.wait_for(api.connect(), timeout=5.0)
                logger.info("✅ ZNP radio detected")
                api.close()
                await asyncio.sleep(1.0)
                return "ZNP"
            except Exception as e2:
                logger.info(f"Not ZNP: {e2}")
                raise RuntimeError(f"No compatible Zigbee radio found on {self.port}")

        logger.info("Note: Background task errors during probe are expected and harmless")
        await asyncio.sleep(3.0)
        return "EZSP"

    def _build_ezsp_config(self, ezsp_conf: dict, network_key) -> dict:
        """Build EZSP config from zigbee.ezsp section."""
        ezsp_settings = self._config.get('ezsp', {})

        if self._is_socket_path():
            device_conf = {
                "path": self.port,
            }
            logger.info(f"Using socket connection: {self.port}")
        else:
            device_conf = {
                "path": self.port,
                "baudrate": ezsp_settings.get('baudrate', 460800),
                "flow_control": ezsp_settings.get('flow_control', 'hardware')
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
            "topology_scan_period": self._config.get('topology_scan_interval', 0)
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf

    def _build_znp_config(self, network_key) -> dict:
        """Build ZNP config from zigbee.znp section."""
        znp_settings = self._config.get('znp', {})

        ota_config = build_ota_config(self._config)

        conf = {
            "device": {
                "path": self.port,
                "baudrate": znp_settings.get('baudrate', 115200)
            },
            "database_path": "zigbee.db",
            "network": {
                "channel": self._config.get('channel', 25),
                "key": network_key,
                "update_id": True,
            },
            "topology_scan_period": self._config.get('topology_scan_interval', 0)
        }

        if ota_config:
            conf["ota"] = ota_config

        return conf