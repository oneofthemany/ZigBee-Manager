"""
Zones API - FastAPI routes for zone management.

Integrates with main.py to expose zone CRUD and status endpoints.
"""

import logging
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- Pydantic Models ---

class ZoneCreateRequest(BaseModel):
    """Request to create a new zone."""
    name: str = Field(..., description="Zone name (e.g., 'Living Room')")
    device_ieees: List[str] = Field(..., description="List of device IEEE addresses in zone")
    deviation_threshold: float = Field(2.5, description="Std deviations from baseline to trigger")
    variance_threshold: float = Field(4.0, description="Variance threshold for fluctuation detection")
    min_links_triggered: int = Field(2, description="Minimum links showing fluctuation")
    calibration_time: int = Field(120, description="Seconds to calibrate baseline")
    clear_delay: int = Field(30, description="Seconds of stability before clearing occupancy")
    mqtt_topic_override: Optional[str] = Field(None, description="Custom MQTT topic")


class ZoneUpdateRequest(BaseModel):
    """Request to update zone configuration."""
    deviation_threshold: Optional[float] = None
    variance_threshold: Optional[float] = None
    min_links_triggered: Optional[int] = None
    clear_delay: Optional[int] = None
    mqtt_topic_override: Optional[str] = None


class ZoneDevicesRequest(BaseModel):
    """Request to add/remove devices from zone."""
    add: List[str] = Field(default_factory=list, description="Device IEEEs to add")
    remove: List[str] = Field(default_factory=list, description="Device IEEEs to remove")


class ZoneResponse(BaseModel):
    """Zone status response."""
    name: str
    state: str
    device_count: int
    link_count: int
    occupied_since: Optional[float]
    config: Dict[str, Any]
    links: Dict[str, Dict[str, Any]]


