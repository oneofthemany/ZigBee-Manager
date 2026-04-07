"""
Matter Definition API — routes for endpoint scanning and definition CRUD.
=========================================================================

Endpoints:
  GET  /api/matter/nodes/{node_id}/scan-endpoints  — Scan & map all endpoints
  POST /api/matter/nodes/{node_id}/generate-definition — Auto-generate a definition draft
  GET  /api/matter/definitions         — List all saved definitions
  GET  /api/matter/definitions/{file}  — Get a specific definition
  POST /api/matter/definitions         — Save a new/updated definition
  DELETE /api/matter/definitions/{file} — Delete a definition
  POST /api/matter/definitions/reload  — Reload definitions from disk
"""

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("routes.matter_definitions")


class SaveDefinitionRequest(BaseModel):
    definition: dict
    filename: Optional[str] = None


def register_matter_definition_routes(app: FastAPI, get_matter_bridge):
    """Register Matter definition management routes."""

    def _get_bridge():
        bridge = get_matter_bridge()
        if not bridge or not bridge.is_connected:
            raise HTTPException(503, "Matter server not connected")
        return bridge

    def _get_device(bridge, node_id: int):
        ieee = f"matter_{node_id}"
        if ieee not in bridge.devices:
            raise HTTPException(404, f"Matter node {node_id} not found")
        return bridge.devices[ieee]

    # ── Endpoint Scanning ──────────────────────────────────────────────

    @app.get("/api/matter/nodes/{node_id}/scan-endpoints", tags=["matter-definitions"])
    async def scan_node_endpoints(node_id: int):
        """
        Scan a Matter node's endpoints and return a structured map.
        Shows device types, clusters, tags, and auto-detected roles.
        """
        bridge = _get_bridge()
        dev = _get_device(bridge, node_id)
        attributes = dev.node.get("attributes", {})

        from modules.matter_definitions import scan_endpoints
        result = scan_endpoints(attributes)

        return {
            "success": True,
            "node_id": node_id,
            "friendly_name": dev.friendly_name,
            "model": dev.model,
            "manufacturer": dev.manufacturer,
            **result,
        }

    @app.post("/api/matter/nodes/{node_id}/generate-definition", tags=["matter-definitions"])
    async def generate_definition(node_id: int):
        """
        Auto-generate a definition draft from a node's attributes.
        Returns a JSON definition that the user can refine and save.
        """
        bridge = _get_bridge()
        dev = _get_device(bridge, node_id)
        attributes = dev.node.get("attributes", {})

        from modules.matter_definitions import generate_definition_draft
        draft = generate_definition_draft(attributes)

        return {
            "success": True,
            "node_id": node_id,
            "definition": draft,
        }

    # ── Definition CRUD ─────────────────────────────────────────────────

    @app.get("/api/matter/definitions", tags=["matter-definitions"])
    async def list_definitions():
        """List all saved Matter device definitions."""
        from modules.matter_definitions import get_definition_store
        store = get_definition_store()
        return {
            "success": True,
            "definitions": store.list_definitions(),
        }

    @app.get("/api/matter/definitions/{filename}", tags=["matter-definitions"])
    async def get_definition(filename: str):
        """Get a specific definition by filename."""
        from modules.matter_definitions import get_definition_store
        store = get_definition_store()
        if filename not in store._by_file:
            raise HTTPException(404, f"Definition not found: {filename}")
        return {
            "success": True,
            "filename": filename,
            "definition": store._by_file[filename],
        }

    @app.post("/api/matter/definitions", tags=["matter-definitions"])
    async def save_definition(request: SaveDefinitionRequest):
        """Save a device definition (new or updated)."""
        from modules.matter_definitions import get_definition_store
        store = get_definition_store()

        defn = request.definition
        if not defn.get("vendor_id"):
            raise HTTPException(400, "Definition must include vendor_id")

        try:
            filename = store.save(defn, request.filename)
            return {
                "success": True,
                "filename": filename,
                "message": f"Definition saved: {filename}",
            }
        except Exception as e:
            raise HTTPException(500, f"Failed to save definition: {e}")

    @app.delete("/api/matter/definitions/{filename}", tags=["matter-definitions"])
    async def delete_definition(filename: str):
        """Delete a definition file."""
        from modules.matter_definitions import get_definition_store
        store = get_definition_store()
        if store.delete(filename):
            return {"success": True, "message": f"Deleted: {filename}"}
        raise HTTPException(404, f"Definition not found: {filename}")

    @app.post("/api/matter/definitions/reload", tags=["matter-definitions"])
    async def reload_definitions():
        """Reload all definitions from disk."""
        from modules.matter_definitions import get_definition_store
        store = get_definition_store()
        store.reload()
        return {
            "success": True,
            "count": len(store._definitions),
            "message": f"Reloaded {len(store._definitions)} definitions",
        }

    logger.info("Matter definition routes registered")