"""
Zones - Per-device RSSI-to-coordinator presence detection.

Re-engineered design (supersedes pair-link RSSI model):
  * Each zone holds a set of device IEEEs.
  * For every frame received at the coordinator from a zone device, a single
    (rssi, lqi) sample is recorded against that device.
  * Calibration is explicit: the user triggers it ONCE the room is empty.
    Baseline (trimmed mean + σ) is computed per-device from that window.
  * Evaluation compares smoothed current RSSI to baseline in σ units.
  * Aggressiveness (per-device σ threshold multiplier) is only exposed for
    mains-fed (Router role) devices. End devices contribute weak "evidence"
    weight at the default threshold because their sample cadence is dictated
    by their own wake cycle.
  * A zone is OCCUPIED when the weighted sum of triggered devices crosses
    `min_devices_triggered`. Clears after `clear_delay` of stability.
"""

import asyncio
import logging
import time
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from collections import deque
from enum import Enum, auto
from statistics import mean, stdev

logger = logging.getLogger(__name__)


def setup_motion_logging():
    """Configure a separate file handler for zone/motion events."""
    import os
    from logging.handlers import RotatingFileHandler

    log_dir = "./logs"
    if not os.path.exists(log_dir):
        return
    log_file = os.path.join(log_dir, "motion.log")
    try:
        handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        logger.info(f"Motion logging initialized to {log_file}")
    except Exception as e:
        print(f"Failed to create log handler: {e}")


try:
    setup_motion_logging()
except Exception as e:
    print(f"Failed to setup motion logging: {e}")


def normalize_ieee(ieee: Any) -> str:
    """Normalize IEEE to lowercase colon-separated string."""
    if ieee is None:
        return ""
    s = str(ieee).lower().strip()
    if len(s) == 16 and ":" not in s:
        s = ":".join(s[i:i + 2] for i in range(0, 16, 2))
    return s


class ZoneState(Enum):
    UNCALIBRATED = auto()
    CALIBRATING = auto()
    VACANT = auto()
    OCCUPIED = auto()


@dataclass
class RssiSample:
    rssi: int
    lqi: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class DeviceStats:
    """
    Per-device RSSI statistics measured at the coordinator.
    Holds rolling samples, calibration baseline, and a short smoothing window
    used for evaluation.
    """
    ieee: str
    is_router: bool = False                   # mains-fed (derived from node_desc)
    aggressiveness: float = 1.0               # σ multiplier; 1.0 = zone default; only used for routers
    samples: deque = field(default_factory=lambda: deque(maxlen=400))
    baseline_samples: List[RssiSample] = field(default_factory=list)  # collected during CALIBRATING only

    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None

    last_rssi: Optional[int] = None
    last_lqi: Optional[int] = None
    last_seen: Optional[float] = None

    _smoothed_rssi: Optional[float] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ #
    def add_sample(self, rssi: int, lqi: int, calibrating: bool) -> None:
        sample = RssiSample(rssi=rssi, lqi=lqi)
        self.samples.append(sample)
        self.last_rssi = rssi
        self.last_lqi = lqi
        self.last_seen = sample.timestamp
        self._update_smoothed(window=5)
        if calibrating:
            self.baseline_samples.append(sample)

    def _update_smoothed(self, window: int = 5) -> None:
        if not self.samples:
            self._smoothed_rssi = None
            return
        recent = list(self.samples)[-window:]
        self._smoothed_rssi = sum(s.rssi for s in recent) / len(recent)

    # ------------------------------------------------------------------ #
    def compute_baseline(self, min_samples: int = 20) -> bool:
        """
        Compute baseline from collected calibration samples.
        Uses trimmed mean (middle 80%) to reduce outlier impact.
        Returns True if baseline was successfully computed.
        """
        if len(self.baseline_samples) < min_samples:
            return False

        values = sorted(s.rssi for s in self.baseline_samples)
        trim = int(len(values) * 0.1)
        if trim > 0:
            values = values[trim:-trim]

        self.baseline_mean = mean(values)
        self.baseline_std = stdev(values) if len(values) > 1 else 1.0
        if self.baseline_std < 1.0:
            self.baseline_std = 1.0   # floor — prevents divide-by-tiny-noise

        logger.info(
            f"[baseline] {self.ieee[-8:]} μ={self.baseline_mean:.1f}dBm "
            f"σ={self.baseline_std:.2f} from {len(self.baseline_samples)} samples "
            f"(trimmed to {len(values)})"
        )
        return True

    def clear_baseline(self) -> None:
        self.baseline_mean = None
        self.baseline_std = None
        self.baseline_samples.clear()

    # ------------------------------------------------------------------ #
    def get_deviation(self) -> Optional[float]:
        """σ-units deviation of smoothed RSSI from calibrated baseline."""
        if self.baseline_mean is None or self.baseline_std is None:
            return None
        if self._smoothed_rssi is None:
            return None
        return abs(self._smoothed_rssi - self.baseline_mean) / self.baseline_std

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ieee': self.ieee,
            'is_router': self.is_router,
            'aggressiveness': self.aggressiveness,
            'last_rssi': self.last_rssi,
            'last_lqi': self.last_lqi,
            'smoothed_rssi': self._smoothed_rssi,
            'baseline_mean': self.baseline_mean,
            'baseline_std': self.baseline_std,
            'deviation': self.get_deviation(),
            'sample_count': len(self.samples),
            'baseline_sample_count': len(self.baseline_samples),
            'last_seen': self.last_seen,
        }


