# handlers/generic.py
import logging
from typing import Any, Dict, List

from .base import ClusterHandler

logger = logging.getLogger("handlers.generic")


class GenericClusterHandler(ClusterHandler):
    """Fallback handler for unsupported clusters. Manufacturer-aware."""

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._override_manager = None
        self._mfr_profile = None

    @property
    def override_manager(self):
        if self._override_manager is None:
            try:
                from modules.device_overrides import get_override_manager
                self._override_manager = get_override_manager()
            except ImportError:
                pass
        return self._override_manager

    @property
    def mfr_profile(self):
        """Lazy-load manufacturer profile for this cluster."""
        if self._mfr_profile is None:
            try:
                from modules.device_overrides import get_manufacturer_profile
                self._mfr_profile = get_manufacturer_profile(self.cluster_id) or {}
            except ImportError:
                self._mfr_profile = {}
        return self._mfr_profile

    def _get_device_info(self):
        model = str(getattr(self.device.zigpy_dev, 'model', '') or '')
        manufacturer = str(getattr(self.device.zigpy_dev, 'manufacturer', '') or '')
        return model, manufacturer

    def _is_manufacturer_cluster(self) -> bool:
        """Check if this is a known manufacturer-specific cluster."""
        return self.cluster_id >= 0xFC00 or bool(self.mfr_profile)

    def attribute_updated(self, attrid, value, timestamp=None):
        super().attribute_updated(attrid, value, timestamp)
        if hasattr(value, 'value'):
            value = value.value

        raw_key = f"cluster_{self.cluster_id:04x}_attr_{attrid:04x}"

        # Check for override mapping
        mapping = None
        if self.override_manager:
            model, manufacturer = self._get_device_info()
            mapping = self.override_manager.get_attribute_mapping(
                str(self.device.ieee), model, manufacturer,
                self.cluster_id, attrid
            )

        if mapping:
            friendly_name = mapping["name"]
            scale = mapping.get("scale", 1)
            try:
                if scale != 1 and isinstance(value, (int, float)):
                    value = round(value / scale, 2)
            except (TypeError, ValueError):
                pass

            self.device.update_state({friendly_name: value})
            logger.info(
                f"[{self.device.ieee}] Generic (mapped): "
                f"0x{self.cluster_id:04X}/0x{attrid:04X} -> {friendly_name} = {value}"
            )
        else:
            # Use manufacturer-aware prefix for known ecosystems
            mfr_name = self.mfr_profile.get("name", "").lower().replace(" ", "_")
            if mfr_name:
                raw_key = f"{mfr_name}_0x{attrid:04x}"

            self.device.update_state({raw_key: value})
            logger.info(
                f"[{self.device.ieee}] Generic{' (' + self.mfr_profile.get('name', '') + ')' if self.mfr_profile else ''}: "
                f"0x{self.cluster_id:04X}/0x{attrid:04X} = {value}"
            )

    def cluster_command(self, tsn, command_id, args):
        super().cluster_command(tsn, command_id, args)

        mapping = None
        if self.override_manager:
            model, manufacturer = self._get_device_info()
            mapping = self.override_manager.get_command_mapping(
                str(self.device.ieee), model, manufacturer,
                self.cluster_id, command_id
            )

        if mapping:
            cmd_name = mapping.get("name", f"cmd_{command_id:02x}")
            self.device.update_state({cmd_name: str(args)})
        else:
            mfr_name = self.mfr_profile.get("name", "").lower().replace(" ", "_")
            if mfr_name:
                raw_key = f"{mfr_name}_cmd_{command_id:02x}"
            else:
                raw_key = f"cluster_{self.cluster_id:04x}_cmd_{command_id:02x}"
            self.device.update_state({raw_key: str(args)})

    async def configure(self):
        """
        Configure — manufacturer-aware.
        Skips binding for manufacturer clusters that don't need it.
        Uses manufacturer code for reporting config where required.
        """
        if self._is_manufacturer_cluster():
            mfr_name = self.mfr_profile.get("name", "Unknown Manufacturer")
            logger.info(
                f"[{self.device.ieee}] Skipping standard configure for "
                f"manufacturer cluster 0x{self.cluster_id:04X} ({mfr_name})"
            )
            return True

        return await super().configure()

    async def poll(self) -> Dict[str, Any]:
        """
        Poll — manufacturer-aware.
        Uses manufacturer code for reads when profile requires it.
        """
        if not self.get_pollable_attributes():
            return {}

        if self.mfr_profile.get("requires_mfr_code"):
            mfr_code = self.mfr_profile["manufacturer_code"]
            results = {}
            for attr_id, attr_name in self.get_pollable_attributes().items():
                try:
                    result = await self.cluster.read_attributes(
                        [attr_id], manufacturer=mfr_code
                    )
                    if result and attr_id in result[0]:
                        value = result[0][attr_id]
                        if hasattr(value, 'value'):
                            value = value.value
                        results[attr_name] = value
                except Exception as e:
                    logger.debug(
                        f"[{self.device.ieee}] Failed to poll 0x{attr_id:04X} "
                        f"with mfr code 0x{mfr_code:04X}: {e}"
                    )
            return results

        return await super().poll()

    def get_debug_info(self) -> Dict:
        info = super().get_debug_info()
        info["generic_fallback"] = True
        info["manufacturer_profile"] = self.mfr_profile if self.mfr_profile else None

        if self.override_manager:
            model, manufacturer = self._get_device_info()
            defn = self.override_manager.get_definition(model, manufacturer)
            ieee_mappings = self.override_manager.get_ieee_mappings(str(self.device.ieee))
            info["has_model_definition"] = defn is not None
            info["ieee_mapping_count"] = len(ieee_mappings)

        return info

    def get_discovery_configs(self) -> List[Dict]:
        configs = []
        if not self.override_manager:
            return configs

        model, manufacturer = self._get_device_info()
        defn = self.override_manager.get_definition(model, manufacturer)
        if not defn:
            return configs

        cluster_hex = f"0x{self.cluster_id:04X}"
        cluster_def = defn.get("clusters", {}).get(cluster_hex, {})

        for attr_hex, attr_def in cluster_def.get("attributes", {}).items():
            name = attr_def.get("name")
            if not name:
                continue

            config = {
                "component": "sensor",
                "object_id": name,
                "config": {
                    "name": name.replace("_", " ").title(),
                    "value_template": "{{ value_json." + name + " }}",
                    "state_class": "measurement",
                }
            }
            if attr_def.get("device_class"):
                config["config"]["device_class"] = attr_def["device_class"]
            if attr_def.get("unit"):
                config["config"]["unit_of_measurement"] = attr_def["unit"]

            configs.append(config)

        return configs