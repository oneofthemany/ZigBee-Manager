"""
Zones - RSSI-based presence detection for ZigBee-Manager.

Creates virtual zones from groups of devices and monitors RSSI fluctuations
between mesh nodes to detect human presence without dedicated sensors.
"""

import asyncio
import logging
import time
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any, Callable
from collections import deque
from enum import Enum, auto

logger = logging.getLogger(__name__)


class ZoneState(Enum):
    """Zone occupancy state."""
    VACANT = auto()
    OCCUPIED = auto()
    CALIBRATING = auto()


@dataclass
class RSSISample:
    """Single RSSI measurement between two nodes."""
    source_ieee: str
    target_ieee: str
    rssi: int
    lqi: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class LinkStats:
    """Statistics for a single mesh link."""
    source_ieee: str
    target_ieee: str
    samples: deque = field(default_factory=lambda: deque(maxlen=120))  # ~2 min at 1/sec
    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None
    last_rssi: Optional[int] = None
    last_lqi: Optional[int] = None

    def add_sample(self, rssi: int, lqi: int) -> None:
        """Add a new RSSI/LQI sample."""
        self.samples.append(RSSISample(
            source_ieee=self.source_ieee,
            target_ieee=self.target_ieee,
            rssi=rssi,
            lqi=lqi
        ))
        self.last_rssi = rssi
        self.last_lqi = lqi

    def compute_baseline(self) -> bool:
        """Compute baseline from current samples. Returns True if sufficient data."""
        if len(self.samples) < 30:  # Need at least 30 samples
            return False

        rssi_values = [s.rssi for s in self.samples]
        self.baseline_mean = statistics.mean(rssi_values)
        self.baseline_std = statistics.stdev(rssi_values) if len(rssi_values) > 1 else 1.0

        # Minimum std to avoid false positives in very stable environments
        if self.baseline_std < 1.0:
            self.baseline_std = 1.0

        return True

    def get_deviation(self) -> Optional[float]:
        """Get current deviation from baseline in standard deviations."""
        if self.baseline_mean is None or self.last_rssi is None:
            return None
        return abs(self.last_rssi - self.baseline_mean) / self.baseline_std

    def get_recent_variance(self, window: int = 10) -> Optional[float]:
        """Get variance of recent samples."""
        if len(self.samples) < window:
            return None
        recent = list(self.samples)[-window:]
        rssi_values = [s.rssi for s in recent]
        return statistics.variance(rssi_values) if len(rssi_values) > 1 else 0.0


@dataclass
class ZoneConfig:
    """Configuration for a single zone."""
    name: str
    device_ieees: List[str]

    # Detection thresholds
    deviation_threshold: float = 2.5  # Std devs from baseline to trigger
    variance_threshold: float = 4.0   # Variance threshold for fluctuation
    min_links_triggered: int = 2      # Min links showing fluctuation

    # Timing
    calibration_time: int = 120       # Seconds to calibrate baseline
    clear_delay: int = 30             # Seconds of stability before clearing
    sample_interval: float = 1.0      # Seconds between samples

    # MQTT
    mqtt_topic_override: Optional[str] = None


