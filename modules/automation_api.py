"""
Automation API - FastAPI routes for state-machine automation rules.
Steps are recursive (if_then_else, parallel) so we accept raw dicts and
delegate validation to the engine.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ConditionItem(BaseModel):
    type: str = "attribute"
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    sustain: Optional[int] = None
    negate: bool = False
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    days: Optional[List[int]] = None

class PrerequisiteItem(BaseModel):
    type: str = "device"
    # device fields
    ieee: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    negate: bool = False
    # time_window fields
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    days: Optional[List[int]] = None

class AutomationCreateRequest(BaseModel):
    name: Optional[str] = ""
    source_ieee: str
    conditions: Optional[List[ConditionItem]] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    prerequisites: Optional[List[PrerequisiteItem]] = Field(default_factory=list)
    then_sequence: List[Dict[str, Any]] = Field(default_factory=list)
    else_sequence: List[Dict[str, Any]] = Field(default_factory=list)
    cooldown: int = 5
    enabled: bool = True

class AutomationUpdateRequest(BaseModel):
    name: Optional[str] = None
    conditions: Optional[List[ConditionItem]] = None
    prerequisites: Optional[List[PrerequisiteItem]] = None
    then_sequence: Optional[List[Dict[str, Any]]] = None
    else_sequence: Optional[List[Dict[str, Any]]] = None
    cooldown: Optional[int] = None
    enabled: Optional[bool] = None


def _conds_to_dicts(items):
    if not items: return []
    r = []
    for c in items:
        if c.type == "time_window":
            r.append({"type": "time_window", "time_from": c.time_from, "time_to": c.time_to,
                      "days": c.days if c.days is not None else list(range(7)), "negate": c.negate})
        else:
            d = {"type": "attribute", "attribute": c.attribute, "operator": c.operator, "value": c.value}
            if c.sustain and c.sustain > 0: d["sustain"] = c.sustain
            r.append(d)
    return r

def _prereqs_to_dicts(items):
    if not items: return []
    result = []
    for p in items:
        if p.type == "time_window":
            result.append({
                "type": "time_window",
                "time_from": p.time_from,
                "time_to": p.time_to,
                "days": p.days if p.days is not None else list(range(7)),
                "negate": p.negate,
            })
        else:
            result.append({
                "type": "device",
                "ieee": p.ieee,
                "attribute": p.attribute,
                "operator": p.operator,
                "value": p.value,
                "negate": p.negate,
            })
    return result


def register_automation_routes(app: FastAPI,
                               automation_getter: Union[Any, Callable[[], Any]]):
    def ge():
        return automation_getter() if callable(automation_getter) else automation_getter

    @app.get("/api/automations", tags=["automations"])
    async def list_rules(source_ieee: Optional[str] = None):
        e = ge(); return e.get_rules(source_ieee=source_ieee) if e else []

    @app.get("/api/automations/stats", tags=["automations"])
    async def stats():
        e = ge(); return e.get_stats() if e else {"total_rules":0}

    @app.get("/api/automations/trace", tags=["automations"])
    async def trace(rule_id: Optional[str] = None):
        e = ge()
        if not e: return []
        entries = e.get_trace_log()
        return [x for x in entries if x.get("rule_id")==rule_id] if rule_id else entries

    @app.get("/api/automations/rule/{rule_id}", tags=["automations"])
    async def get_rule(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        r = e.get_rule(rule_id)
        if not r: raise HTTPException(404, f"Not found: {rule_id}")
        return r

    @app.post("/api/automations", tags=["automations"])
    async def create(request: AutomationCreateRequest):
        e = ge()
        if not e: raise HTTPException(503)
        data = request.model_dump()
        if data.get("conditions"): data["conditions"] = _conds_to_dicts(request.conditions)
        if data.get("prerequisites"): data["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        # then_sequence and else_sequence are already raw dicts
        result = e.add_rule(data)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.put("/api/automations/{rule_id}", tags=["automations"])
    async def update(rule_id: str, request: AutomationUpdateRequest):
        e = ge()
        if not e: raise HTTPException(503)
        updates = {k:v for k,v in request.model_dump().items() if v is not None}
        if not updates: raise HTTPException(400, "Nothing to update")
        if "conditions" in updates and request.conditions:
            updates["conditions"] = _conds_to_dicts(request.conditions)
        if "prerequisites" in updates and request.prerequisites:
            updates["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        result = e.update_rule(rule_id, updates)
        if not result.get("success"):
            code = 404 if "not found" in result.get("error","").lower() else 400
            raise HTTPException(code, result.get("error"))
        return result

    @app.patch("/api/automations/{rule_id}/toggle", tags=["automations"])
    async def toggle(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        r = e.get_rule(rule_id)
        if not r: raise HTTPException(404)
        return e.update_rule(rule_id, {"enabled": not r.get("enabled",True)})

    @app.delete("/api/automations/{rule_id}", tags=["automations"])
    async def delete(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        result = e.delete_rule(rule_id)
        if not result.get("success"): raise HTTPException(404, result.get("error"))
        return result

    @app.get("/api/automations/device/{ieee}/attributes", tags=["automations"])
    async def attrs(ieee: str):
        e = ge(); return e.get_source_attributes(ieee) if e else []

    @app.get("/api/automations/device/{ieee}/state", tags=["automations"])
    async def dev_state(ieee: str):
        e = ge(); return e.get_device_state(ieee) if e else {}

    @app.get("/api/automations/device/{ieee}/actions", tags=["automations"])
    async def actions(ieee: str):
        e = ge(); return e.get_target_actions(ieee) if e else []

    @app.get("/api/automations/actuators", tags=["automations"])
    async def actuators():
        e = ge(); return e.get_actuator_devices() if e else []

    @app.get("/api/automations/devices", tags=["automations"])
    async def all_devs():
        e = ge(); return e.get_all_devices_summary() if e else []

    logger.info("Automation API routes registered")