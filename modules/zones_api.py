"""
Zones API - FastAPI routes for zone management.

New endpoints:
  POST   /api/zones/{name}/calibrate/start   -- user triggers when room empty
  POST   /api/zones/{name}/calibrate/stop    -- finalize early
  POST   /api/zones/{name}/calibrate/cancel  -- abort, drop samples
  PUT    /api/zones/{name}/devices/{ieee}/aggressiveness  -- routers only
"""

import logging
from typing import List, Optional, Dict, Any, Callable, Union
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- Pydantic models ---
class ZoneCreateRequest(BaseModel):
    name: str = Field(..., description="Zone name (e.g., 'Living Room')")
    device_ieees: List[str] = Field(..., description="Device IEEEs in zone")
    deviation_threshold: float = Field(2.5, description="Default σ threshold for routers")
    min_devices_triggered: float = Field(1.5, description="Weighted sum required to trigger")
    clear_delay: int = Field(15, description="Seconds of stability before VACANT")
    calibration_time: int = Field(120, description="Seconds to run calibration window")
    end_device_weight: float = Field(0.5, description="Partial weight for end-device triggers")
    mqtt_topic_override: Optional[str] = Field(None)


class ZoneUpdateRequest(BaseModel):
    deviation_threshold: Optional[float] = None
    min_devices_triggered: Optional[float] = None
    clear_delay: Optional[int] = None
    calibration_time: Optional[int] = None
    end_device_weight: Optional[float] = None
    mqtt_topic_override: Optional[str] = None


class ZoneDevicesRequest(BaseModel):
    add: List[str] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)


class AggressivenessRequest(BaseModel):
    value: float = Field(..., ge=0.5, le=2.0,
                         description="σ multiplier; 0.5=very sensitive, 2.0=very relaxed")


