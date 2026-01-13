"""
Zones - RSSI-based presence detection for ZigBee-Manager.
Robust version with topology integration matching core.py logic.
Includes dedicated motion.log.
"""

import asyncio
import logging
import time
import statistics
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any, Callable, Tuple
from collections import deque
from enum import Enum, auto
from statistics import mean, stdev

logger = logging.getLogger(__name__)

def setup_motion_logging():
    """Configure a separate file handler for zone/motion events."""
    import os
    from logging.handlers import RotatingFileHandler

    # Try local logs dir first, then system path
    log_dir = "./logs"

    if os.path.exists(log_dir):
        log_file = os.path.join(log_dir, "motion.log")

        # Create handler
        try:
            handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            handler.setLevel(logging.INFO)

            # Add to logger
            logger.addHandler(handler)
            logger.propagate = False # Logs appear ONLY in motion.log, not main system log
            logger.info(f"Motion logging initialized to {log_file}")
        except Exception as e:
            print(f"Failed to create log handler: {e}")

# Initialize immediately
try:
    setup_motion_logging()
except Exception as e:
    print(f"Failed to setup motion logging: {e}")


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
class ZoneConfig:
    """Configuration for a single zone."""
    name: str
    device_ieees: List[str]
    deviation_threshold: float = 2.5
    variance_threshold: float = 15.0
    min_links_triggered: int = 2
    calibration_time: int = 120
    clear_delay: int = 15
    mqtt_topic_override: Optional[str] = None

    # Optional room properties
    room_volume_m3: Optional[float] = None  # For adaptive thresholds
    zone_center: Optional[Tuple[float, float, float]] = None  # (x, y, z) coordinates
    
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
    samples: deque = field(default_factory=lambda: deque(maxlen=100))

    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None

    last_rssi: Optional[int] = None
    last_lqi: Optional[int] = None

    # Moving average state
    _smoothed_rssi: Optional[float] = field(default=None, init=False, repr=False)

    def add_sample(self, rssi: int, lqi: int) -> None:
        self.samples.append(RSSISample(
            source_ieee=self.source_ieee,
            target_ieee=self.target_ieee,
            rssi=rssi,
            lqi=lqi
        ))
        self.last_rssi = rssi
        self.last_lqi = lqi
        self._update_smoothed_rssi()

    def _update_smoothed_rssi(self, window: int = 5) -> None:
        """Calculate moving average of recent RSSI samples."""
        if len(self.samples) < window:
            self._smoothed_rssi = self.last_rssi if self.last_rssi is not None else None
            return

        recent = list(self.samples)[-window:]
        self._smoothed_rssi = sum(s.rssi for s in recent) / len(recent)

    def compute_baseline(self) -> bool:
        """Compute baseline statistics from calibration samples."""
        if len(self.samples) < 30:  # ✅ Changed from 10 to 30
            return False

        # Use MIDDLE 80% of samples to reduce outlier impact
        rssi_values = sorted([s.rssi for s in self.samples])
        trim = int(len(rssi_values) * 0.1)  # Remove top/bottom 10%
        if trim > 0:
            rssi_values = rssi_values[trim:-trim]

        self.baseline_mean = mean(rssi_values)
        self.baseline_std = stdev(rssi_values) if len(rssi_values) > 1 else 1.0

        # Ensure reasonable std dev (2-10 dBm for stable links)
        if self.baseline_std < 2.0:
            self.baseline_std = 2.0

        logger.info(
            f"Baseline computed: μ={self.baseline_mean:.1f}, σ={self.baseline_std:.1f} "
            f"from {len(self.samples)} samples (trimmed to {len(rssi_values)})"
        )

        return True

    def get_deviation(self) -> Optional[float]:
        """Get deviation using SMOOTHED RSSI for stability."""
        if self.baseline_mean is None or self.baseline_std is None:
            return None
        if self._smoothed_rssi is None:
            return None
        if self.baseline_std == 0:
            return 0.0

        deviation = abs(self._smoothed_rssi - self.baseline_mean) / self.baseline_std

        # Log suspicious deviations
        if deviation < 0.5 and abs(self.last_rssi - self.baseline_mean) > 5:
            logger.warning(
                f"Link {self.source_ieee[-8:]}->{self.target_ieee[-8:]}: "
                f"Low deviation ({deviation:.2f}σ) despite RSSI change. "
                f"Last={self.last_rssi}, Smoothed={self._smoothed_rssi:.1f}, "
                f"Baseline={self.baseline_mean:.1f}±{self.baseline_std:.1f}"
            )

        return deviation

    def get_recent_variance(self, window: int = 10) -> Optional[float]:
        """Calculate variance of recent samples."""
        if len(self.samples) < window:
            return None

        recent = list(self.samples)[-window:]
        values = [s.rssi for s in recent]

        if len(values) < 2:
            return None

        return stdev(values)


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

    # Calibration progress tracking
    _calibration_callback: Optional[Callable] = field(default=None, init=False, repr=False)
    _last_progress: int = field(default=0, init=False, repr=False)

    # Device info cache and app controller reference
    _device_cache: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _app_controller: Any = field(default=None, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def device_ieees(self) -> List[str]:
        return self.config.device_ieees

    def get_link_key(self, source: str, target: str) -> str:
        """Get normalized link key."""
        s = normalize_ieee(source)
        t = normalize_ieee(target)
        return f"{min(s, t)}:{max(s, t)}"

    def get_or_create_link(self, source: str, target: str) -> LinkStats:
        """Get or create link statistics."""
        key = self.get_link_key(source, target)
        if key not in self.links:
            self.links[key] = LinkStats(source_ieee=source, target_ieee=target)
        return self.links[key]

    def record_rssi(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        """Record RSSI/LQI measurement for a link."""
        s_norm = normalize_ieee(source_ieee)
        t_norm = normalize_ieee(target_ieee)

        # CRITICAL: Only track INTRA-ZONE links (both devices in zone)
        source_in_zone = s_norm in self.device_ieees
        target_in_zone = t_norm in self.device_ieees

        if not (source_in_zone and target_in_zone):  # must be in zone
            return

        link = self.get_or_create_link(s_norm, t_norm)
        link.add_sample(rssi, lqi)

        if self.state == ZoneState.CALIBRATING:
            logger.info(
                f"CALIB [{self.name}]: {s_norm[-8:]}->{t_norm[-8:]} | "
                f"RSSI: {rssi} (LQI:{lqi}) | Samples: {len(link.samples)}"
            )

    def _is_router(self, ieee: str) -> bool:
        """Check if device is a router (mains-powered relay)."""
        # Check cache first
        if ieee in self._device_cache:
            return self._device_cache[ieee].get('is_router', False)

        # Check if we have app_controller reference
        if not self._app_controller or not hasattr(self._app_controller, 'devices'):
            return False

        # Get device from core.py devices dict
        device = self._app_controller.devices.get(ieee)
        if not device:
            return False

        # Check role
        role = device.get_role()
        is_router = (role == "Router" or role == "Coordinator")

        # Cache result
        self._device_cache[ieee] = {'is_router': is_router}
        return is_router

    def _get_adaptive_threshold(self) -> float:
        """Calculate adaptive deviation threshold based on zone properties."""
        base = self.config.deviation_threshold

        # Scale by room volume if available
        if self.config.room_volume_m3:
            # Larger rooms = higher threshold (person has less RF impact)
            # Reference: 20m³ room uses base threshold
            scaling_factor = self.config.room_volume_m3 / 20.0
            return base * scaling_factor

        return base

    def check_calibration(self) -> bool:
        """Check if calibration is complete."""
        if self.state != ZoneState.CALIBRATING:
            return True

        # Check actual sample counts in links
        valid_links = [l for l in self.links.values() if len(l.samples) > 0]

        if self.calibration_start is None:
            if len(valid_links) > 0:
                self.calibration_start = time.time()
                logger.info(f"Zone '{self.name}' started calibration timer (active links: {len(valid_links)})")
                self._emit_calibration_update()
            return False

        elapsed = time.time() - self.calibration_start
        progress = min(100, int((elapsed / self.config.calibration_time) * 100))

        # Emit progress updates every 5%
        if progress - self._last_progress >= 5:
            self._last_progress = progress
            self._emit_calibration_update()

        if elapsed < self.config.calibration_time:
            return False

        # Calibration complete - compute baselines
        ready_links = 0
        for link in self.links.values():
            if link.compute_baseline():
                ready_links += 1

        if ready_links > 0:
            self.state = ZoneState.VACANT
            logger.info(f"Zone '{self.name}' calibrated with {ready_links} links")
            self._emit_calibration_update()
            return True

        return False

    def _emit_calibration_update(self):
        """Emit calibration progress via callback."""
        if not self._calibration_callback:
            return

        elapsed = time.time() - self.calibration_start if self.calibration_start else 0
        progress = min(100, int((elapsed / self.config.calibration_time) * 100))
        ready_links = len([l for l in self.links.values() if l.baseline_mean is not None])

        # Include LIVE link stats for frontend display
        link_stats = {}
        for key, link in self.links.items():
            if len(link.samples) > 0:  # Only include active links
                link_stats[key] = {
                    'last_rssi': link.last_rssi,
                    'last_lqi': link.last_lqi,
                    'smoothed_rssi': link._smoothed_rssi,
                    'sample_count': len(link.samples),
                    'baseline_mean': link.baseline_mean,
                    'baseline_std': link.baseline_std,
                    'deviation': link.get_deviation(),
                }

        self._calibration_callback({
            'zone_name': self.name,
            'state': self.state.name.lower(),
            'progress': progress,
            'elapsed': int(elapsed),
            'total': self.config.calibration_time,
            'link_count': len([l for l in self.links.values() if len(l.samples) > 0]),
            'ready_links': ready_links,
            'links': link_stats
        })

    def evaluate(self) -> ZoneState:
        """Evaluate zone state based on link quality deviations."""
        if self.state == ZoneState.CALIBRATING:
            self.check_calibration()
            return self.state

        triggered_links = 0.0  # Float for weighted scoring
        high_variance_links = 0

        # Get adaptive threshold
        adaptive_threshold = self._get_adaptive_threshold()

        for link in self.links.values():
            deviation = link.get_deviation()
            variance = link.get_recent_variance()

            # Weighted link evaluation
            if deviation is not None and deviation > adaptive_threshold:
                # Check device types for weighting
                src_is_router = self._is_router(link.source_ieee)
                dst_is_router = self._is_router(link.target_ieee)

                weight = 1.0
                if src_is_router and dst_is_router:
                    weight = 2.0  # Router-router: most stable
                elif src_is_router or dst_is_router:
                    weight = 1.5  # Router-end device: medium confidence
                # else: end device-end device: weight = 1.0 (lowest confidence)

                triggered_links += weight

                # Debug logging for triggered links
                logger.debug(
                    f"Zone '{self.name}': Link {link.source_ieee[-8:]}->{link.target_ieee[-8:]} "
                    f"triggered (dev={deviation:.2f}, weight={weight:.1f})"
                )

            if variance is not None and variance > self.config.variance_threshold:
                high_variance_links += 1

        total_triggers = triggered_links

        # Check if enough weighted triggers to indicate presence
        is_fluctuating = (total_triggers >= self.config.min_links_triggered)

        now = time.time()
        if is_fluctuating:
            self.last_trigger_time = now
            if self.state != ZoneState.OCCUPIED:
                self.state = ZoneState.OCCUPIED
                self.occupied_since = now
                logger.info(
                    f"Zone '{self.name}' -> OCCUPIED "
                    f"(weighted triggers: {total_triggers:.1f}, threshold: {self.config.min_links_triggered})"
                )
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
        """Convert zone to dictionary for API."""
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
                'room_volume_m3': self.config.room_volume_m3,
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
                    'min_rssi': min([s.rssi for s in link.samples]) if link.samples else None,
                    'max_rssi': max([s.rssi for s in link.samples]) if link.samples else None,
                    'smoothed_rssi': link._smoothed_rssi,
                }
                for key, link in self.links.items()
            }
        }

    def recalibrate(self) -> None:
        """Reset and restart calibration."""
        self.state = ZoneState.CALIBRATING
        self.calibration_start = None
        self._last_progress = 0
        for link in self.links.values():
            link.samples.clear()
            link.baseline_mean = None
            link.baseline_std = None
            link._smoothed_rssi = None
        logger.info(f"Zone '{self.name}' recalibration started")


    def _calculate_link_weight_with_fresnel(
            self,
            link: LinkStats,
            base_weight: float
    ) -> float:
        """
        Apply Fresnel zone weighting if position data available.
        Links crossing zone center are more sensitive to presence.
        """
        if not self.config.zone_center:
            return base_weight

        # Would need device position data - stub for now
        # In practice, you'd need to maintain a device_positions dict
        # device_positions[ieee] = (x, y, z)

        # For now, just return base weight
        # Full implementation requires position tracking
        return base_weight

