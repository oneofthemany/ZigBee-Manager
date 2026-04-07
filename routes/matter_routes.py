"""
Matter integration routes.
Extracted from main.py.
"""
import logging
from fastapi import FastAPI
from models import MatterCommissionRequest, MatterRemoveRequest, RenameRequest

logger = logging.getLogger("routes.matter")


def register_matter_routes(app: FastAPI, get_zigbee_service, get_matter_server, get_matter_bridge):
    """Register Matter integration routes."""

    @app.post("/api/matter/commission")
    async def matter_commission(request: MatterCommissionRequest):
        """Commission a Matter device using setup code."""
        matter_bridge = get_matter_bridge()
        if not matter_bridge or not matter_bridge.is_connected:
            return {"success": False, "error": "Matter server not connected"}
        return await matter_bridge.commission(request.code)

    @app.post("/api/matter/remove")
    async def matter_remove(request: MatterRemoveRequest):
        """Remove a Matter device."""
        matter_bridge = get_matter_bridge()
        if not matter_bridge or not matter_bridge.is_connected:
            return {"success": False, "error": "Matter server not connected"}
        return await matter_bridge.remove_node(request.node_id)

    @app.get("/api/matter/status")
    async def matter_status():
        """Get Matter server + bridge status."""
        matter_server = get_matter_server()
        matter_bridge = get_matter_bridge()
        result = {"enabled": False}

        if matter_server:
            result["enabled"] = True
            result["server"] = matter_server.get_status()
            result["mode"] = "embedded"
        elif matter_bridge:
            result["enabled"] = True
            result["mode"] = "external"

        if matter_bridge:
            result.update(matter_bridge.get_status())

        # Thread network status for frontend gate
        try:
            import subprocess
            r = subprocess.run(["ot-ctl", "state"], capture_output=True, text=True, timeout=5)
            thread_state = r.stdout.strip().split("\n")[0].strip().lower() if r.returncode == 0 else "disabled"
            result["thread_ready"] = thread_state in ("leader", "router", "child")
            result["thread_state"] = thread_state
        except Exception:
            result["thread_ready"] = False
            result["thread_state"] = "unavailable"


        return result

    @app.get("/api/multipan/status")
    async def multipan_status():
        """MultiPAN RCP stack status."""
        if not zigbee_service.multipan:
            return {"enabled": False, "running": False}
        return zigbee_service.multipan.get_status()

    @app.post("/api/matter/rename")
    async def matter_rename(request: RenameRequest):
        """Rename a Matter device."""
        matter_bridge = get_matter_bridge()
        zigbee_service = get_zigbee_service()

        if not matter_bridge:
            return {"success": False, "error": "Matter bridge not configured"}

        matter_bridge.rename_device(request.ieee, request.name)
        zigbee_service.friendly_names[request.ieee] = request.name
        zigbee_service._save_json("./data/names.json", zigbee_service.friendly_names)

        return {"success": True, "ieee": request.ieee, "name": request.name}