"""
Automation API - FastAPI routes for threshold-based automation rules.

Integrates with main.py to expose automation CRUD and helper endpoints.
Follows the same registration pattern as zones_api.py.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ConditionItem(BaseModel):
    """Single condition in a compound rule."""
    attribute: str = Field(..., description="State attribute to monitor")
    operator: str = Field(..., description="Comparison operator: eq, neq, gt, lt, gte, lte")
    value: Any = Field(..., description="Threshold value to compare against")


class AutomationCreateRequest(BaseModel):
    """Request to create a new automation rule (single or compound conditions)."""
    source_ieee: str = Field(..., description="IEEE of the sensor/trigger device")
    # Compound conditions (preferred)
    conditions: Optional[List[ConditionItem]] = Field(None, description="AND conditions list")
    # Single condition shorthand (auto-converted to conditions list)
    attribute: Optional[str] = Field(None, description="State attribute (single condition shorthand)")
    operator: Optional[str] = Field(None, description="Comparison operator (single condition shorthand)")
    value: Optional[Any] = Field(None, description="Threshold value (single condition shorthand)")
    # Target action
    target_ieee: str = Field(..., description="IEEE of the actuator device to control")
    command: str = Field(..., description="Command to execute: on, off, toggle, brightness, etc.")
    command_value: Optional[Any] = Field(None, description="Optional value for the command")
    endpoint_id: Optional[int] = Field(None, description="Optional target endpoint ID")
    cooldown: int = Field(5, description="Seconds between re-fires of this rule")
    enabled: bool = Field(True, description="Whether the rule is active")


class AutomationUpdateRequest(BaseModel):
    """Request to update an existing automation rule."""
    conditions: Optional[List[ConditionItem]] = None
    target_ieee: Optional[str] = None
    command: Optional[str] = None
    command_value: Optional[Any] = None
    endpoint_id: Optional[int] = None
    cooldown: Optional[int] = None
    enabled: Optional[bool] = None


# ============================================================================
# ROUTE REGISTRATION
# ============================================================================

def register_automation_routes(
        app: FastAPI,
        automation_getter: Union[Any, Callable[[], Any]],
):
    """
    Register API routes for automation management.

    Args:
        app: FastAPI app instance
        automation_getter: AutomationEngine instance OR a callable returning it
    """

    def get_engine():
        if callable(automation_getter):
            return automation_getter()
        return automation_getter

    # -----------------------------------------------------------------
    # LIST / QUERY
    # -----------------------------------------------------------------

    @app.get("/api/automations", tags=["automations"])
    async def list_automations(source_ieee: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all automation rules.
        Optional query param ?source_ieee= to filter by source device.
        """
        engine = get_engine()
        if not engine:
            return []
        return engine.get_rules(source_ieee=source_ieee)

    @app.get("/api/automations/stats", tags=["automations"])
    async def get_automation_stats() -> Dict[str, Any]:
        """Get automation engine statistics."""
        engine = get_engine()
        if not engine:
            return {"total_rules": 0}
        return engine.get_stats()

    @app.get("/api/automations/trace", tags=["automations"])
    async def get_automation_trace() -> List[Dict[str, Any]]:
        """Get recent automation trace log (last 100 entries)."""
        engine = get_engine()
        if not engine:
            return []
        return engine.get_trace_log()

    @app.get("/api/automations/rule/{rule_id}", tags=["automations"])
    async def get_automation_rule(rule_id: str) -> Dict[str, Any]:
        """Get a single automation rule by ID."""
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Automation engine not initialised")

        rule = engine.get_rule(rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")
        return rule

    # -----------------------------------------------------------------
    # CREATE
    # -----------------------------------------------------------------

    @app.post("/api/automations", tags=["automations"])
    async def create_automation(request: AutomationCreateRequest) -> Dict[str, Any]:
        """Create a new automation rule."""
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Automation engine not initialised")

        data = request.model_dump()
        # Convert ConditionItem models to plain dicts
        if data.get("conditions"):
            data["conditions"] = [
                {"attribute": c["attribute"], "operator": c["operator"], "value": c["value"]}
                for c in data["conditions"]
            ]

        result = engine.add_rule(data)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))

        return result

    # -----------------------------------------------------------------
    # UPDATE
    # -----------------------------------------------------------------

    @app.put("/api/automations/{rule_id}", tags=["automations"])
    async def update_automation(rule_id: str, request: AutomationUpdateRequest) -> Dict[str, Any]:
        """Update an existing automation rule."""
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Automation engine not initialised")

        # Only send fields that were explicitly set
        updates = {k: v for k, v in request.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Convert ConditionItem models to plain dicts
        if "conditions" in updates and updates["conditions"]:
            updates["conditions"] = [
                {"attribute": c["attribute"], "operator": c["operator"], "value": c["value"]}
                for c in updates["conditions"]
            ]

        result = engine.update_rule(rule_id, updates)
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            if "not found" in error.lower():
                raise HTTPException(status_code=404, detail=error)
            raise HTTPException(status_code=400, detail=error)

        return result

    # -----------------------------------------------------------------
    # TOGGLE ENABLE/DISABLE (convenience endpoint)
    # -----------------------------------------------------------------

    @app.patch("/api/automations/{rule_id}/toggle", tags=["automations"])
    async def toggle_automation(rule_id: str) -> Dict[str, Any]:
        """Toggle a rule's enabled state."""
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Automation engine not initialised")

        rule = engine.get_rule(rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")

        new_state = not rule.get("enabled", True)
        result = engine.update_rule(rule_id, {"enabled": new_state})
        return result

    # -----------------------------------------------------------------
    # DELETE
    # -----------------------------------------------------------------

    @app.delete("/api/automations/{rule_id}", tags=["automations"])
    async def delete_automation(rule_id: str) -> Dict[str, Any]:
        """Delete an automation rule."""
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Automation engine not initialised")

        result = engine.delete_rule(rule_id)
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            if "not found" in error.lower():
                raise HTTPException(status_code=404, detail=error)
            raise HTTPException(status_code=400, detail=error)

        return result

    # -----------------------------------------------------------------
    # HELPER ENDPOINTS (for the frontend Automation tab)
    # -----------------------------------------------------------------

    @app.get("/api/automations/device/{ieee}/attributes", tags=["automations"])
    async def get_device_attributes(ieee: str) -> List[Dict[str, Any]]:
        """
        Get available threshold attributes for a source device.
        Returns state keys with current values and valid operators.
        Used by the frontend to populate the threshold selector.
        """
        engine = get_engine()
        if not engine:
            return []
        return engine.get_source_attributes(ieee)

    @app.get("/api/automations/device/{ieee}/actions", tags=["automations"])
    async def get_device_actions(ieee: str) -> List[Dict[str, Any]]:
        """
        Get available actions for a target device.
        Returns control commands (on/off/brightness/etc).
        Used by the frontend to populate the action selector.
        """
        engine = get_engine()
        if not engine:
            return []
        return engine.get_target_actions(ieee)

    @app.get("/api/automations/actuators", tags=["automations"])
    async def get_actuator_devices() -> List[Dict[str, Any]]:
        """
        Get list of all actuator devices that can be automation targets.
        Filters to devices with controllable capabilities (on_off, light, etc).
        Used by the frontend to populate the target device picker.
        """
        engine = get_engine()
        if not engine:
            return []
        return engine.get_actuator_devices()

    logger.info("Automation API routes registered")