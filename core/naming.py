"""
Device Naming Mixin
Handles device name mapping, sanitization, and identification.
"""
import re
from typing import Optional

class NamingMixin:

    def get_safe_name(self, ieee: str) -> str:
        name = self.friendly_names.get(ieee, ieee)
        return re.sub(r'[+#/]', '-', name)

    def _rebuild_name_maps(self):
        self._name_to_ieee.clear()
        self._node_id_to_ieee.clear()
        for ieee in self.devices:
            safe_name = self.get_safe_name(ieee)
            self._name_to_ieee[safe_name] = ieee
            self._name_to_ieee[safe_name.lower()] = ieee
            node_id = ieee.replace(":", "")
            self._node_id_to_ieee[node_id] = ieee
            self._node_id_to_ieee[node_id.lower()] = ieee

    def _resolve_device_identifier(self, identifier: str) -> Optional[str]:
        """Resolve a device name/node_id/ieee to an IEEE address."""
        if identifier in self.devices:
            return identifier
        if identifier in self._name_to_ieee:
            return self._name_to_ieee[identifier]
        if identifier in self._node_id_to_ieee:
            return self._node_id_to_ieee[identifier]
        
        lower_id = identifier.lower()
        if lower_id in self._name_to_ieee:
            return self._name_to_ieee[lower_id]
        if lower_id in self._node_id_to_ieee:
            return self._node_id_to_ieee[lower_id]
        
        for name, ieee in self._name_to_ieee.items():
            if lower_id in name.lower():
                return ieee
        return None