@dataclass
class ZoneConfig:
    name: str
    device_ieees: List[str]

    # Detection tuning
    deviation_threshold: float = 2.5          # σ threshold (default for routers)
    min_devices_triggered: float = 1.5        # weighted sum required to trigger
    clear_delay: int = 15                     # seconds of stability before VACANT
    calibration_time: int = 120               # seconds — duration of CALIBRATING window

    # Evidence weighting
    end_device_weight: float = 0.5            # end devices count as partial evidence only

    # Integration
    mqtt_topic_override: Optional[str] = None

    # Per-device aggressiveness (σ multiplier). ONLY mains-fed devices are allowed
    # to have an entry here. Accepted range: 0.5 (very sensitive) .. 2.0 (very relaxed).
    per_device_aggressiveness: Dict[str, float] = field(default_factory=dict)


@dataclass
class Zone:
    config: ZoneConfig
    state: ZoneState = ZoneState.UNCALIBRATED
    devices: Dict[str, DeviceStats] = field(default_factory=dict)

    calibration_start: Optional[float] = None
    last_trigger_time: Optional[float] = None
    last_clear_time: Optional[float] = None
    occupied_since: Optional[float] = None

    on_occupied: Optional[Callable[['Zone'], None]] = None
    on_vacant: Optional[Callable[['Zone'], None]] = None

    _calibration_callback: Optional[Callable] = field(default=None, init=False, repr=False)
    _last_progress: int = field(default=0, init=False, repr=False)
    _app_controller: Any = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def device_ieees(self) -> List[str]:
        return self.config.device_ieees

    def _ensure_device(self, ieee: str) -> DeviceStats:
        if ieee not in self.devices:
            stats = DeviceStats(ieee=ieee)
            stats.aggressiveness = self.config.per_device_aggressiveness.get(ieee, 1.0)
            stats.is_router = self._is_router(ieee)
            self.devices[ieee] = stats
        return self.devices[ieee]

    def _is_router(self, ieee: str) -> bool:
        if not self._app_controller or not hasattr(self._app_controller, 'devices'):
            return False
        dev = self._app_controller.devices.get(ieee)
        if not dev:
            return False
        try:
            return dev.get_role() in ("Router", "Coordinator")
        except Exception:
            return False

    def refresh_device_roles(self) -> None:
        """Re-read is_router from the device registry (called after zone load)."""
        for ieee, stats in self.devices.items():
            stats.is_router = self._is_router(ieee)

    # ------------------------------------------------------------------ #
    def record_rssi(self, ieee: str, rssi: int, lqi: int) -> None:
        """
        Record a single RSSI/LQI sample for a zone device.
        Called by ZoneManager; already filtered to in-zone devices.
        """
        norm = normalize_ieee(ieee)
        if norm not in self.device_ieees:
            return
        stats = self._ensure_device(norm)
        stats.add_sample(rssi, lqi, calibrating=(self.state == ZoneState.CALIBRATING))
        if self.state == ZoneState.CALIBRATING:
            logger.debug(
                f"CALIB [{self.name}] {norm[-8:]} "
                f"rssi={rssi} lqi={lqi} n={len(stats.baseline_samples)}"
            )

    # ------------------------------------------------------------------ #
    def start_calibration(self) -> None:
        """Enter the CALIBRATING state and clear previous baselines."""
        self.state = ZoneState.CALIBRATING
        self.calibration_start = time.time()
        self._last_progress = 0
        for stats in self.devices.values():
            stats.clear_baseline()
        # Ensure every configured device has a DeviceStats so the UI can show it
        for ieee in self.device_ieees:
            self._ensure_device(ieee)
        logger.info(f"Zone '{self.name}' calibration started ({self.config.calibration_time}s)")
        self._emit_calibration_update()

    def cancel_calibration(self) -> None:
        """Abort without computing baselines."""
        if self.state != ZoneState.CALIBRATING:
            return
        self.state = ZoneState.UNCALIBRATED
        self.calibration_start = None
        for stats in self.devices.values():
            stats.baseline_samples.clear()
        logger.info(f"Zone '{self.name}' calibration cancelled")

    def finalize_calibration(self, min_samples_per_device: int = 20) -> int:
        """Compute baselines from collected samples. Returns ready device count."""
        ready = 0
        for stats in self.devices.values():
            if stats.compute_baseline(min_samples=min_samples_per_device):
                ready += 1

        if ready == 0:
            logger.warning(
                f"Zone '{self.name}' calibration produced 0 baselines — "
                f"insufficient samples. Staying UNCALIBRATED."
            )
            self.state = ZoneState.UNCALIBRATED
            return 0

        self.state = ZoneState.VACANT
        logger.info(f"Zone '{self.name}' calibrated: {ready}/{len(self.devices)} devices have baselines")
        self._emit_calibration_update()
        return ready

    def check_calibration(self) -> None:
        """Advance calibration progress and finalize if window elapsed."""
        if self.state != ZoneState.CALIBRATING or self.calibration_start is None:
            return
        elapsed = time.time() - self.calibration_start
        progress = min(100, int((elapsed / self.config.calibration_time) * 100))
        if progress - self._last_progress >= 5:
            self._last_progress = progress
            self._emit_calibration_update()
        if elapsed >= self.config.calibration_time:
            self.finalize_calibration()

    def _emit_calibration_update(self) -> None:
        if not self._calibration_callback:
            return
        elapsed = (time.time() - self.calibration_start) if self.calibration_start else 0
        progress = min(100, int((elapsed / self.config.calibration_time) * 100)) if self.calibration_start else 0
        payload = {
            'zone_name': self.name,
            'state': self.state.name.lower(),
            'progress': progress,
            'elapsed': int(elapsed),
            'total': self.config.calibration_time,
            'devices': {ieee: stats.to_dict() for ieee, stats in self.devices.items()},
        }
        try:
            self._calibration_callback(payload)
        except Exception as e:
            logger.debug(f"Calibration callback error: {e}")

    # ------------------------------------------------------------------ #
    def evaluate(self) -> ZoneState:
        """Run one evaluation tick."""
        if self.state == ZoneState.CALIBRATING:
            self.check_calibration()
            return self.state
        if self.state == ZoneState.UNCALIBRATED:
            return self.state

        # Stale-sample safety: if a device hasn't transmitted recently, its RSSI
        # isn't fresh enough to contribute. We only use samples seen in the last
        # 60s (mains routers) or 180s (end devices, which are inherently slower).
        now = time.time()
        weighted_triggered = 0.0
        active = 0

        for stats in self.devices.values():
            dev = stats.to_dict()
            dev_ieee_short = stats.ieee[-8:]

            if stats.last_seen is None:
                continue
            max_age = 60 if stats.is_router else 180
            if (now - stats.last_seen) > max_age:
                continue

            deviation = stats.get_deviation()
            if deviation is None:
                continue
            active += 1

            # Per-device σ threshold
            threshold = self.config.deviation_threshold * stats.aggressiveness

            if deviation > threshold:
                weight = 1.0 if stats.is_router else self.config.end_device_weight
                weighted_triggered += weight
                logger.debug(
                    f"[{self.name}] {dev_ieee_short} deviation={deviation:.2f}σ "
                    f"threshold={threshold:.2f}σ weight={weight}"
                )

        is_present = weighted_triggered >= self.config.min_devices_triggered

        if is_present:
            self.last_trigger_time = now
            if self.state != ZoneState.OCCUPIED:
                self.state = ZoneState.OCCUPIED
                self.occupied_since = now
                logger.info(
                    f"Zone '{self.name}' -> OCCUPIED "
                    f"(weighted={weighted_triggered:.2f}, active={active})"
                )
                if self.on_occupied:
                    self.on_occupied(self)
        else:
            if self.state == ZoneState.OCCUPIED:
                if self.last_trigger_time and (now - self.last_trigger_time) >= self.config.clear_delay:
                    self.state = ZoneState.VACANT
                    self.last_clear_time = now
                    duration = (now - self.occupied_since) if self.occupied_since else 0
                    logger.info(f"Zone '{self.name}' -> VACANT (was occupied {duration:.0f}s)")
                    self.occupied_since = None
                    if self.on_vacant:
                        self.on_vacant(self)

        return self.state

    # ------------------------------------------------------------------ #
    def set_device_aggressiveness(self, ieee: str, value: float) -> bool:
        """
        Set per-device σ multiplier. Rejects non-router devices.
        Returns True on success, False if device is not a router.
        """
        ieee = normalize_ieee(ieee)
        if ieee not in self.device_ieees:
            return False
        stats = self._ensure_device(ieee)
        if not stats.is_router:
            logger.warning(f"Refusing aggressiveness change on non-router {ieee}")
            return False
        value = max(0.5, min(2.0, float(value)))
        stats.aggressiveness = value
        self.config.per_device_aggressiveness[ieee] = value
        logger.info(f"Zone '{self.name}' device {ieee[-8:]} aggressiveness={value:.2f}σ")
        return True

    def recalibrate(self) -> None:
        """Drop baselines and enter UNCALIBRATED. User must start calibration."""
        self.state = ZoneState.UNCALIBRATED
        self.calibration_start = None
        self._last_progress = 0
        for stats in self.devices.values():
            stats.clear_baseline()
            stats.samples.clear()
            stats._smoothed_rssi = None
        logger.info(f"Zone '{self.name}' reset to UNCALIBRATED")

    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'state': self.state.name.lower(),
            'device_ieees': self.device_ieees,
            'device_count': len(self.device_ieees),
            'occupied_since': self.occupied_since,
            'calibration_start': self.calibration_start,
            'config': {
                'deviation_threshold': self.config.deviation_threshold,
                'min_devices_triggered': self.config.min_devices_triggered,
                'clear_delay': self.config.clear_delay,
                'calibration_time': self.config.calibration_time,
                'end_device_weight': self.config.end_device_weight,
            },
            'devices': {ieee: stats.to_dict() for ieee, stats in self.devices.items()},
        }