class ZoneManager:
    """Manages multiple presence detection zones."""

    def __init__(self, app_controller=None, mqtt_handler=None, event_emitter=None):
        self.zones: Dict[str, Zone] = {}
        self.app_controller = app_controller
        self.mqtt_handler = mqtt_handler
        self._event_emitter = event_emitter
        self._running = False
        self._device_to_zones: Dict[str, List[str]] = {}
        self._collection_task: Optional[asyncio.Task] = None
        self._evaluation_task: Optional[asyncio.Task] = None
        self._topology_logged = False
        self._force_collect = False

    def create_zone(self, config: ZoneConfig) -> Zone:
        """Create a new zone."""
        # Normalize all device IEEEs
        config.device_ieees = [normalize_ieee(i) for i in config.device_ieees]

        zone = Zone(
            config=config,
            on_occupied=self._on_zone_occupied,
            on_vacant=self._on_zone_vacant,
        )

        # Wire calibration callback
        zone._calibration_callback = self._emit_calibration_progress

        # Pass app_controller reference for device type checking
        zone._app_controller = self.app_controller

        self.zones[config.name] = zone

        # Register zone with devices
        for ieee in config.device_ieees:
            if ieee not in self._device_to_zones:
                self._device_to_zones[ieee] = []
            self._device_to_zones[ieee].append(config.name)

        logger.info(f"Created zone '{config.name}' with {len(config.device_ieees)} devices")
        return zone


    def _emit_calibration_progress(self, data: dict):
        """Broadcast calibration updates via core event callback."""
        if self._event_emitter:
            try:
                # Create task to emit asynchronously
                asyncio.create_task(self._event_emitter('zone_calibration', data))
            except Exception as e:
                logger.debug(f"Failed to emit calibration progress: {e}")

    def remove_zone(self, zone_name: str) -> bool:
        """Remove a zone."""
        if zone_name not in self.zones:
            return False

        zone = self.zones[zone_name]

        # Unregister devices
        for ieee in zone.device_ieees:
            if ieee in self._device_to_zones:
                self._device_to_zones[ieee].remove(zone_name)
                if not self._device_to_zones[ieee]:
                    del self._device_to_zones[ieee]

        del self.zones[zone_name]
        logger.info(f"Removed zone '{zone_name}'")
        return True

    def get_zone(self, zone_name: str) -> Optional[Zone]:
        """Get zone by name."""
        return self.zones.get(zone_name)

    def record_link_quality(self, source_ieee: str, target_ieee: str, rssi: int, lqi: int) -> None:
        """Record link quality measurement."""
        s_norm = normalize_ieee(source_ieee)
        t_norm = normalize_ieee(target_ieee)

        # Find zones that include either device
        zones_to_update = set()
        if s_norm in self._device_to_zones:
            zones_to_update.update(self._device_to_zones[s_norm])
        if t_norm in self._device_to_zones:
            zones_to_update.update(self._device_to_zones[t_norm])

        # Diagnostic log (verbose)
        if self._force_collect and zones_to_update:
            logger.info(f"Diagnostic: Found INTRA-ZONE link for {zones_to_update}: {s_norm}<->{t_norm} (LQI:{lqi})")

        # Record measurement in relevant zones
        for zone_name in zones_to_update:
            if zone_name in self.zones:
                self.zones[zone_name].record_rssi(s_norm, t_norm, rssi, lqi)

    async def start(self) -> None:
        """Start background tasks."""
        logger.info("Starting zone manager...")
        self._collection_task = asyncio.create_task(self._collection_loop())
        self._evaluation_task = asyncio.create_task(self._evaluation_loop())

    async def stop(self) -> None:
        """Stop background tasks."""
        logger.info("Stopping zone manager...")
        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass
        if self._evaluation_task:
            self._evaluation_task.cancel()
            try:
                await self._evaluation_task
            except asyncio.CancelledError:
                pass

    async def _collection_loop(self) -> None:
        """Background task to collect neighbor data."""
        while True:
            try:
                await asyncio.sleep(5)  # Collect every 5 seconds
                await self._collect_neighbor_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Collection loop error: {e}")
                await asyncio.sleep(5)

    async def _evaluation_loop(self) -> None:
        """Background task to evaluate zone states."""
        while True:
            try:
                await asyncio.sleep(2)  # Evaluate every 2 seconds
                for zone in self.zones.values():
                    zone.evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Evaluation loop error: {e}")
                await asyncio.sleep(2)


    async def _collect_neighbor_data(self) -> None:
        """Collect RSSI/LQI data from topology."""
        if not self.app_controller:
            return

        links_found = 0

        # Read from topology cache first
        if hasattr(self.app_controller, 'topology') and self.app_controller.topology.neighbors:
            for src_ieee, neighbors in self.app_controller.topology.neighbors.items():
                src_str = normalize_ieee(src_ieee)
                for neighbor in neighbors:
                    dst_str = normalize_ieee(neighbor.ieee)
                    if src_str in self._device_to_zones or dst_str in self._device_to_zones:
                        links_found += 1
                        lqi = getattr(neighbor, 'lqi', 0) or 0
                        rssi = self._lqi_to_rssi(lqi)
                        self.record_link_quality(src_str, dst_str, rssi, lqi)

        # Query neighbor tables directly from routers to get fresh data
        for device_ieee in self._device_to_zones.keys():
            try:
                device = self.app_controller.devices.get(device_ieee)
                if not device:
                    continue

                # Query neighbor table - FIX: Use zigpy_dev
                status, count, start, neighbor_list = await asyncio.wait_for(
                    device.zigpy_dev.zdo.Mgmt_Lqi_req(0),  # ✅ FIXED
                    timeout=2.0
                )

                if status == 0:
                    for n in neighbor_list:
                        neighbor_ieee = normalize_ieee(n.ieee)
                        if neighbor_ieee in self._device_to_zones or device_ieee in self._device_to_zones:
                            self.record_link_quality(
                                device_ieee,
                                neighbor_ieee,
                                self._lqi_to_rssi(n.lqi),
                                n.lqi
                            )
                            links_found += 1
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Failed to query {device_ieee}: {e}")

        if self._force_collect:
            logger.info(f"Collection complete: {links_found} links found")
            self._force_collect = False

    async def _get_neighbors(self, ieee: Any) -> List[Dict[str, Any]]:
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
                # FIX: Use zigpy_dev
                status, count, start, neighbor_list = await device.zigpy_dev.zdo.Mgmt_Lqi_req(start_index)  # ✅ FIXED
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
        """Convert LQI to approximate RSSI."""
        return int(-100 + (lqi / 255) * 70)

    def _on_zone_occupied(self, zone: Zone) -> None:
        """Handle zone occupied event."""
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

        if self._event_emitter:
            asyncio.create_task(self._event_emitter('zone_state', {
                'zone_name': zone.name,
                'state': 'occupied'
            }))

    def _on_zone_vacant(self, zone: Zone) -> None:
        """Handle zone vacant event."""
        if self.mqtt_handler:
            asyncio.create_task(self._publish_zone_state(zone))

        if self._event_emitter:
            asyncio.create_task(self._event_emitter('zone_state', {
                'zone_name': zone.name,
                'state': 'vacant'
            }))

    async def _publish_zone_state(self, zone: Zone) -> None:
        """Publish zone state to MQTT and WebSocket."""
        # 1. WebSocket Broadcast (Frontend Live Updates)
        if self._event_emitter:
            ws_payload = {
                'zone': {
                    'name': zone.name,
                    'state': zone.state.name.lower(),
                    'occupied': zone.state == ZoneState.OCCUPIED,
                    'links': {
                        k: {
                            'last_rssi': l.last_rssi,
                            'deviation': l.get_deviation(),
                            'baseline_mean': l.baseline_mean
                        } for k, l in zone.links.items()
                    }
                }
            }
            await self._event_emitter('zone_update', ws_payload)

        # 2. MQTT Publish (Home Assistant)
        if self.mqtt_handler:
            topic = zone.config.mqtt_topic_override or f"zigbee/zone/{zone.name.lower().replace(' ', '_')}"
            payload = {'occupancy': zone.state == ZoneState.OCCUPIED, 'state': zone.state.name.lower()}
            try:
                await self.mqtt_handler.publish(f"{topic}/state", json.dumps(payload))
            except Exception as e:
                logger.error(f"Failed to publish zone state: {e}")

    async def publish_discovery(self, zone: Zone) -> None:
        """Publish MQTT discovery for a zone."""
        if not self.mqtt_handler:
            return

        node_id = normalize_ieee(zone.name).replace(":", "")
        topic = f"homeassistant/binary_sensor/{node_id}/occupancy/config"

        config = {
            "name": f"{zone.name} Occupancy",
            "unique_id": f"zone_{node_id}_occupancy",
            "state_topic": f"zigbee/zones/{zone.name}/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "occupancy",
            "device": {
                "identifiers": [f"zone_{node_id}"],
                "name": f"Zone: {zone.name}",
                "model": "Presence Detection Zone",
                "manufacturer": "ZigBee Manager"
            }
        }

        try:
            import json
            await self.mqtt_handler.publish(topic, json.dumps(config), retain=True, qos=1)
            logger.info(f"Published discovery for zone '{zone.name}'")
        except Exception as e:
            logger.error(f"Failed to publish discovery: {e}")

    def list_zones(self) -> List[Dict[str, Any]]:
        return [zone.to_dict() for zone in self.zones.values()]

    def load_config(self, configs: List[Dict[str, Any]]) -> None:
        for cfg in configs:
            try:
                zone_config = ZoneConfig(
                    name=cfg['name'],
                    device_ieees=cfg['device_ieees'],
                    deviation_threshold=cfg.get('deviation_threshold', 2.5),
                    variance_threshold=cfg.get('variance_threshold', 15.0),
                    min_links_triggered=cfg.get('min_links_triggered', 2),
                    calibration_time=cfg.get('calibration_time', 120),
                    clear_delay=cfg.get('clear_delay', 15),
                    room_volume_m3=cfg.get('room_volume_m3'),
                    mqtt_topic_override=cfg.get('mqtt_topic_override'),
                )
                self.create_zone(zone_config)
            except Exception as e:
                logger.error(f"Failed to load zone config: {e}")

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
                'room_volume_m3': zone.config.room_volume_m3,
                'mqtt_topic_override': zone.config.mqtt_topic_override,
            })
        return configs

    def force_recalibrate_all(self):
        """Force recalibration and collection immediately."""
        self._force_collect = True
        for zone in self.zones.values():
            zone.recalibrate()