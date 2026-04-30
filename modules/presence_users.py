"""
Presence Users — Per-user home/away tracking via PWA geolocation and OwnTracks.

Each user is exposed to the rest of ZMM as a *virtual device* with attributes:
    presence       : "home" | "away" | "unknown"
    distance_m     : float (metres from home)
    accuracy_m     : float (GPS reported accuracy)
    source         : "pwa" | "owntracks" | "manual"
    last_update    : float (unix ts)
    lat / lon      : float (latest reported position) — stored in memory only
                      after persistence, NOT written to disk in long-term form.

These virtual devices are merged into the automation engine's device registry
(see main.py wiring), so existing rule-builder, AI automations and MQTT
discovery all work without modification.

Storage:
    data/presence_users.yaml  — user definitions (no coordinates persisted
                                across restarts unless explicitly enabled).

Dependencies:
    - mqtt_handler (optional, for HA discovery + state publish)
    - event_emitter (websocket broadcast)
    - automation_evaluator (instant rule trigger on state change)

Privacy:
    - We never log raw coordinates beyond DEBUG level.
    - We persist only the configured home location and radius, not the
      live position fixes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("modules.presence_users")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRESENCE_HOME = "home"
PRESENCE_AWAY = "away"
PRESENCE_UNKNOWN = "unknown"

DEFAULT_RADIUS_M = 100.0          # 100 m geofence radius
DEFAULT_HYSTERESIS_M = 30.0       # extra buffer to leave home (radius + this)
DEFAULT_STALE_AFTER_S = 30 * 60   # mark unknown after 30 min of silence
DEFAULT_MIN_ACCURACY_M = 250.0    # ignore fixes worse than this

CONFIG_PATH = Path("./data/presence_users.yaml")

# IEEE-style identifier prefix for virtual users so they slot into the
# existing 16-hex-char namespace cleanly (collision-proof).
USER_IEEE_PREFIX = "user::"


def _user_ieee(user_id: str) -> str:
    """Stable virtual-IEEE for a user_id."""
    return f"{USER_IEEE_PREFIX}{user_id.lower()}"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class UserConfig:
    user_id: str                          # short stable id, e.g. "sean"
    display_name: str                     # "Sean"
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    radius_m: float = DEFAULT_RADIUS_M
    hysteresis_m: float = DEFAULT_HYSTERESIS_M
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    min_accuracy_m: float = DEFAULT_MIN_ACCURACY_M
    enabled: bool = True
    # Sources allowed to update this user, in priority order. PWA reports are
    # always accepted but OwnTracks reports are only honoured if the topic
    # device id matches `owntracks_device` (when set).
    owntracks_device: Optional[str] = None  # e.g. "phone"
    owntracks_user: Optional[str] = None    # e.g. "sean"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UserConfig":
        return UserConfig(
            user_id=str(d["user_id"]),
            display_name=str(d.get("display_name") or d["user_id"]),
            home_lat=d.get("home_lat"),
            home_lon=d.get("home_lon"),
            radius_m=float(d.get("radius_m", DEFAULT_RADIUS_M)),
            hysteresis_m=float(d.get("hysteresis_m", DEFAULT_HYSTERESIS_M)),
            stale_after_s=float(d.get("stale_after_s", DEFAULT_STALE_AFTER_S)),
            min_accuracy_m=float(d.get("min_accuracy_m", DEFAULT_MIN_ACCURACY_M)),
            enabled=bool(d.get("enabled", True)),
            owntracks_device=d.get("owntracks_device"),
            owntracks_user=d.get("owntracks_user"),
        )


# ---------------------------------------------------------------------------
# Virtual device shim
# ---------------------------------------------------------------------------

class _Capabilities:
    """Minimal capabilities object so the automation engine treats us as
    a sensor (no actuator capabilities → never appears in target lists)."""
    def has_capability(self, _name: str) -> bool:
        return False


class PresenceUserDevice:
    """
    Quack-types as a Zigbee/Matter device for the automation engine and
    UI listing. Only the attributes/methods the rest of the code reads
    are implemented.
    """
    def __init__(self, cfg: UserConfig):
        self.cfg = cfg
        self.ieee = _user_ieee(cfg.user_id)
        self.friendly_name = cfg.display_name
        self.manufacturer = "ZMM"
        self.model = "Presence User"
        self.last_seen: float = 0.0
        self.state: Dict[str, Any] = {
            "presence": PRESENCE_UNKNOWN,
            "available": True,
            "distance_m": None,
            "accuracy_m": None,
            "source": None,
            "last_update": None,
        }
        # In-memory only — never persisted
        self._last_lat: Optional[float] = None
        self._last_lon: Optional[float] = None
        self.capabilities = _Capabilities()

    # The automation engine calls these
    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        # Not actuatable from automations
        return []

    def get_device_discovery_configs(self) -> List[Dict[str, Any]]:
        """HA MQTT Discovery payload for this user."""
        node_id = f"presence_{self.cfg.user_id}"
        return [
            {
                "component": "device_tracker",
                "object_id": "presence",
                "config": {
                    "name": f"{self.cfg.display_name} Presence",
                    "unique_id": f"{node_id}_presence",
                    "state_topic": f"zigbee/presence/{self.cfg.user_id}/state",
                    "value_template": "{{ value_json.presence }}",
                    "payload_home": "home",
                    "payload_not_home": "away",
                    "source_type": "gps",
                    "device": {
                        "identifiers": [node_id],
                        "name": self.cfg.display_name,
                        "model": "Presence User",
                        "manufacturer": "ZMM",
                    },
                },
            },
        ]

    def to_device_list_entry(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee,
            "friendly_name": self.friendly_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "type": "presence_user",
            "available": True,
            "state": dict(self.state),
            "last_seen": self.last_seen,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class PresenceUserManager:
    """
    Owns user configs, the virtual device dict, and ingest paths from
    PWA HTTP and OwnTracks MQTT.
    """

    def __init__(
            self,
            mqtt_handler: Optional[Any] = None,
            event_emitter: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
            automation_evaluator: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
            config_path: Path = CONFIG_PATH,
    ):
        self.mqtt_handler = mqtt_handler
        self.event_emitter = event_emitter
        self.automation_evaluator = automation_evaluator
        self.config_path = Path(config_path)

        self.devices: Dict[str, PresenceUserDevice] = {}
        self._stale_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._load_config()
        for dev in self.devices.values():
            await self._publish_discovery(dev)
            await self._publish_state(dev)
        self._stale_task = asyncio.create_task(self._stale_watcher())
        logger.info(f"Presence users started ({len(self.devices)} configured)")

    async def stop(self) -> None:
        if self._stale_task:
            self._stale_task.cancel()
            try:
                await self._stale_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        if not self.config_path.exists():
            return
        try:
            with open(self.config_path) as f:
                raw = yaml.safe_load(f) or {}
            users = raw.get("users", [])
            for u in users:
                try:
                    cfg = UserConfig.from_dict(u)
                    self.devices[_user_ieee(cfg.user_id)] = PresenceUserDevice(cfg)
                except Exception as e:
                    logger.warning(f"Skipping bad presence user entry: {e}")
            logger.info(f"Loaded {len(self.devices)} presence users")
        except Exception as e:
            logger.error(f"Failed to load presence users config: {e}")

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"users": [d.cfg.to_dict() for d in self.devices.values()]}
            with open(self.config_path, "w") as f:
                yaml.dump(payload, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.error(f"Failed to save presence users config: {e}")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {
                **d.cfg.to_dict(),
                "ieee": d.ieee,
                "state": dict(d.state),
                "last_seen": d.last_seen,
            }
            for d in self.devices.values()
        ]

    def get_user(self, user_id: str) -> Optional[PresenceUserDevice]:
        return self.devices.get(_user_ieee(user_id))

    async def upsert_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                cfg = UserConfig.from_dict(data)
            except Exception as e:
                return {"success": False, "error": f"Bad user payload: {e}"}

            if not cfg.user_id or not cfg.user_id.replace("_", "").isalnum():
                return {"success": False, "error": "user_id must be alphanumeric/underscore"}

            ieee = _user_ieee(cfg.user_id)
            existing = self.devices.get(ieee)
            if existing:
                # Preserve runtime state across config edits
                state_snapshot = dict(existing.state)
                last_seen = existing.last_seen
                last_lat, last_lon = existing._last_lat, existing._last_lon
                dev = PresenceUserDevice(cfg)
                dev.state.update(state_snapshot)
                dev.last_seen = last_seen
                dev._last_lat = last_lat
                dev._last_lon = last_lon
                self.devices[ieee] = dev
            else:
                self.devices[ieee] = PresenceUserDevice(cfg)

            self._save_config()
            await self._publish_discovery(self.devices[ieee])
            await self._publish_state(self.devices[ieee])
            return {"success": True, "user": self.list_users()}

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        async with self._lock:
            ieee = _user_ieee(user_id)
            dev = self.devices.pop(ieee, None)
            if not dev:
                return {"success": False, "error": "User not found"}
            self._save_config()
            await self._remove_discovery(dev)
            return {"success": True}

    # ------------------------------------------------------------------
    # Ingest paths
    # ------------------------------------------------------------------
    async def report_pwa_fix(
            self,
            user_id: str,
            lat: float,
            lon: float,
            accuracy: Optional[float] = None,
            timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Called from POST /api/presence/user/{user_id}."""
        return await self._ingest(
            user_id=user_id,
            lat=lat,
            lon=lon,
            accuracy=accuracy,
            timestamp=timestamp,
            source="pwa",
        )

    async def report_owntracks(self, topic: str, payload: Dict[str, Any]) -> None:
        """
        OwnTracks publishes to: owntracks/<user>/<device>
        with payloads like:
            {"_type":"location","lat":51.5,"lon":-0.1,"acc":15,"tst":1690000000,...}
        """
        try:
            parts = topic.split("/")
            if len(parts) < 3 or parts[0] != "owntracks":
                return
            ot_user = parts[1]
            ot_device = parts[2]
        except Exception:
            return

        if payload.get("_type") != "location":
            return

        # Find the user whose owntracks_user/owntracks_device matches.
        target: Optional[UserConfig] = None
        for d in self.devices.values():
            cu = d.cfg.owntracks_user
            cd = d.cfg.owntracks_device
            if cu and cu == ot_user and (not cd or cd == ot_device):
                target = d.cfg
                break

        if not target:
            logger.debug(f"OwnTracks msg for unmapped {ot_user}/{ot_device}")
            return

        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return

        await self._ingest(
            user_id=target.user_id,
            lat=lat,
            lon=lon,
            accuracy=float(payload.get("acc", 0) or 0) or None,
            timestamp=float(payload.get("tst", time.time())),
            source="owntracks",
        )

    async def _ingest(
            self,
            user_id: str,
            lat: float,
            lon: float,
            accuracy: Optional[float],
            timestamp: Optional[float],
            source: str,
    ) -> Dict[str, Any]:
        dev = self.get_user(user_id)
        if not dev:
            return {"success": False, "error": "User not found"}
        if not dev.cfg.enabled:
            return {"success": False, "error": "User disabled"}

        if dev.cfg.home_lat is None or dev.cfg.home_lon is None:
            return {"success": False, "error": "User has no home location set"}

        # Drop low-accuracy fixes
        if accuracy is not None and accuracy > dev.cfg.min_accuracy_m:
            logger.debug(
                f"[{user_id}] dropping low-accuracy fix ({accuracy:.0f} m > "
                f"{dev.cfg.min_accuracy_m:.0f} m) from {source}"
            )
            return {"success": False, "error": "accuracy too low", "ignored": True}

        ts = timestamp or time.time()
        # Reject obviously stale fixes
        if ts < time.time() - 6 * 3600:
            return {"success": False, "error": "fix too old", "ignored": True}

        distance = _haversine_m(lat, lon, dev.cfg.home_lat, dev.cfg.home_lon)

        # Hysteresis: stay 'home' until we exceed radius+hysteresis
        prev = dev.state.get("presence", PRESENCE_UNKNOWN)
        radius = dev.cfg.radius_m
        leave_threshold = radius + dev.cfg.hysteresis_m

        if prev == PRESENCE_HOME:
            new_state = PRESENCE_HOME if distance <= leave_threshold else PRESENCE_AWAY
        else:
            new_state = PRESENCE_HOME if distance <= radius else PRESENCE_AWAY

        async with self._lock:
            dev._last_lat = lat
            dev._last_lon = lon
            dev.last_seen = ts

            changed: Dict[str, Any] = {}
            if dev.state.get("presence") != new_state:
                changed["presence"] = new_state
            if dev.state.get("source") != source:
                changed["source"] = source

            dev.state["presence"] = new_state
            dev.state["distance_m"] = round(distance, 1)
            dev.state["accuracy_m"] = round(accuracy, 1) if accuracy else None
            dev.state["source"] = source
            dev.state["last_update"] = ts

        if changed:
            logger.info(
                f"[presence:{user_id}] {prev} → {new_state} "
                f"(distance {distance:.0f} m, source={source})"
            )
            await self._fire_state_change(dev, changed)
        else:
            # Still publish periodically so HA + WS see distance updates
            await self._publish_state(dev)
            await self._broadcast_event(dev)

        return {
            "success": True,
            "user_id": user_id,
            "presence": new_state,
            "distance_m": round(distance, 1),
        }

    async def manual_set(self, user_id: str, presence: str) -> Dict[str, Any]:
        """Manual override (e.g. for testing or when GPS isn't available)."""
        if presence not in (PRESENCE_HOME, PRESENCE_AWAY, PRESENCE_UNKNOWN):
            return {"success": False, "error": "Bad presence value"}
        dev = self.get_user(user_id)
        if not dev:
            return {"success": False, "error": "User not found"}

        prev = dev.state.get("presence")
        async with self._lock:
            dev.state["presence"] = presence
            dev.state["source"] = "manual"
            dev.state["last_update"] = time.time()
            dev.last_seen = time.time()

        if prev != presence:
            await self._fire_state_change(dev, {"presence": presence, "source": "manual"})
        return {"success": True, "user_id": user_id, "presence": presence}

    # ------------------------------------------------------------------
    # State change pipeline
    # ------------------------------------------------------------------
    async def _fire_state_change(
            self,
            dev: PresenceUserDevice,
            changed: Dict[str, Any],
    ) -> None:
        # 1. Trigger automation engine immediately
        if self.automation_evaluator:
            try:
                await self.automation_evaluator(dev.ieee, changed)
            except Exception as e:
                logger.error(f"[presence:{dev.cfg.user_id}] automation eval failed: {e}")

        # 2. Publish state to MQTT
        await self._publish_state(dev)

        # 3. Broadcast to UI websocket
        await self._broadcast_event(dev)

    async def _broadcast_event(self, dev: PresenceUserDevice) -> None:
        if not self.event_emitter:
            return
        try:
            await self.event_emitter("presence_user_updated", {
                "ieee": dev.ieee,
                "user_id": dev.cfg.user_id,
                "state": dict(dev.state),
                "last_seen": dev.last_seen,
            })
        except Exception as e:
            logger.debug(f"presence broadcast failed: {e}")

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------
    async def _publish_state(self, dev: PresenceUserDevice) -> None:
        if not self.mqtt_handler:
            return
        topic = f"zigbee/presence/{dev.cfg.user_id}/state"
        payload = {
            "presence": dev.state.get("presence"),
            "distance_m": dev.state.get("distance_m"),
            "accuracy_m": dev.state.get("accuracy_m"),
            "source": dev.state.get("source"),
            "last_update": dev.state.get("last_update"),
            "available": True,
        }
        try:
            await self.mqtt_handler.publish(
                topic, json.dumps(payload), retain=True, qos=1
            )
        except Exception as e:
            logger.debug(f"MQTT presence state publish failed: {e}")

    async def _publish_discovery(self, dev: PresenceUserDevice) -> None:
        if not self.mqtt_handler:
            return
        node_id = f"presence_{dev.cfg.user_id}"
        for entity in dev.get_device_discovery_configs():
            topic = (
                f"homeassistant/{entity['component']}/"
                f"{node_id}/{entity['object_id']}/config"
            )
            try:
                await self.mqtt_handler.publish(
                    topic, json.dumps(entity["config"]), retain=True, qos=1
                )
            except Exception as e:
                logger.debug(f"MQTT presence discovery failed: {e}")

    async def _remove_discovery(self, dev: PresenceUserDevice) -> None:
        if not self.mqtt_handler:
            return
        node_id = f"presence_{dev.cfg.user_id}"
        for entity in dev.get_device_discovery_configs():
            topic = (
                f"homeassistant/{entity['component']}/"
                f"{node_id}/{entity['object_id']}/config"
            )
            try:
                await self.mqtt_handler.publish(topic, "", retain=True, qos=1)
            except Exception as e:
                logger.debug(f"MQTT presence discovery removal failed: {e}")

    # ------------------------------------------------------------------
    # Stale-fix watchdog
    # ------------------------------------------------------------------
    async def _stale_watcher(self) -> None:
        """Mark users as 'unknown' if no fix has been received for too long."""
        try:
            while True:
                await asyncio.sleep(60)
                now = time.time()
                for dev in list(self.devices.values()):
                    if not dev.cfg.enabled:
                        continue
                    if not dev.last_seen:
                        continue
                    if dev.state.get("presence") == PRESENCE_UNKNOWN:
                        continue
                    if now - dev.last_seen > dev.cfg.stale_after_s:
                        prev = dev.state.get("presence")
                        async with self._lock:
                            dev.state["presence"] = PRESENCE_UNKNOWN
                            dev.state["source"] = "stale"
                        if prev != PRESENCE_UNKNOWN:
                            logger.info(
                                f"[presence:{dev.cfg.user_id}] {prev} → unknown "
                                f"(no fix in {now - dev.last_seen:.0f}s)"
                            )
                            await self._fire_state_change(
                                dev, {"presence": PRESENCE_UNKNOWN, "source": "stale"}
                            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Stale watcher crashed: {e}")


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------

_manager: Optional[PresenceUserManager] = None


def get_presence_manager() -> Optional[PresenceUserManager]:
    return _manager


def set_presence_manager(mgr: PresenceUserManager) -> None:
    global _manager
    _manager = mgr