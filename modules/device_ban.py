"""
Device Banning System for ZigBee-Manager
Blocks unwanted devices from joining by IEEE address.
"""
import logging
import json
import os
import asyncio
from typing import Set, Optional

logger = logging.getLogger("device_ban")


class DeviceBanManager:
    """
    Manages banned IEEE addresses.
    Devices in the ban list are immediately sent leave requests when they attempt to join.
    """

    def __init__(self, storage_path: str = "banned_devices.json"):
        self.storage_path = storage_path
        self._banned: Set[str] = set()
        self._load()

    def _normalize_ieee(self, ieee: str) -> str:
        """Normalize IEEE address to lowercase with colons."""
        ieee = str(ieee).lower().replace("-", ":")
        # Add colons if missing (e.g., "001122334455aabb" -> "00:11:22:33:44:55:aa:bb")
        if ":" not in ieee and len(ieee) == 16:
            ieee = ":".join(ieee[i:i+2] for i in range(0, 16, 2))
        return ieee

    def _load(self):
        """Load banned list from storage."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    self._banned = set(self._normalize_ieee(ieee) for ieee in data.get("banned", []))
                    logger.info(f"Loaded {len(self._banned)} banned IEEE addresses")
            except Exception as e:
                logger.error(f"Failed to load banned list: {e}")
                self._banned = set()
        else:
            self._banned = set()

    def _save(self):
        """Persist banned list to storage."""
        try:
            with open(self.storage_path, "w") as f:
                json.dump({
                    "banned": sorted(list(self._banned)),
                    "count": len(self._banned)
                }, f, indent=2)
            logger.info(f"Saved {len(self._banned)} banned addresses")
        except Exception as e:
            logger.error(f"Failed to save banned list: {e}")

    def is_banned(self, ieee: str) -> bool:
        """Check if an IEEE address is banned."""
        return self._normalize_ieee(ieee) in self._banned

    def ban(self, ieee: str, reason: Optional[str] = None) -> bool:
        """Add an IEEE address to the ban list."""
        normalized = self._normalize_ieee(ieee)
        if normalized in self._banned:
            logger.info(f"IEEE {normalized} already banned")
            return False

        self._banned.add(normalized)
        self._save()
        logger.warning(f"🚫 Banned IEEE: {normalized}" + (f" - Reason: {reason}" if reason else ""))
        return True

    def unban(self, ieee: str) -> bool:
        """Remove an IEEE address from the ban list."""
        normalized = self._normalize_ieee(ieee)
        if normalized not in self._banned:
            logger.info(f"IEEE {normalized} was not banned")
            return False

        self._banned.discard(normalized)
        self._save()
        logger.info(f"✅ Unbanned IEEE: {normalized}")
        return True

    def get_banned_list(self) -> list:
        """Get list of all banned IEEE addresses."""
        return sorted(list(self._banned))

    def clear(self):
        """Clear all bans."""
        count = len(self._banned)
        self._banned.clear()
        self._save()
        logger.info(f"Cleared {count} banned addresses")


# Singleton instance
_ban_manager: Optional[DeviceBanManager] = None


def get_ban_manager(storage_path: str = "banned_devices.json") -> DeviceBanManager:
    """Get or create the singleton ban manager."""
    global _ban_manager
    if _ban_manager is None:
        _ban_manager = DeviceBanManager(storage_path)
    return _ban_manager