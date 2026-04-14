"""
Device management routes.
Extracted from main.py.
"""
import logging
from typing import Optional
from fastapi import FastAPI
from models import (
    DeviceRequest, RenameRequest, ConfigureRequest, CommandRequest,
    AttributeReadRequest, BindRequest, PermitJoinRequest,
    BanRequest, UnbanRequest, TouchlinkRequest, DiscoverAttributesRequest
)
from modules.zone_device_config import configure_zone_device_reporting, remove_aggressive_reporting

logger = logging.getLogger("routes.device")


def register_device_routes(app: FastAPI, get_zigbee_service, get_matter_bridge):
    """Register device management routes."""

    @app.get("/api/devices")
    async def get_devices():
        """Get list of all devices with their current state."""
        zigbee_service = get_zigbee_service()
        matter_bridge = get_matter_bridge()
        devices = zigbee_service.get_device_list()
        if matter_bridge and matter_bridge.is_connected:
            devices.extend(matter_bridge.get_device_list())
        return devices

    @app.post("/api/permit_join")
    async def permit_join(request: Optional[PermitJoinRequest] = None):
        """Enable or disable pairing mode."""
        duration = 240
        target = None
        if request:
            duration = request.duration
            target = request.target_ieee
        result = await get_zigbee_service().permit_join(duration, target)
        return {"status": "success", **result}

    @app.get("/api/permit_join")
    async def get_permit_join_status():
        """Get current pairing status."""
        return get_zigbee_service().get_pairing_status()

    # ---- Touchlink ----

    @app.post("/api/touchlink/scan")
    async def touchlink_scan(request: Optional[TouchlinkRequest] = None):
        """Scan for Touchlink devices."""
        channel = request.channel if request else None
        return await get_zigbee_service().touchlink_scan(channel)

    @app.post("/api/touchlink/identify")
    async def touchlink_identify(request: Optional[TouchlinkRequest] = None):
        """Identify Touchlink device(s) - make them blink."""
        channel = request.channel if request else None
        ieee = request.ieee if request else None
        return await get_zigbee_service().touchlink_identify(channel=channel, target_ieee=ieee)

    @app.post("/api/touchlink/reset")
    async def touchlink_reset(request: Optional[TouchlinkRequest] = None):
        """Factory reset Touchlink device(s)."""
        channel = request.channel if request else None
        ieee = request.ieee if request else None
        return await get_zigbee_service().touchlink_factory_reset(channel=channel, target_ieee=ieee)

    # ---- Device Lifecycle ----

    @app.post("/api/device/remove")
    async def remove_device(request: DeviceRequest):
        """Remove a device from the network, optionally banning it."""
        matter_bridge = get_matter_bridge()
        zigbee_service = get_zigbee_service()

        if request.ieee.startswith("matter_") and matter_bridge and matter_bridge.is_connected:
            node_id = int(request.ieee.replace("matter_", ""))
            return await matter_bridge.remove_node(node_id)

        if request.ban:
            zigbee_service.ban_device(request.ieee, reason="Banned on removal")

        result = await zigbee_service.remove_device(request.ieee, force=request.force)
        if request.ban:
            result["banned"] = True
        return result

    @app.post("/api/device/rename")
    async def rename_device(request: RenameRequest):
        """Rename a device."""
        matter_bridge = get_matter_bridge()
        zigbee_service = get_zigbee_service()

        if request.ieee.startswith("matter_") and matter_bridge:
            matter_bridge.rename_device(request.ieee, request.name)
            zigbee_service.friendly_names[request.ieee] = request.name
            zigbee_service._save_json("./data/names.json", zigbee_service.friendly_names)
            return {"success": True, "ieee": request.ieee, "name": request.name}

        return await zigbee_service.rename_device(request.ieee, request.name)

    @app.post("/api/device/reconfigure")
    async def reconfigure_device_endpoint(request: DeviceRequest):
        """Reconfigure device with optional aggressive LQI reporting."""
        logger.info(f"[{request.ieee}] Starting reconfiguration...")
        try:
            zigbee_service = get_zigbee_service()
            if request.ieee not in zigbee_service.devices:
                return {"success": False, "error": "Device not found"}

            device = zigbee_service.devices[request.ieee]
            role = device.get_role()

            # 1. Always run standard config
            await zigbee_service.configure_device(request.ieee)

            # 2. Handle aggressive/baseline LQI reporting
            if request.aggressive is True and role == "Router":
                await configure_zone_device_reporting(zigbee_service, [request.ieee])
                return {"success": True, "message": "Reconfigured with aggressive LQI reporting"}
            elif request.aggressive is False:
                await remove_aggressive_reporting(zigbee_service, request.ieee)
                return {"success": True, "message": "Restored baseline reporting"}

            return {"success": True, "message": "Reconfigured"}
        except Exception as e:
            logger.error(f"[{request.ieee}] Reconfiguration failed: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/device/interview")
    async def interview_device(request: DeviceRequest):
        """Re-interview a device."""
        return await get_zigbee_service().interview_device(request.ieee)

    @app.post("/api/device/poll")
    async def poll_device(request: DeviceRequest):
        """Poll device for current attribute values."""
        return await get_zigbee_service().poll_device(request.ieee)

    # ---- Commands & Attributes ----

    @app.post("/api/device/command")
    async def send_command(request: CommandRequest):
        """Send a command to a device."""
        matter_bridge = get_matter_bridge()
        if request.ieee.startswith("matter_") and matter_bridge and matter_bridge.is_connected:
            node_id = int(request.ieee.replace("matter_", ""))
            return await matter_bridge.send_command(node_id, request.command, request.value)

        return await get_zigbee_service().send_command(
            request.ieee, request.command, request.value, endpoint_id=request.endpoint
        )

    @app.post("/api/device/read_attribute")
    async def read_attribute(request: AttributeReadRequest):
        """Read a specific attribute from a device."""
        return await get_zigbee_service().read_attribute(
            request.ieee, request.endpoint_id, request.cluster_id, request.attribute
        )

    @app.post("/api/device/discover_attributes")
    async def discover_attributes(request: DiscoverAttributesRequest):
        """Discover attributes and their access control on a device cluster."""
        return await get_zigbee_service().discover_cluster_attributes(
            request.ieee, request.endpoint_id, request.cluster_id
        )

    @app.post("/api/device/bind")
    async def bind_devices(request: BindRequest):
        """Bind two devices."""
        return await get_zigbee_service().bind_devices(
            request.source_ieee, request.target_ieee, request.cluster_id
        )

    @app.post("/api/device/configure")
    async def configure_device(request: ConfigureRequest):
        """Update device settings (QoS, polling, reporting, Tuya)."""
        return await get_zigbee_service().configure_device(request.ieee, config=request.dict(exclude_none=True))

    # ---- Banning ----

    @app.post("/api/ban")
    async def ban_device(request: BanRequest):
        """Ban a device by IEEE address."""
        return get_zigbee_service().ban_device(request.ieee, request.reason)

    @app.post("/api/unban")
    async def unban_device(request: UnbanRequest):
        """Remove a device from the ban list."""
        return get_zigbee_service().unban_device(request.ieee)

    @app.get("/api/banned")
    async def get_banned_devices():
        """Get list of all banned IEEE addresses."""
        banned = get_zigbee_service().get_banned_devices()
        return {"banned": banned, "count": len(banned)}

    @app.get("/api/banned/{ieee}")
    async def check_banned(ieee: str):
        """Check if a specific device is banned."""
        return {"ieee": ieee, "banned": get_zigbee_service().is_device_banned(ieee)}

    # ---- Tabs ----

    @app.get("/api/tabs")
    async def get_tabs():
        return get_zigbee_service().get_device_tabs()

    @app.post("/api/tabs")
    async def create_tab(data: dict):
        return get_zigbee_service().create_device_tab(data['name'])

    @app.delete("/api/tabs/{tab_name}")
    async def delete_tab(tab_name: str):
        return get_zigbee_service().delete_device_tab(tab_name)

    @app.post("/api/tabs/{tab_name}/devices")
    async def add_device_to_tab(tab_name: str, data: dict):
        return get_zigbee_service().add_device_to_tab(tab_name, data['ieee'])

    @app.delete("/api/tabs/{tab_name}/devices/{ieee}")
    async def remove_device_from_tab(tab_name: str, ieee: str):
        return get_zigbee_service().remove_device_from_tab(tab_name, ieee)

    # ---- Orphaned Devices ----

    @app.get("/api/devices/orphaned")
    async def get_orphaned_devices():
        """Find devices in database but not active in network."""
        return await get_zigbee_service().find_duplicate_devices()

    @app.post("/api/devices/cleanup-orphaned")
    async def cleanup_orphaned():
        """Remove all orphaned devices from database."""
        return await get_zigbee_service().cleanup_orphaned_devices()

    # ---- Device Overrides ----

    @app.get("/api/device_overrides")
    async def get_device_overrides():
        """Get all device override definitions."""
        from modules.device_overrides import get_override_manager
        mgr = get_override_manager()
        return {
            "success": True,
            "definitions": mgr.list_definitions(),
            "ieee_overrides": mgr.list_ieee_overrides()
        }

    @app.post("/api/device_overrides/definition")
    async def add_device_definition(data: dict):
        """Add/update a model-level device definition."""
        from modules.device_overrides import get_override_manager
        model = data.get("model", "")
        manufacturer = data.get("manufacturer", "")
        definition = data.get("definition", {})
        if not model:
            return {"success": False, "error": "model is required"}
        mgr = get_override_manager()
        mgr.add_definition(model, manufacturer, definition)
        return {"success": True}

    @app.delete("/api/device_overrides/definition")
    async def remove_device_definition(data: dict):
        """Remove a model-level device definition."""
        from modules.device_overrides import get_override_manager
        mgr = get_override_manager()
        result = mgr.remove_definition(data.get("model", ""), data.get("manufacturer", ""))
        return {"success": result}

    @app.post("/api/device_overrides/ieee_mapping")
    async def set_ieee_mapping(data: dict):
        """Set an attribute mapping for a specific device."""
        from modules.device_overrides import get_override_manager
        mgr = get_override_manager()
        mgr.set_ieee_mapping(
            ieee=data["ieee"], raw_key=data["raw_key"],
            friendly_name=data["friendly_name"],
            scale=data.get("scale", 1), unit=data.get("unit", ""),
            device_class=data.get("device_class", "")
        )
        return {"success": True}

    @app.delete("/api/device_overrides/ieee_mapping")
    async def remove_ieee_mapping(data: dict):
        """Remove a per-device attribute mapping."""
        from modules.device_overrides import get_override_manager
        mgr = get_override_manager()
        result = mgr.remove_ieee_mapping(data["ieee"], data["raw_key"])
        return {"success": result}

    @app.get("/api/device_overrides/{ieee}")
    async def get_device_mappings(ieee: str):
        """Get all override mappings active for a specific device."""
        from modules.device_overrides import get_override_manager
        zigbee_service = get_zigbee_service()
        mgr = get_override_manager()

        model = ""
        manufacturer = ""
        if ieee in zigbee_service.devices:
            dev = zigbee_service.devices[ieee]
            model = str(getattr(dev.zigpy_dev, 'model', '') or '')
            manufacturer = str(getattr(dev.zigpy_dev, 'manufacturer', '') or '')

        return {
            "success": True, "ieee": ieee,
            "model": model, "manufacturer": manufacturer,
            "model_definition": mgr.get_definition(model, manufacturer),
            "ieee_mappings": mgr.get_ieee_mappings(ieee),
            "unmapped_keys": [
                k for k in zigbee_service.devices[ieee].state.keys()
                if k.startswith("cluster_")
            ] if ieee in zigbee_service.devices else []
        }