"""
Rotary Binding API — routes for managing rotary → device bindings.
=================================================================

Endpoints:
  GET  /api/rotary-bindings                      — List all bindings
  GET  /api/rotary-bindings/{source_ieee}        — List bindings for a device
  POST /api/rotary-bindings                      — Add/update a binding
  DELETE /api/rotary-bindings/{source_ieee}/{key} — Remove a binding
  GET  /api/rotary-bindings/stats                — Binding stats
  GET  /api/rotary-bindings/commands             — Available target commands + defaults
  POST /api/rotary-bindings/reload               — Reload from definitions
"""

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("routes.rotary_bindings")


class BindingTarget(BaseModel):
    ieee: str = Field(..., description="Target device IEEE or matter_N")
    command: str = Field("brightness", description="Command to send")
    endpoint: Optional[int] = Field(None, description="Target endpoint (optional)")
    min: float = Field(0, description="Minimum value")
    max: float = Field(254, description="Maximum value")
    wrap: bool = Field(False, description="Wrap around at max")
    invert: bool = Field(False, description="Reverse direction")
    description: str = Field("", description="Binding description")


class AddBindingRequest(BaseModel):
    source_ieee: str = Field(..., description="Source device IEEE (matter_N)")
    rotary_key: str = Field(..., description="Rotary state key from definition")
    ep: int = Field(..., description="Source endpoint ID")
    max_positions: int = Field(18, description="Total positions on the rotary")
    mode: str = Field("step", description="'step' or 'position'")
    cw_ep: int = Field(0, description="Clockwise endpoint ID")
    ccw_ep: int = Field(0, description="Counter-clockwise endpoint ID")
    step_size: int = Field(25, description="Value change per click")
    target: BindingTarget


def register_rotary_binding_routes(app: FastAPI, get_definition_store, get_binding_manager):
    """Register rotary binding API routes."""

    @app.get("/api/rotary-bindings", tags=["rotary-bindings"])
    async def list_all_bindings():
        mgr = get_binding_manager()
        return {
            "success": True,
            "bindings": mgr.get_bindings(),
        }

    @app.get("/api/rotary-bindings/stats", tags=["rotary-bindings"])
    async def binding_stats():
        mgr = get_binding_manager()
        return {"success": True, **mgr.get_stats()}

    @app.get("/api/rotary-bindings/commands", tags=["rotary-bindings"])
    async def available_commands():
        """List available target commands with default min/max ranges."""
        from modules.rotary_bindings import COMMAND_DEFAULTS
        return {"success": True, "commands": COMMAND_DEFAULTS}

    @app.get("/api/rotary-bindings/{source_ieee}", tags=["rotary-bindings"])
    async def get_device_bindings(source_ieee: str):
        mgr = get_binding_manager()
        return {
            "success": True,
            "source_ieee": source_ieee,
            "bindings": mgr.get_bindings(source_ieee),
        }

    @app.post("/api/rotary-bindings", tags=["rotary-bindings"])
    async def add_binding(request: AddBindingRequest):
        mgr = get_binding_manager()
        store = get_definition_store()

        result = mgr.add_binding(
            source_ieee=request.source_ieee,
            rotary_key=request.rotary_key,
            ep=request.ep,
            max_positions=request.max_positions,
            target=request.target.model_dump(),
            mode=request.mode,
            cw_ep=request.cw_ep,
            ccw_ep=request.ccw_ep,
            step_size=request.step_size,
        )

        if result.get("success"):
            mgr.save_to_definition(store, request.source_ieee)

        return result

    @app.delete("/api/rotary-bindings/{source_ieee}/{rotary_key}", tags=["rotary-bindings"])
    async def remove_binding(source_ieee: str, rotary_key: str):
        mgr = get_binding_manager()
        store = get_definition_store()

        result = mgr.remove_binding(source_ieee, rotary_key)

        if result.get("success"):
            mgr.save_to_definition(store, source_ieee)

        return result

    @app.post("/api/rotary-bindings/reload", tags=["rotary-bindings"])
    async def reload_bindings():
        mgr = get_binding_manager()
        store = get_definition_store()
        mgr.load_from_definitions(store)
        return {
            "success": True,
            "count": len(mgr._all_bindings),
        }

    logger.info("Rotary binding routes registered")