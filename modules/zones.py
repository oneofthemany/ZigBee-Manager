"""
Zones - RSSI-based presence detection for ZigBee-Manager.
Robust version with topology integration matching core.py logic.
"""

import asyncio
import logging
import time
import statistics
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any, Callable
from collections import deque
from enum import Enum, auto

logger = logging.getLogger(__name__)


def normalize_ieee(ieee: Any) -> str:
    """Normalize IEEE to lowercase string with consistent formatting."""
    if ieee is None:
        return ""
    # Convert to string and lowercase
    s = str(ieee).lower().strip()
    # Ensure standard format (add colons if missing and length is 16 hex chars)
    if len(s) == 16 and ":" not in s:
        s = ":".join(s[i:i+2] for i in range(0, 16, 2))
    return s


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
    samples: deque = field(default_factory=lambda: deque(maxlen=120))
    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None
    last_rssi: Optional[int] = None
    last_lqi: Optional[int] = None

    def add_sample(self, rssi: int, lqi: int) -> None:
        self.samples.append(RSSISample(
            source_ieee=self.source_ieee,
            target_ieee=self.target_ieee,
            rssi=rssi,
            lqi=lqi
        ))
        self.last_rssi = rssi
        self.last_lqi = lqi

    def compute_baseline(self) -> bool:
        if len(self.samples) < 30:
            return False
        rssi_values = [s.rssi for s in self.samples]
        self.baseline_mean = statistics.mean(rssi_values)
        self.baseline_std = statistics.stdev(rssi_values) if len(rssi_values) > 1 else 1.0
        if self.baseline_std < 1.0:
            self.baseline_std = 1.0
        return True

    def get_deviation(self) -> Optional[float]:
        if self.baseline_mean is None or self.last_rssi is None:
            return None
        return abs(self.last_rssi - self.baseline_mean) / self.baseline_std

    def get_recent_variance(self, window: int = 10) -> Optional[float]:
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
    deviation_threshold: float = 2.5
    variance_threshold: float = 4.0
    min_links_triggered: int = 2
    calibration_time: int = 120
    clear_delay: int = 30
    mqtt_topic_override: Optional[str] = None