def register_zone_routes(app, zone_manager_or_getter, device_registry_or_getter=None):
    from fastapi import HTTPException
    from .zones import ZoneConfig

    def get_zm():
        zm = zone_manager_or_getter() if callable(zone_manager_or_getter) else zone_manager_or_getter
        return zm

    def get_devices():
        if device_registry_or_getter is None:
            return None
        return device_registry_or_getter() if callable(device_registry_or_getter) else device_registry_or_getter

    @app.get("/api/zones", tags=["zones"])
    async def list_zones():
        zm = get_zm()
        if not zm:
            return []
        return zm.list_zones()

    @app.post("/api/zones", tags=["zones"])
    async def create_zone(request: ZoneCreateRequest) -> Dict[str, Any]:
        """Create a new presence detection zone."""
        if request.name in zone_manager_or_getter.zones:
            raise HTTPException(status_code=400, detail=f"Zone '{request.name}' already exists")

        # Validate device IEEEs if registry available
        if device_registry_or_getter:
            invalid_ieees = []
            for ieee in request.device_ieees:
                if ieee not in device_registry_or_getter:
                    invalid_ieees.append(ieee)
            if invalid_ieees:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown device IEEEs: {invalid_ieees}"
                )

        if len(request.device_ieees) < 2:
            raise HTTPException(
                status_code=400,
                detail="Zone requires at least 2 devices"
            )

        config = ZoneConfig(
            name=request.name,
            device_ieees=request.device_ieees,
            deviation_threshold=request.deviation_threshold,
            variance_threshold=request.variance_threshold,
            min_links_triggered=request.min_links_triggered,
            calibration_time=request.calibration_time,
            clear_delay=request.clear_delay,
            mqtt_topic_override=request.mqtt_topic_override,
        )

        zone = zone_manager_or_getter.create_zone(config)

        # Publish HA discovery
        await zone_manager_or_getter.publish_discovery(zone)

        return zone.to_dict()

    @app.get("/api/zones/{zone_name}", tags=["zones"])
    async def get_zone(zone_name: str) -> Dict[str, Any]:
        """Get zone details and current status."""
        zone = zone_manager_or_getter.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")
        return zone.to_dict()

    @app.patch("/api/zones/{zone_name}", tags=["zones"])
    async def update_zone(zone_name: str, request: ZoneUpdateRequest) -> Dict[str, Any]:
        """Update zone configuration."""
        zone = zone_manager_or_getter.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        if request.deviation_threshold is not None:
            zone.config.deviation_threshold = request.deviation_threshold
        if request.variance_threshold is not None:
            zone.config.variance_threshold = request.variance_threshold
        if request.min_links_triggered is not None:
            zone.config.min_links_triggered = request.min_links_triggered
        if request.clear_delay is not None:
            zone.config.clear_delay = request.clear_delay
        if request.mqtt_topic_override is not None:
            zone.config.mqtt_topic_override = request.mqtt_topic_override

        logger.info(f"Updated zone '{zone_name}' config")
        return zone.to_dict()

    @app.delete("/api/zones/{zone_name}", tags=["zones"])
    async def delete_zone(zone_name: str) -> Dict[str, str]:
        """Delete a zone."""
        if not zone_manager_or_getter.remove_zone(zone_name):
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")
        return {"status": "deleted", "zone": zone_name}

    @app.post("/api/zones/{zone_name}/recalibrate", tags=["zones"])
    async def recalibrate_zone(zone_name: str) -> Dict[str, Any]:
        """Force zone recalibration."""
        zone = zone_manager_or_getter.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        zone.recalibrate()
        return {"status": "recalibrating", "zone": zone.to_dict()}

    @app.post("/api/zones/{zone_name}/devices", tags=["zones"])
    async def modify_zone_devices(zone_name: str, request: ZoneDevicesRequest) -> Dict[str, Any]:
        """Add or remove devices from a zone."""
        zone = zone_manager_or_getter.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        # Add devices
        for ieee in request.add:
            if ieee not in zone.config.device_ieees:
                zone.config.device_ieees.append(ieee)
                if ieee not in zone_manager_or_getter._device_to_zones:
                    zone_manager_or_getter._device_to_zones[ieee] = set()
                zone_manager_or_getter._device_to_zones[ieee].add(zone_name)

        # Remove devices
        for ieee in request.remove:
            if ieee in zone.config.device_ieees:
                zone.config.device_ieees.remove(ieee)
                if ieee in zone_manager_or_getter._device_to_zones:
                    zone_manager_or_getter._device_to_zones[ieee].discard(zone_name)

        # Trigger recalibration if devices changed
        if request.add or request.remove:
            zone.recalibrate()

        return zone.to_dict()

    @app.get("/api/zones/{zone_name}/links", tags=["zones"])
    async def get_zone_links(zone_name: str) -> Dict[str, Any]:
        """Get detailed link statistics for a zone."""
        zone = zone_manager_or_getter.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        links_detail = {}
        for key, link in zone.links.items():
            # Get recent samples for graphing
            recent_samples = [
                {
                    'timestamp': s.timestamp,
                    'rssi': s.rssi,
                    'lqi': s.lqi,
                }
                for s in list(link.samples)[-30:]  # Last 30 samples
            ]

            links_detail[key] = {
                'source': link.source_ieee,
                'target': link.target_ieee,
                'last_rssi': link.last_rssi,
                'last_lqi': link.last_lqi,
                'baseline_mean': link.baseline_mean,
                'baseline_std': link.baseline_std,
                'current_deviation': link.get_deviation(),
                'current_variance': link.get_recent_variance(),
                'sample_count': len(link.samples),
                'recent_samples': recent_samples,
            }

        return {
            'zone': zone_name,
            'state': zone.state.name.lower(),
            'links': links_detail,
        }

    @app.get("/api/zones/suggest/{room_name}", tags=["zones"])
    async def suggest_zone_devices(room_name: str) -> Dict[str, Any]:
        """
        Suggest devices for a zone based on room name matching.
        """
        if not device_registry_or_getter:
            # Return empty if registry not ready yet
            return {"room": room_name, "suggested_devices": [], "count": 0}

        suggested = []
        room_lower = room_name.lower()

        for ieee, device in device_registry_or_getter.items():
            # Handle ZigManDevice wrapper vs Raw Zigpy Device
            # If it's a wrapper, we might need to access .zigpy_dev for specific attributes
            zigpy_dev = getattr(device, 'zigpy_dev', device)

            # Friendly name is usually stored in the wrapper or managed externally
            # If your ZigManDevice has a .name or .friendly_name attribute, use it
            name = getattr(device, 'friendly_name', getattr(device, 'name', str(ieee))) or str(ieee)

            if room_lower in name.lower():
                # Safely get model
                model = getattr(zigpy_dev, 'model', 'Unknown')

                # Safely get router status
                node_desc = getattr(zigpy_dev, 'node_desc', None)
                is_router = getattr(node_desc, 'is_router', False) if node_desc else False

                suggested.append({
                    'ieee': ieee,
                    'name': name,
                    'model': model,
                    'is_router': is_router,
                })

        return {
            'room': room_name,
            'suggested_devices': suggested,
            'count': len(suggested),
        }