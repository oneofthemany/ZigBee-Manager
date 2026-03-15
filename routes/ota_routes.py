"""
OTA API Routes
==============
FastAPI routes for firmware update management.
Register with: register_ota_routes(app, lambda: zigbee_service.ota_manager)
"""
import logging
from fastapi import APIRouter, UploadFile, File, FastAPI
from typing import Callable

logger = logging.getLogger("ota_routes")

_get_ota_manager = None


def register_ota_routes(app: FastAPI, ota_manager_getter: Callable):
    """Register OTA routes on the FastAPI app."""
    global _get_ota_manager
    _get_ota_manager = ota_manager_getter
    logger.info("OTA API routes registered")


    @app.get("/api/ota/config")
    async def get_ota_config():
        """Get current OTA configuration and local firmware files."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return {"success": True, **mgr.get_ota_config()}


    @app.get("/api/ota/check/{ieee}")
    async def check_device_update(ieee: str):
        """Check if firmware update is available for a specific device."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        result = await mgr.check_device_update(ieee)
        return {"success": True, **result}


    @app.get("/api/ota/check-all")
    async def check_all_updates():
        """Scan all devices for available firmware updates."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return {"success": True, **(await mgr.check_all_updates())}


    @app.post("/api/ota/update/{ieee}")
    async def start_update(ieee: str, data: dict = None):
        """Trigger firmware update for a device."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        force = (data or {}).get("force", False)
        return await mgr.start_update(ieee, force=force)


    @app.get("/api/ota/status/{ieee}")
    async def get_update_status(ieee: str):
        """Get current update progress for a device."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return {"success": True, **mgr.get_update_status(ieee)}


    @app.post("/api/ota/cancel/{ieee}")
    async def cancel_update(ieee: str):
        """Cancel an in-progress update."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return await mgr.cancel_update(ieee)


    @app.post("/api/ota/notify/{ieee}")
    async def notify_device(ieee: str):
        """Send OTA Image Notify to prompt a device to check for updates."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return await mgr.notify_device(ieee)


    @app.post("/api/ota/upload")
    async def upload_firmware(file: UploadFile = File(...)):
        """Upload a local firmware file to the OTA directory."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}

        allowed_ext = {'.ota', '.zigbee', '.bin', '.ota1', '.sbl-ota'}
        ext = '.' + file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in allowed_ext:
            return {"success": False, "error": f"Invalid file type. Allowed: {', '.join(allowed_ext)}"}

        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            return {"success": False, "error": "File too large (max 10MB)"}

        return await mgr.upload_firmware(file.filename, content)


    @app.delete("/api/ota/firmware/{filename}")
    async def delete_firmware(filename: str):
        """Delete a firmware file from the local OTA directory."""
        mgr = _get_ota_manager()
        if not mgr:
            return {"success": False, "error": "OTA manager not initialised"}
        return await mgr.delete_firmware(filename)