@dataclass
class Zone:
    """A presence detection zone."""
    config: ZoneConfig
    state: ZoneState = ZoneState.CALIBRATING
    links: Dict[str, LinkStats] = field(default_factory=dict)

    calibration_start: Optional[float] = None
    last_trigger_time: Optional[float] = None
    last_clear_time: Optional[float] = None
    occupied_since: Optional[float] = None

    on_occupied: Optional[Callable[['Zone'], None]] = None
    on_vacant: Optional[Callable[['Zone'], None]] = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def device_ieees(self) -> List[str]:
        return self.config.device_ieees

    def get_link_key(self, source: str, target: str) -> str:
        s = normalize_ieee(source)
        t = normalize_ieee(target)
        return f"{min(s, t)}:{max(s, t)}"

    def get_or_create_link(self, source: str, target: str) -> LinkStats:
        key = self.get_link_key(source, target)
        if key not in self.links:
            self.links[key] = LinkStats(source_ieee=source, target_ieee=target)
        return self.links[key]

    def record_rssi(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        s_norm = normalize_ieee(source_ieee)
        t_norm = normalize_ieee(target_ieee)

        # EXPANDED LOGIC: Track link if AT LEAST ONE device is in the zone.
        source_in_zone = s_norm in self.device_ieees
        target_in_zone = t_norm in self.device_ieees

        if not source_in_zone and not target_in_zone:
            return

        link = self.get_or_create_link(s_norm, t_norm)
        link.add_sample(rssi, lqi)

    def check_calibration(self) -> bool:
        if self.state != ZoneState.CALIBRATING:
            return True

        # Check actual sample counts in links, not just if link objects exist
        valid_links = [l for l in self.links.values() if len(l.samples) > 0]

        if self.calibration_start is None:
            if len(valid_links) > 0:
                self.calibration_start = time.time()
                logger.info(f"Zone '{self.name}' started calibration timer (active links: {len(valid_links)})")
            return False

        elapsed = time.time() - self.calibration_start
        if elapsed < self.config.calibration_time:
            return False

        ready_links = 0
        for link in self.links.values():
            if link.compute_baseline():
                ready_links += 1

        if ready_links > 0:
            self.state = ZoneState.VACANT
            logger.info(f"Zone '{self.name}' calibrated with {ready_links} links")
            return True

        return False

    def evaluate(self) -> ZoneState:
        if self.state == ZoneState.CALIBRATING:
            self.check_calibration()
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

        total_triggers = triggered_links

        is_fluctuating = (total_triggers >= self.config.min_links_triggered)

        now = time.time()
        if is_fluctuating:
            self.last_trigger_time = now
            if self.state != ZoneState.OCCUPIED:
                self.state = ZoneState.OCCUPIED
                self.occupied_since = now
                logger.info(f"Zone '{self.name}' -> OCCUPIED (active links: {total_triggers})")
                if self.on_occupied:
                    self.on_occupied(self)
        else:
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
        return {
            'name': self.name,
            'state': self.state.name.lower(),
            'device_ieees': self.device_ieees,
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
        self._device_to_zones: Dict[str, Set[str]] = {}
        self._topology_logged = False
        self._force_collect = False

    def create_zone(self, config: ZoneConfig) -> Zone:
        config.device_ieees = [normalize_ieee(i) for i in config.device_ieees]

        zone = Zone(
            config=config,
            on_occupied=self._on_zone_occupied,
            on_vacant=self._on_zone_vacant,
        )
        self.zones[config.name] = zone

        for ieee in config.device_ieees:
            if ieee not in self._device_to_zones:
                self._device_to_zones[ieee] = set()
            self._device_to_zones[ieee].add(config.name)

        logger.info(f"Created zone '{config.name}' with {len(config.device_ieees)} devices")
        return zone

    def remove_zone(self, name: str) -> bool:
        if name not in self.zones:
            return False
        zone = self.zones.pop(name)
        for ieee in zone.device_ieees:
            if ieee in self._device_to_zones:
                self._device_to_zones[ieee].discard(name)
                if not self._device_to_zones[ieee]:
                    del self._device_to_zones[ieee]
        return True

    def get_zone(self, name: str) -> Optional[Zone]:
        return self.zones.get(name)

    def record_link_quality(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        s_norm = normalize_ieee(source_ieee)
        t_norm = normalize_ieee(target_ieee)

        affected_zones = set()

        if s_norm in self._device_to_zones:
            affected_zones.update(self._device_to_zones[s_norm])

        if t_norm in self._device_to_zones:
            affected_zones.update(self._device_to_zones[t_norm])

        # Diagnostic log (verbose)
        if self._force_collect and not affected_zones:
            # logger.debug(f"Link {s_norm}->{t_norm} ignored (not in any zone)")
            pass

        for zone_name in affected_zones:
            zone = self.zones.get(zone_name)
            if zone:
                zone.record_rssi(s_norm, t_norm, rssi, lqi)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sample_task = asyncio.create_task(self._sample_loop())
        self._eval_task = asyncio.create_task(self._eval_loop())
        logger.info("Zone manager started")

    async def stop(self) -> None:
        self._running = False
        if self._sample_task:
            self._sample_task.cancel()
        if self._eval_task:
            self._eval_task.cancel()

    async def _sample_loop(self) -> None:
        logger.info("Starting Zone sample loop...")
        while self._running:
            try:
                await self._collect_neighbor_data()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sample loop: {e}")
                await asyncio.sleep(5.0)

    async def _eval_loop(self) -> None:
        while self._running:
            try:
                for zone in self.zones.values():
                    zone.evaluate()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in eval loop: {e}")

    async def _collect_neighbor_data(self) -> None:
        """
        Collect link quality data using logic mirroring core.py's connection table builder.
        """
        # Log every call initially (remove after debugging)
        logger.info(f"_collect_neighbor_data called, app_controller={self.app_controller is not None}")

        if not self.app_controller:
            logger.warning("Zone sampling: app_controller is None!")
            return

        # Check topology exists
        has_topology = hasattr(self.app_controller, 'topology')
        has_neighbors = has_topology and hasattr(self.app_controller.topology, 'neighbors')
        #logger.info(f"has_topology={has_topology}, has_neighbors={has_neighbors}")

        if has_neighbors:
            neighbor_count = len(self.app_controller.topology.neighbors)
            #logger.info(f"Topology has {neighbor_count} source nodes")
            #logger.info(f"Zone device mappings: {list(self._device_to_zones.keys())}")

        # Reset diagnostic flag once we have app_controller
        if self._topology_logged:
            logger.info(f"Zone sampling: app_controller is now available")
            self._topology_logged = False

        links_found = 0

        # 1. Read from Cached Topology (primary source)
        if hasattr(self.app_controller, 'topology') and hasattr(self.app_controller.topology, 'neighbors'):
            topology_neighbors = self.app_controller.topology.neighbors

            if self._force_collect:
                #logger.info(f"Topology has {len(topology_neighbors)} source nodes")
                #logger.info(f"Zone devices: {list(self._device_to_zones.keys())}")

                for src_ieee, neighbors in topology_neighbors.items():
                    src_str = normalize_ieee(src_ieee)

                    for neighbor in neighbors:
                        dst_str = normalize_ieee(neighbor.ieee)
                        lqi = getattr(neighbor, 'lqi', 0) or 0
                        rssi = self._lqi_to_rssi(lqi)

                        # Check if either device is in a zone BEFORE recording
                        if src_str in self._device_to_zones or dst_str in self._device_to_zones:
                            links_found += 1
                            self.record_link_quality(src_str, dst_str, rssi, lqi)

                            if self._force_collect:
                                logger.info(f"  Link: {src_str[:17]} <-> {dst_str[:17]} LQI={lqi}")
        else:
            if self._force_collect:
                logger.warning("Topology.neighbors not available!")

        # 2. Active Polling from Coordinator
        try:
            coord_ieee = str(self.app_controller.ieee)
            neighbors = await self._get_neighbors(coord_ieee)

            for neighbor in neighbors:
                target_ieee = normalize_ieee(neighbor.get('ieee', ''))
                if coord_ieee in self._device_to_zones or target_ieee in self._device_to_zones:
                    links_found += 1
                    self.record_link_quality(
                        source_ieee=coord_ieee,
                        target_ieee=target_ieee,
                        rssi=neighbor.get('rssi', 0),
                        lqi=neighbor.get('lqi', 0),
                    )
        except Exception as e:
            if self._force_collect:
                logger.debug(f"Coordinator Mgmt_Lqi_req failed: {e}")

        if self._force_collect:
            logger.info(f"Collection complete: {links_found} relevant links found")
            self._force_collect = False

    async def _get_neighbors(self, ieee: Any) -> List[Dict[str, Any]]:
        # Helper to find device by ieee string or object
        device = None
        if hasattr(self.app_controller, 'devices'):
            device = self.app_controller.devices.get(ieee)
            if not device:
                s_ieee = normalize_ieee(ieee)
                for k, v in self.app_controller.devices.items():
                    if normalize_ieee(k) == s_ieee:
                        device = v
                        break

        if not device:
            return []

        neighbors = []
        start_index = 0

        for _ in range(3):
            try:
                status, count, start, neighbor_list = await device.zdo.Mgmt_Lqi_req(start_index)
                if status != 0: break

                for n in neighbor_list:
                    neighbors.append({
                        'ieee': str(n.ieee),
                        'lqi': n.lqi,
                        'rssi': self._lqi_to_rssi(n.lqi)
                    })

                if start + len(neighbor_list) >= count: break
                start_index = start + len(neighbor_list)
            except Exception:
                break

        return neighbors

    def _lqi_to_rssi(self, lqi: int) -> int:
        return int(-100 + (lqi / 255) * 70)

    def _on_zone_occupied(self, zone: Zone) -> None:
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

    def _on_zone_vacant(self, zone: Zone) -> None:
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

    async def _publish_zone_state(self, zone: Zone) -> None:
        if not self.mqtt_handler: return
        topic = zone.config.mqtt_topic_override or f"zigbee/zone/{zone.name.lower().replace(' ', '_')}"
        payload = {'occupancy': zone.state == ZoneState.OCCUPIED, 'state': zone.state.name.lower()}
        try:
            await self.mqtt_handler.publish(f"{topic}/state", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to publish zone state: {e}")

    async def publish_discovery(self, zone: Zone) -> None:
        if not self.mqtt_handler: return
        safe_name = zone.name.lower().replace(' ', '_')
        unique_id = f"zigbee_zone_{safe_name}"
        discovery_payload = {
            'name': f"{zone.name} Presence",
            'unique_id': unique_id,
            'device_class': 'occupancy',
            'state_topic': f"zigbee/zone/{safe_name}/state",
            'value_template': '{{ value_json.occupancy }}',
            'payload_on': True, 'payload_off': False,
            'device': {
                'identifiers': [unique_id],
                'name': f"Zone: {zone.name}",
                'model': 'RSSI Presence Zone',
                'manufacturer': 'ZigBee-Manager',
            }
        }
        try:
            await self.mqtt_handler.publish(
                f"homeassistant/binary_sensor/{unique_id}/config",
                json.dumps(discovery_payload),
                retain=True
            )
        except Exception as e:
            logger.error(f"Failed to publish zone discovery: {e}")

    def list_zones(self) -> List[Dict[str, Any]]:
        return [zone.to_dict() for zone in self.zones.values()]

    def save_config(self) -> List[Dict[str, Any]]:
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

    def force_recalibrate_all(self):
        """Force recalibration and collection immediately."""
        self._force_collect = True
        for zone in self.zones.values():
            zone.recalibrate()