"""
Device tabs mixin for ZigbeeService.
Manages custom tab groupings for the frontend.
"""
import logging

logger = logging.getLogger("core.tabs")


class TabsMixin:
    """Device tab management methods."""

    def get_device_tabs(self) -> dict:
        """Get all device tabs."""
        return self.device_tabs

    def create_device_tab(self, name: str) -> dict:
        """Create a new device tab."""
        if name in self.device_tabs:
            return {"success": False, "error": f"Tab '{name}' already exists"}
        self.device_tabs[name] = []
        self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True, "tab": name}

    def delete_device_tab(self, name: str) -> dict:
        """Delete a device tab."""
        if name not in self.device_tabs:
            return {"success": False, "error": f"Tab '{name}' not found"}
        del self.device_tabs[name]
        self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True}

    def add_device_to_tab(self, tab_name: str, ieee: str) -> dict:
        """Add a device to a tab."""
        if tab_name not in self.device_tabs:
            return {"success": False, "error": f"Tab '{tab_name}' not found"}
        if ieee not in self.device_tabs[tab_name]:
            self.device_tabs[tab_name].append(ieee)
            self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True}

    def remove_device_from_tab(self, tab_name: str, ieee: str) -> dict:
        """Remove a device from a tab."""
        if tab_name not in self.device_tabs:
            return {"success": False, "error": f"Tab '{tab_name}' not found"}
        if ieee in self.device_tabs[tab_name]:
            self.device_tabs[tab_name].remove(ieee)
            self._save_json("./data/device_tabs.json", self.device_tabs)
        return {"success": True}
