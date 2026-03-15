"""
Zones API - FastAPI routes for zone management.

Integrates with main.py to expose zone CRUD and status endpoints.
"""

import logging
from typing import List, Optional, Dict, Any, Callable, Union
from fastapi import FastAPI, HTTPException, Body
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


def register_zone_routes(
        app: FastAPI,
        zone_manager_or_getter: Union[Any, Callable[[], Any]],
        device_registry_or_getter: Union[Dict, Callable[[], Dict]]
):
    """
    Register API routes for zone management.

    Args:
        app: FastAPI app instance
        zone_manager_or_getter: ZoneManager instance OR a callable returning it
        device_registry_or_getter: Dict of devices OR a callable returning it
    """
    # Lazy import to avoid circular dependency
    from modules.zones import ZoneConfig

    # Helper to resolve lazy dependencies (unwrap lambdas)
    def get_zm():
        if callable(zone_manager_or_getter):
            return zone_manager_or_getter()
        return zone_manager_or_getter

    def get_devices():
        if callable(device_registry_or_getter):
            return device_registry_or_getter()
        return device_registry_or_getter

    @app.get("/api/zones", tags=["zones"])
    async def list_zones():
        zm = get_zm()
        if not zm:
            return []
        return zm.list_zones()

    @app.post("/api/zones", tags=["zones"])
    async def create_zone(request: ZoneCreateRequest) -> Dict[str, Any]:
        """Create a new presence detection zone."""
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        if request.name in zm.zones:
            raise HTTPException(status_code=400, detail=f"Zone '{request.name}' already exists")

        # Validate device IEEEs if registry available
        devices = get_devices()
        if devices:
            invalid_ieees = []
            for ieee in request.device_ieees:
                # Basic check, might need normalization depending on registry keys
                # Assuming registry keys are normalized strings for now
                found = False
                for k in devices.keys():
                    if str(k).lower() == ieee.lower():
                        found = True
                        break
                if not found:
                    invalid_ieees.append(ieee)

            # Warn but don't block? Or block?
            # Blocking helps prevent typos.
            if invalid_ieees:
                logger.warning(f"Creating zone with potentially unknown devices: {invalid_ieees}")

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

        zone = zm.create_zone(config)

        # Publish HA discovery
        await zm.publish_discovery(zone)

        return zone.to_dict()

    @app.get("/api/zones/{zone_name}", tags=["zones"])
    async def get_zone(zone_name: str) -> Dict[str, Any]:
        """Get zone details and current status."""
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")
        return zone.to_dict()

    @app.patch("/api/zones/{zone_name}", tags=["zones"])
    async def update_zone(zone_name: str, request: ZoneUpdateRequest) -> Dict[str, Any]:
        """Update zone configuration."""
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        zone = zm.get_zone(zone_name)
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
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        if not zm.remove_zone(zone_name):
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")
        return {"status": "deleted", "zone": zone_name}

    @app.post("/api/zones/{zone_name}/recalibrate", tags=["zones"])
    async def recalibrate_zone(zone_name: str) -> Dict[str, Any]:
        """Force zone recalibration."""
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        zone.recalibrate()

        # Trigger global diagnostic collection
        if hasattr(zm, 'force_recalibrate_all'):
            zm.force_recalibrate_all()

        return {"status": "recalibrating", "zone": zone.to_dict()}

    @app.post("/api/zones/{zone_name}/devices", tags=["zones"])
    async def modify_zone_devices(zone_name: str, request: ZoneDevicesRequest) -> Dict[str, Any]:
        """Add or remove devices from a zone."""
        zm = get_zm()
        if not zm:
            raise HTTPException(status_code=503, detail="Zone manager not initialized")

        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(status_code=404, detail=f"Zone '{zone_name}' not found")

        # Add devices
        for ieee in request.add:
            norm_ieee = ieee.lower().strip()
            if norm_ieee not in zone.config.device_ieees:
                zone.config.device_ieees.append(norm_ieee)
                if norm_ieee not in zm._device_to_zones:
                    zm._device_to_zones[norm_ieee] = set()
                zm._device_to_zones[norm_ieee].add(zone_name)

        # Remove devices
        for ieee in request.remove:
            norm_ieee = ieee.lower().strip()
            if norm_ieee in zone.config.device_ieees:
                zone.config.device_ieees.remove(norm_ieee)
                if norm_ieee in zm._device_to_zones:
                    zm._device_to_zones[norm_ieee].discard(zone_name)

        # Trigger recalibration if devices changed
        if request.add or request.remove:
            zone.recalibrate()
            if hasattr(zm, 'force_recalibrate_all'):
                zm.force_recalibrate_all()

        return zone.to_dict()

    @app.get("/api/zones/suggest/{room_name}", tags=["zones"])
    async def suggest_zone_devices(room_name: str) -> Dict[str, Any]:
        """
        Suggest devices for a zone based on room name matching.
        """
        devices = get_devices()
        if not devices:
            # Return empty if registry not ready yet
            return {"room": room_name, "suggested_devices": [], "count": 0}

        suggested = []
        room_lower = room_name.lower()

        for ieee, device in devices.items():
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