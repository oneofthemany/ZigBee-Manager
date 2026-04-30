"""
Presence Users API routes — auth-aware version.

Scope model
-----------
- `admin`                              full control over presence users
- `presence:read`                      list/view presence state for all users
- `presence:write`                     update any user's location
- `presence:write:<user_id>`           update only a specific user's location
                                       (this is what mobile-app tokens get)

The fix endpoint is the hot path called by the companion app every few minutes
(or on geofence transitions). To minimize attack surface, we issue mobile
tokens with ONLY `presence:write:<user_id>` and nothing else. A leaked phone
token can update one person's location and nothing more.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from modules.auth_middleware import (
    Principal, require_scope, require_any_scope, require_authenticated,
)

logger = logging.getLogger("modules.presence_routes")


class UserUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=32)
    display_name: str = Field(..., min_length=1, max_length=64)
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    radius_m: float = 100.0
    hysteresis_m: float = 30.0
    stale_after_s: float = 1800.0
    min_accuracy_m: float = 250.0
    enabled: bool = True
    owntracks_user: Optional[str] = None
    owntracks_device: Optional[str] = None


class FixReport(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    accuracy: Optional[float] = Field(None, ge=0.0)
    timestamp: Optional[float] = None


class ManualSet(BaseModel):
    presence: str = Field(..., pattern="^(home|away|unknown)$")


def register_presence_routes(app: FastAPI, presence_manager_getter: Callable):

    def _mgr():
        mgr = presence_manager_getter()
        if not mgr:
            raise HTTPException(503, "Presence manager not initialised")
        return mgr

    @app.get("/api/presence/users")
    async def list_users(_=Depends(require_scope("presence:read"))):
        return {"users": _mgr().list_users()}

    @app.get("/api/presence/users/{user_id}")
    async def get_user(
            user_id: str,
            _=Depends(require_scope("presence:read")),
    ):
        dev = _mgr().get_user(user_id)
        if not dev:
            raise HTTPException(404, "User not found")
        return {
            **dev.cfg.to_dict(),
            "ieee": dev.ieee,
            "state": dict(dev.state),
            "last_seen": dev.last_seen,
        }

    @app.post("/api/presence/users")
    async def upsert_user(
            payload: UserUpsert,
            _=Depends(require_scope("admin")),
    ):
        result = await _mgr().upsert_user(payload.dict())
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.delete("/api/presence/users/{user_id}")
    async def delete_user(
            user_id: str,
            _=Depends(require_scope("admin")),
    ):
        result = await _mgr().delete_user(user_id)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/presence/users/{user_id}/fix")
    async def report_fix(
            user_id: str,
            fix: FixReport,
            request: Request,
    ):
        # Per-user scope check: presence:write:<user_id> OR presence:write OR admin.
        # We do this manually because FastAPI Depends doesn't see path params
        # at scope construction time.
        principal: Optional[Principal] = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "Authentication required",
                                headers={"WWW-Authenticate": "Bearer"})

        from modules.auth import scope_matches
        wanted = f"presence:write:{user_id}"
        if not (
                scope_matches(wanted, principal.scopes)
                or scope_matches("presence:write", principal.scopes)
                or scope_matches("admin", principal.scopes)
        ):
            raise HTTPException(403, f"Token lacks scope: {wanted}")

        result = await _mgr().report_pwa_fix(
            user_id=user_id,
            lat=fix.lat,
            lon=fix.lon,
            accuracy=fix.accuracy,
            timestamp=fix.timestamp,
        )
        if not result.get("success") and not result.get("ignored"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.post("/api/presence/users/{user_id}/manual")
    async def manual_override(
            user_id: str,
            body: ManualSet,
            _=Depends(require_any_scope("admin", "presence:write")),
    ):
        result = await _mgr().manual_set(user_id, body.presence)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    logger.info("Presence user routes registered (auth-protected)")