@dataclass
class Zone:
    """A presence detection zone."""
    config: ZoneConfig
    state: ZoneState = ZoneState.CALIBRATING
    links: Dict[str, LinkStats] = field(default_factory=dict)

    # State tracking
    calibration_start: Optional[float] = None
    last_trigger_time: Optional[float] = None
    last_clear_time: Optional[float] = None
    occupied_since: Optional[float] = None

    # Callbacks
    on_occupied: Optional[Callable[['Zone'], None]] = None
    on_vacant: Optional[Callable[['Zone'], None]] = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def device_ieees(self) -> List[str]:
        return self.config.device_ieees

    def get_link_key(self, source: str, target: str) -> str:
        """Generate consistent key for a link (order-independent)."""
        return f"{min(source, target)}:{max(source, target)}"

    def get_or_create_link(self, source: str, target: str) -> LinkStats:
        """Get or create link stats for a device pair."""
        key = self.get_link_key(source, target)
        if key not in self.links:
            self.links[key] = LinkStats(source_ieee=source, target_ieee=target)
        return self.links[key]

    def record_rssi(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        """Record an RSSI measurement for this zone."""
        # Only track links between zone devices
        if source_ieee not in self.device_ieees or target_ieee not in self.device_ieees:
            return

        link = self.get_or_create_link(source_ieee, target_ieee)
        link.add_sample(rssi, lqi)

    def check_calibration(self) -> bool:
        """Check if calibration is complete. Returns True if zone is ready."""
        if self.state != ZoneState.CALIBRATING:
            return True

        if self.calibration_start is None:
            self.calibration_start = time.time()
            return False

        elapsed = time.time() - self.calibration_start
        if elapsed < self.config.calibration_time:
            return False

        # Attempt to compute baselines for all links
        ready_links = 0
        for link in self.links.values():
            if link.compute_baseline():
                ready_links += 1

        if ready_links >= self.config.min_links_triggered:
            self.state = ZoneState.VACANT
            logger.info(f"Zone '{self.name}' calibrated with {ready_links} links")
            return True

        # Not enough data, extend calibration
        logger.debug(f"Zone '{self.name}' extending calibration, only {ready_links} links ready")
        return False

    def evaluate(self) -> ZoneState:
        """Evaluate current zone state based on RSSI patterns."""
        if self.state == ZoneState.CALIBRATING:
            if not self.check_calibration():
                return self.state

        triggered_links = 0
        high_variance_links = 0

        for link in self.links.values():
            deviation = link.get_deviation()
            variance = link.get_recent_variance()

            if deviation is not None and deviation > self.config.deviation_threshold:
                triggered_links += 1

            if variance is not None and variance > self.config.variance_threshold:
                high_variance_links += 1

        now = time.time()

        # Check for presence
        is_fluctuating = (triggered_links >= self.config.min_links_triggered or
                          high_variance_links >= self.config.min_links_triggered)

        if is_fluctuating:
            self.last_trigger_time = now

            if self.state != ZoneState.OCCUPIED:
                self.state = ZoneState.OCCUPIED
                self.occupied_since = now
                logger.info(f"Zone '{self.name}' -> OCCUPIED (links: {triggered_links}, variance: {high_variance_links})")
                if self.on_occupied:
                    self.on_occupied(self)
        else:
            # Check clear delay
            if self.state == ZoneState.OCCUPIED:
                if self.last_trigger_time:
                    stable_duration = now - self.last_trigger_time
                    if stable_duration >= self.config.clear_delay:
                        self.state = ZoneState.VACANT
                        self.last_clear_time = now
                        duration = now - self.occupied_since if self.occupied_since else 0
                        logger.info(f"Zone '{self.name}' -> VACANT (was occupied {duration:.0f}s)")
                        self.occupied_since = None
                        if self.on_vacant:
                            self.on_vacant(self)

        return self.state

    def to_dict(self) -> Dict[str, Any]:
        """Serialize zone state for API/MQTT."""
        return {
            'name': self.name,
            'state': self.state.name.lower(),
            'device_count': len(self.device_ieees),
            'link_count': len(self.links),
            'occupied_since': self.occupied_since,
            'config': {
                'deviation_threshold': self.config.deviation_threshold,
                'variance_threshold': self.config.variance_threshold,
                'clear_delay': self.config.clear_delay,
            },
            'links': {
                key: {
                    'last_rssi': link.last_rssi,
                    'last_lqi': link.last_lqi,
                    'baseline_mean': link.baseline_mean,
                    'baseline_std': link.baseline_std,
                    'deviation': link.get_deviation(),
                    'variance': link.get_recent_variance(),
                    'sample_count': len(link.samples),
                }
                for key, link in self.links.items()
            }
        }

    def recalibrate(self) -> None:
        """Force recalibration of the zone."""
        self.state = ZoneState.CALIBRATING
        self.calibration_start = None
        for link in self.links.values():
            link.samples.clear()
            link.baseline_mean = None
            link.baseline_std = None
        logger.info(f"Zone '{self.name}' recalibration started")


class ZoneManager:
    """Manages all presence detection zones."""

    def __init__(self, app_controller=None, mqtt_handler=None):
        self.zones: Dict[str, Zone] = {}
        self.app_controller = app_controller
        self.mqtt_handler = mqtt_handler
        self._running = False
        self._sample_task: Optional[asyncio.Task] = None
        self._eval_task: Optional[asyncio.Task] = None

        # Map device IEEE to zones for fast lookup
        self._device_to_zones: Dict[str, Set[str]] = {}

    def create_zone(self, config: ZoneConfig) -> Zone:
        """Create and register a new zone."""
        zone = Zone(
            config=config,
            on_occupied=self._on_zone_occupied,
            on_vacant=self._on_zone_vacant,
        )
        self.zones[config.name] = zone

        # Update device->zone mapping
        for ieee in config.device_ieees:
            if ieee not in self._device_to_zones:
                self._device_to_zones[ieee] = set()
            self._device_to_zones[ieee].add(config.name)

        logger.info(f"Created zone '{config.name}' with {len(config.device_ieees)} devices")
        return zone

    def remove_zone(self, name: str) -> bool:
        """Remove a zone by name."""
        if name not in self.zones:
            return False

        zone = self.zones.pop(name)

        # Clean up device->zone mapping
        for ieee in zone.device_ieees:
            if ieee in self._device_to_zones:
                self._device_to_zones[ieee].discard(name)
                if not self._device_to_zones[ieee]:
                    del self._device_to_zones[ieee]

        logger.info(f"Removed zone '{name}'")
        return True

    def get_zone(self, name: str) -> Optional[Zone]:
        """Get a zone by name."""
        return self.zones.get(name)

    def record_link_quality(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        """Record RSSI/LQI from any message between devices."""
        # Find zones containing both devices
        source_zones = self._device_to_zones.get(source_ieee, set())
        target_zones = self._device_to_zones.get(target_ieee, set())
        common_zones = source_zones & target_zones

        for zone_name in common_zones:
            zone = self.zones.get(zone_name)
            if zone:
                zone.record_rssi(source_ieee, target_ieee, rssi, lqi)

    async def start(self) -> None:
        """Start the zone manager background tasks."""
        if self._running:
            return

        self._running = True
        self._sample_task = asyncio.create_task(self._sample_loop())
        self._eval_task = asyncio.create_task(self._eval_loop())
        logger.info("Zone manager started")

    async def stop(self) -> None:
        """Stop the zone manager."""
        self._running = False

        if self._sample_task:
            self._sample_task.cancel()
            try:
                await self._sample_task
            except asyncio.CancelledError:
                pass

        if self._eval_task:
            self._eval_task.cancel()
            try:
                await self._eval_task
            except asyncio.CancelledError:
                pass

        logger.info("Zone manager stopped")

    async def _sample_loop(self) -> None:
        """Periodically request LQI data from devices."""
        while self._running:
            try:
                await self._collect_neighbor_data()
                await asyncio.sleep(1.0)  # Sample every second
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sample loop: {e}")
                await asyncio.sleep(5.0)

    async def _eval_loop(self) -> None:
        """Periodically evaluate all zones."""
        while self._running:
            try:
                for zone in self.zones.values():
                    zone.evaluate()
                await asyncio.sleep(0.5)  # Evaluate twice per second
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in eval loop: {e}")
                await asyncio.sleep(1.0)

    async def _collect_neighbor_data(self) -> None:
        """Collect neighbor/LQI data from the coordinator and routers."""
        if not self.app_controller:
            return

        try:
            # Get neighbor table from coordinator
            neighbors = await self._get_neighbors(self.app_controller.ieee)

            for neighbor in neighbors:
                self.record_link_quality(
                    source_ieee=str(self.app_controller.ieee),
                    target_ieee=str(neighbor.get('ieee', '')),
                    rssi=neighbor.get('rssi', 0),
                    lqi=neighbor.get('lqi', 0),
                )

            # Also poll routers in zones for their neighbor tables
            router_ieees = set()
            for zone in self.zones.values():
                for ieee in zone.device_ieees:
                    device = self.app_controller.devices.get(ieee)
                    if device and device.node_desc and device.node_desc.is_router:
                        router_ieees.add(ieee)

            for ieee in router_ieees:
                try:
                    neighbors = await self._get_neighbors(ieee)
                    for neighbor in neighbors:
                        self.record_link_quality(
                            source_ieee=str(ieee),
                            target_ieee=str(neighbor.get('ieee', '')),
                            rssi=neighbor.get('rssi', 0),
                            lqi=neighbor.get('lqi', 0),
                        )
                except Exception as e:
                    logger.debug(f"Failed to get neighbors from {ieee}: {e}")

        except Exception as e:
            logger.debug(f"Error collecting neighbor data: {e}")

    async def _get_neighbors(self, ieee: str) -> List[Dict[str, Any]]:
        """Get neighbor table from a device via ZDO Mgmt_Lqi_req."""
        if not self.app_controller:
            return []

        device = self.app_controller.devices.get(ieee)
        if not device:
            return []

        neighbors = []
        start_index = 0

        while True:
            try:
                # ZDO Mgmt_Lqi_req
                status, count, start, neighbor_list = await device.zdo.Mgmt_Lqi_req(start_index)

                if status != 0:
                    break

                for neighbor in neighbor_list:
                    neighbors.append({
                        'ieee': str(neighbor.ieee),
                        'nwk': neighbor.nwk,
                        'lqi': neighbor.lqi,
                        'rssi': self._lqi_to_rssi(neighbor.lqi),
                        'device_type': neighbor.device_type,
                        'depth': neighbor.depth,
                    })

                if start + len(neighbor_list) >= count:
                    break

                start_index = start + len(neighbor_list)

            except Exception as e:
                logger.debug(f"Mgmt_Lqi_req failed: {e}")
                break

        return neighbors

    def _lqi_to_rssi(self, lqi: int) -> int:
        """Convert LQI to approximate RSSI (dBm)."""
        # This is an approximation - actual conversion depends on radio stack
        # Using linear mapping: LQI 255 -> -30dBm, LQI 0 -> -100dBm
        return int(-100 + (lqi / 255) * 70)

    def _on_zone_occupied(self, zone: Zone) -> None:
        """Handle zone becoming occupied."""
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

    def _on_zone_vacant(self, zone: Zone) -> None:
        """Handle zone becoming vacant."""
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

    async def _publish_zone_state(self, zone: Zone) -> None:
        """Publish zone state to MQTT."""
        if not self.mqtt_handler:
            return

        # Publish as binary_sensor occupancy
        topic = zone.config.mqtt_topic_override or f"zigbee/zone/{zone.name.lower().replace(' ', '_')}"

        payload = {
            'occupancy': zone.state == ZoneState.OCCUPIED,
            'state': zone.state.name.lower(),
        }

        try:
            await self.mqtt_handler.publish(f"{topic}/state", payload)
        except Exception as e:
            logger.error(f"Failed to publish zone state: {e}")

    async def publish_discovery(self, zone: Zone) -> None:
        """Publish Home Assistant MQTT discovery for a zone."""
        if not self.mqtt_handler:
            return

        safe_name = zone.name.lower().replace(' ', '_')
        unique_id = f"zigbee_zone_{safe_name}"

        discovery_payload = {
            'name': f"{zone.name} Presence",
            'unique_id': unique_id,
            'device_class': 'occupancy',
            'state_topic': f"zigbee/zone/{safe_name}/state",
            'value_template': '{{ value_json.occupancy }}',
            'payload_on': True,
            'payload_off': False,
            'device': {
                'identifiers': [unique_id],
                'name': f"Zone: {zone.name}",
                'model': 'RSSI Presence Zone',
                'manufacturer': 'ZigBee-Manager',
            },
            'json_attributes_topic': f"zigbee/zone/{safe_name}/attributes",
        }

        try:
            await self.mqtt_handler.publish(
                f"homeassistant/binary_sensor/{unique_id}/config",
                discovery_payload,
                retain=True
            )
            logger.info(f"Published HA discovery for zone '{zone.name}'")
        except Exception as e:
            logger.error(f"Failed to publish zone discovery: {e}")

    def list_zones(self) -> List[Dict[str, Any]]:
        """List all zones with their current state."""
        return [zone.to_dict() for zone in self.zones.values()]

    def save_config(self) -> List[Dict[str, Any]]:
        """Export zone configurations for persistence."""
        configs = []
        for zone in self.zones.values():
            configs.append({
                'name': zone.config.name,
                'device_ieees': zone.config.device_ieees,
                'deviation_threshold': zone.config.deviation_threshold,
                'variance_threshold': zone.config.variance_threshold,
                'min_links_triggered': zone.config.min_links_triggered,
                'calibration_time': zone.config.calibration_time,
                'clear_delay': zone.config.clear_delay,
                'mqtt_topic_override': zone.config.mqtt_topic_override,
            })
        return configs

    def load_config(self, configs: List[Dict[str, Any]]) -> None:
        """Load zone configurations."""
        for cfg in configs:
            zone_config = ZoneConfig(
                name=cfg['name'],
                device_ieees=cfg['device_ieees'],
                deviation_threshold=cfg.get('deviation_threshold', 2.5),
                variance_threshold=cfg.get('variance_threshold', 4.0),
                min_links_triggered=cfg.get('min_links_triggered', 2),
                calibration_time=cfg.get('calibration_time', 120),
                clear_delay=cfg.get('clear_delay', 30),
                mqtt_topic_override=cfg.get('mqtt_topic_override'),
            )
            self.create_zone(zone_config)