# =============================================================================
#   ZoneManager
# =============================================================================
class ZoneManager:
    """Manages multiple presence-detection zones."""

    def __init__(self, app_controller=None, mqtt_handler=None, event_emitter=None):
        self.zones: Dict[str, Zone] = {}
        self.app_controller = app_controller
        self.mqtt_handler = mqtt_handler
        self._event_emitter = event_emitter
        self._running = False
        self._device_to_zones: Dict[str, List[str]] = {}
        self._evaluation_task: Optional[asyncio.Task] = None
        self._zigbee_service = None

    # ------------------------------------------------------------------ #
    def create_zone(self, config: ZoneConfig) -> Zone:
        config.device_ieees = [normalize_ieee(i) for i in config.device_ieees]
        config.per_device_aggressiveness = {
            normalize_ieee(k): float(v) for k, v in (config.per_device_aggressiveness or {}).items()
        }
        zone = Zone(
            config=config,
            on_occupied=self._on_zone_occupied,
            on_vacant=self._on_zone_vacant,
        )
        zone._calibration_callback = self._emit_calibration_progress
        zone._app_controller = self.app_controller

        self.zones[config.name] = zone
        for ieee in config.device_ieees:
            self._device_to_zones.setdefault(ieee, []).append(config.name)
            zone._ensure_device(ieee)

        logger.info(f"Created zone '{config.name}' with {len(config.device_ieees)} devices")
        return zone

    def remove_zone(self, zone_name: str) -> bool:
        if zone_name not in self.zones:
            return False
        zone = self.zones[zone_name]
        for ieee in zone.device_ieees:
            if ieee in self._device_to_zones:
                try:
                    self._device_to_zones[ieee].remove(zone_name)
                except ValueError:
                    pass
                if not self._device_to_zones[ieee]:
                    del self._device_to_zones[ieee]
        del self.zones[zone_name]
        logger.info(f"Removed zone '{zone_name}'")
        return True

    def get_zone(self, zone_name: str) -> Optional[Zone]:
        return self.zones.get(zone_name)

    def list_zones(self) -> List[Dict[str, Any]]:
        return [zone.to_dict() for zone in self.zones.values()]

    # ------------------------------------------------------------------ #
    def record_device_rssi(self, ieee: str, rssi: int, lqi: int) -> None:
        """
        PUBLIC ENTRY POINT from zones_handler.
        Routes a single device's sample into every zone that contains it.
        """
        norm = normalize_ieee(ieee)
        zones = self._device_to_zones.get(norm, [])
        if not zones:
            return
        for zone_name in zones:
            zone = self.zones.get(zone_name)
            if zone:
                zone.record_rssi(norm, rssi, lqi)

    # Back-compat shim: handlers that still pass (src, dst, rssi, lqi) work.
    # The coordinator side is discarded — we only care about the zone device.
    def record_link_quality(self, source_ieee: str, target_ieee: str,
                            rssi: int, lqi: int) -> None:
        coord_ieee = None
        if self.app_controller is not None:
            try:
                coord_ieee = normalize_ieee(self.app_controller.ieee)
            except Exception:
                coord_ieee = None
        s = normalize_ieee(source_ieee)
        t = normalize_ieee(target_ieee)
        # Pick the non-coordinator side as the "device"
        device_ieee = t if s == coord_ieee else s
        if device_ieee:
            self.record_device_rssi(device_ieee, rssi, lqi)

    # ------------------------------------------------------------------ #
    async def start_zone(self) -> None:
        self._running = True
        self._evaluation_task = asyncio.create_task(self._evaluation_loop())
        # Ensure every zone's device roles are populated now that app is ready
        for zone in self.zones.values():
            zone.refresh_device_roles()
        logger.info("Zone manager started")

    async def stop_zone(self) -> None:
        self._running = False
        if self._evaluation_task:
            self._evaluation_task.cancel()
            try:
                await self._evaluation_task
            except asyncio.CancelledError:
                pass
        logger.info("Zone manager stopped")

    # ------------------------------------------------------------------ #
    async def _evaluation_loop(self) -> None:
        last_broadcast = 0.0
        while self._running:
            try:
                await asyncio.sleep(2)
                now = time.time()
                should_broadcast = (now - last_broadcast) >= 5.0
                for zone in self.zones.values():
                    zone.evaluate()
                    if should_broadcast:
                        asyncio.create_task(self._publish_zone_state(zone))
                if should_broadcast:
                    last_broadcast = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Evaluation loop error: {e}")
                await asyncio.sleep(2)

    # ------------------------------------------------------------------ #
    def _on_zone_occupied(self, zone: Zone) -> None:
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))
        if self._event_emitter:
            asyncio.create_task(self._event_emitter('zone_state', {
                'zone_name': zone.name, 'state': 'occupied'
            }))

    def _on_zone_vacant(self, zone: Zone) -> None:
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))
        if self._event_emitter:
            asyncio.create_task(self._event_emitter('zone_state', {
                'zone_name': zone.name, 'state': 'vacant'
            }))

    def _emit_calibration_progress(self, data: dict) -> None:
        if self._event_emitter:
            try:
                asyncio.create_task(self._event_emitter('zone_calibration', data))
            except Exception as e:
                logger.debug(f"Failed to emit calibration progress: {e}")

    async def _publish_zone_state(self, zone: Zone) -> None:
        # WebSocket
        if self._event_emitter:
            await self._event_emitter('zone_update', {'zone': zone.to_dict()})
        # MQTT
        if self.mqtt_handler:
            topic = (zone.config.mqtt_topic_override
                     or f"zigbee/zone/{zone.name.lower().replace(' ', '_')}")
            payload = {
                'occupancy': zone.state == ZoneState.OCCUPIED,
                'state': zone.state.name.lower(),
            }
            try:
                await self.mqtt_handler.publish(f"{topic}/state", json.dumps(payload))
            except Exception as e:
                logger.error(f"Failed to publish zone state: {e}")

    async def publish_discovery(self, zone: Zone) -> None:
        """Publish MQTT discovery for a zone."""
        if not self.mqtt_handler:
            return
        node_id = normalize_ieee(zone.name).replace(":", "_").replace(" ", "_")
        topic = f"homeassistant/binary_sensor/{node_id}/occupancy/config"
        config = {
            "name": f"{zone.name} Occupancy",
            "unique_id": f"zone_{node_id}_occupancy",
            "state_topic": (zone.config.mqtt_topic_override
                            or f"zigbee/zone/{zone.name.lower().replace(' ', '_')}") + "/state",
            "value_template": "{{ 'ON' if value_json.occupancy else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "occupancy",
            "device": {
                "identifiers": [f"zone_{node_id}"],
                "name": f"Zone: {zone.name}",
                "model": "Presence Detection Zone",
                "manufacturer": "ZigBee Manager",
            },
        }
        try:
            await self.mqtt_handler.publish(topic, json.dumps(config), retain=True, qos=1)
            logger.info(f"Published discovery for zone '{zone.name}'")
        except Exception as e:
            logger.error(f"Failed to publish discovery: {e}")

    # ------------------------------------------------------------------ #
    def load_config(self, configs: List[Dict[str, Any]]) -> None:
        for cfg in configs:
            try:
                zc = ZoneConfig(
                    name=cfg['name'],
                    device_ieees=cfg.get('device_ieees', []),
                    deviation_threshold=cfg.get('deviation_threshold', 2.5),
                    min_devices_triggered=cfg.get('min_devices_triggered',
                                                  cfg.get('min_links_triggered', 1.5)),
                    clear_delay=cfg.get('clear_delay', 15),
                    calibration_time=cfg.get('calibration_time', 120),
                    end_device_weight=cfg.get('end_device_weight', 0.5),
                    mqtt_topic_override=cfg.get('mqtt_topic_override'),
                    per_device_aggressiveness=cfg.get('per_device_aggressiveness', {}),
                )
                self.create_zone(zc)
            except Exception as e:
                logger.error(f"Failed to load zone config: {e}")

    def save_config(self) -> List[Dict[str, Any]]:
        out = []
        for zone in self.zones.values():
            out.append({
                'name': zone.config.name,
                'device_ieees': zone.config.device_ieees,
                'deviation_threshold': zone.config.deviation_threshold,
                'min_devices_triggered': zone.config.min_devices_triggered,
                'clear_delay': zone.config.clear_delay,
                'calibration_time': zone.config.calibration_time,
                'end_device_weight': zone.config.end_device_weight,
                'mqtt_topic_override': zone.config.mqtt_topic_override,
                'per_device_aggressiveness': zone.config.per_device_aggressiveness,
            })
        return out

    # ------------------------------------------------------------------ #
    async def configure_zone_devices(self, zigbee_service):
        """
        Configure aggressive LQI reporting ONLY on routers that have a
        per-device aggressiveness entry set (i.e. user-enabled).
        End devices are always skipped; routers without an explicit
        aggressiveness entry keep baseline reporting.
        """
        from modules.zone_device_config import configure_zone_device_reporting
        self._zigbee_service = zigbee_service

        opted_in = set()
        for zone in self.zones.values():
            for ieee in zone.config.per_device_aggressiveness.keys():
                opted_in.add(ieee)

        if not opted_in:
            logger.info("No routers opted into aggressive RSSI reporting")
            return

        logger.info(f"Configuring {len(opted_in)} routers for aggressive LQI reporting")
        await configure_zone_device_reporting(zigbee_service, list(opted_in))