# ---------------------------------------------------------------------- #
def register_zone_routes(
        app: FastAPI,
        zone_manager_or_getter: Union[Any, Callable[[], Any]],
        device_registry_or_getter: Union[Dict, Callable[[], Dict]],
):
    from modules.zones import ZoneConfig

    def get_zm():
        return zone_manager_or_getter() if callable(zone_manager_or_getter) else zone_manager_or_getter

    def get_devices():
        return device_registry_or_getter() if callable(device_registry_or_getter) else device_registry_or_getter

    # ------------------------------------------------------------------ #
    @app.get("/api/zones", tags=["zones"])
    async def list_zones():
        zm = get_zm()
        return zm.list_zones() if zm else []

    @app.post("/api/zones", tags=["zones"])
    async def create_zone(request: ZoneCreateRequest) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        if request.name in zm.zones:
            raise HTTPException(400, f"Zone '{request.name}' already exists")
        if len(request.device_ieees) < 1:
            raise HTTPException(400, "Zone requires at least 1 device")

        cfg = ZoneConfig(
            name=request.name,
            device_ieees=request.device_ieees,
            deviation_threshold=request.deviation_threshold,
            min_devices_triggered=request.min_devices_triggered,
            clear_delay=request.clear_delay,
            calibration_time=request.calibration_time,
            end_device_weight=request.end_device_weight,
            mqtt_topic_override=request.mqtt_topic_override,
        )
        zone = zm.create_zone(cfg)
        zone.refresh_device_roles()
        await zm.publish_discovery(zone)
        return zone.to_dict()

    @app.get("/api/zones/{zone_name}", tags=["zones"])
    async def get_zone(zone_name: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        zone.refresh_device_roles()
        return zone.to_dict()

    @app.patch("/api/zones/{zone_name}", tags=["zones"])
    async def update_zone(zone_name: str, request: ZoneUpdateRequest) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        c = zone.config
        if request.deviation_threshold is not None:
            c.deviation_threshold = request.deviation_threshold
        if request.min_devices_triggered is not None:
            c.min_devices_triggered = request.min_devices_triggered
        if request.clear_delay is not None:
            c.clear_delay = request.clear_delay
        if request.calibration_time is not None:
            c.calibration_time = request.calibration_time
        if request.end_device_weight is not None:
            c.end_device_weight = request.end_device_weight
        if request.mqtt_topic_override is not None:
            c.mqtt_topic_override = request.mqtt_topic_override
        logger.info(f"Updated zone '{zone_name}' config")
        return zone.to_dict()

    @app.delete("/api/zones/{zone_name}", tags=["zones"])
    async def delete_zone(zone_name: str) -> Dict[str, str]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        if not zm.remove_zone(zone_name):
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        return {"status": "deleted", "zone": zone_name}

    # ------------------------------------------------------------------ #
    # Calibration control
    # ------------------------------------------------------------------ #
    @app.post("/api/zones/{zone_name}/calibrate/start", tags=["zones"])
    async def calibrate_start(zone_name: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        zone.refresh_device_roles()
        zone.start_calibration()
        return {"status": "calibrating", "zone": zone.to_dict()}

    @app.post("/api/zones/{zone_name}/calibrate/stop", tags=["zones"])
    async def calibrate_stop(zone_name: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        ready = zone.finalize_calibration()
        return {"status": zone.state.name.lower(), "ready_devices": ready, "zone": zone.to_dict()}

    @app.post("/api/zones/{zone_name}/calibrate/cancel", tags=["zones"])
    async def calibrate_cancel(zone_name: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        zone.cancel_calibration()
        return {"status": zone.state.name.lower(), "zone": zone.to_dict()}

    # Legacy alias: /recalibrate now drops baselines and waits for user to start
    @app.post("/api/zones/{zone_name}/recalibrate", tags=["zones"])
    async def recalibrate_zone(zone_name: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        zone.recalibrate()
        return {"status": "uncalibrated", "zone": zone.to_dict()}

    # ------------------------------------------------------------------ #
    # Device membership
    # ------------------------------------------------------------------ #
    @app.post("/api/zones/{zone_name}/devices", tags=["zones"])
    async def modify_zone_devices(zone_name: str, request: ZoneDevicesRequest) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")

        from modules.zones import normalize_ieee
        for ieee in request.add:
            n = normalize_ieee(ieee)
            if n not in zone.config.device_ieees:
                zone.config.device_ieees.append(n)
                zm._device_to_zones.setdefault(n, [])
                if zone_name not in zm._device_to_zones[n]:
                    zm._device_to_zones[n].append(zone_name)
                zone._ensure_device(n)

        for ieee in request.remove:
            n = normalize_ieee(ieee)
            if n in zone.config.device_ieees:
                zone.config.device_ieees.remove(n)
            if n in zm._device_to_zones and zone_name in zm._device_to_zones[n]:
                zm._device_to_zones[n].remove(zone_name)
                if not zm._device_to_zones[n]:
                    del zm._device_to_zones[n]
            zone.devices.pop(n, None)
            zone.config.per_device_aggressiveness.pop(n, None)

        if request.add or request.remove:
            zone.recalibrate()

        zone.refresh_device_roles()
        return zone.to_dict()

    # ------------------------------------------------------------------ #
    # Per-device aggressiveness (mains-only)
    # ------------------------------------------------------------------ #
    @app.put("/api/zones/{zone_name}/devices/{ieee}/aggressiveness", tags=["zones"])
    async def set_device_aggressiveness(zone_name: str, ieee: str,
                                        request: AggressivenessRequest) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        zone.refresh_device_roles()
        if not zone.set_device_aggressiveness(ieee, request.value):
            raise HTTPException(400, "Device is not in zone or is not mains-fed (Router)")

        # Re-apply aggressive reporting for the opted-in routers
        try:
            zigbee_service = getattr(zm, '_zigbee_service', None)
            if zigbee_service:
                await zm.configure_zone_devices(zigbee_service)
        except Exception as e:
            logger.warning(f"Could not re-apply reporting config: {e}")

        return zone.to_dict()

    @app.delete("/api/zones/{zone_name}/devices/{ieee}/aggressiveness", tags=["zones"])
    async def clear_device_aggressiveness(zone_name: str, ieee: str) -> Dict[str, Any]:
        zm = get_zm()
        if not zm:
            raise HTTPException(503, "Zone manager not initialized")
        zone = zm.get_zone(zone_name)
        if not zone:
            raise HTTPException(404, f"Zone '{zone_name}' not found")
        from modules.zones import normalize_ieee
        n = normalize_ieee(ieee)
        zone.config.per_device_aggressiveness.pop(n, None)
        if n in zone.devices:
            zone.devices[n].aggressiveness = 1.0

        # Restore baseline reporting for this device
        try:
            from modules.zone_device_config import remove_aggressive_reporting
            zigbee_service = getattr(zm, '_zigbee_service', None)
            if zigbee_service:
                await remove_aggressive_reporting(zigbee_service, [n])
        except Exception as e:
            logger.warning(f"Could not restore baseline reporting for {n}: {e}")

        return zone.to_dict()

    # ------------------------------------------------------------------ #
    @app.get("/api/zones/suggest/{room_name}", tags=["zones"])
    async def suggest_zone_devices(room_name: str) -> Dict[str, Any]:
        devices = get_devices()
        if not devices:
            return {"room": room_name, "suggested_devices": [], "count": 0}
        suggested = []
        room_lower = room_name.lower()
        for ieee, device in devices.items():
            zigpy_dev = getattr(device, 'zigpy_dev', device)
            name = getattr(device, 'friendly_name',
                           getattr(device, 'name', str(ieee))) or str(ieee)
            if room_lower in name.lower():
                model = getattr(zigpy_dev, 'model', 'Unknown')
                node_desc = getattr(zigpy_dev, 'node_desc', None)
                is_router = getattr(node_desc, 'is_router', False) if node_desc else False
                suggested.append({
                    'ieee': ieee, 'name': name, 'model': model, 'is_router': is_router,
                })
        return {'room': room_name, 'suggested_devices': suggested, 'count': len(suggested)}