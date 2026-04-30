"""
Auth API routes — MFA-aware version.

Login flow
----------
Step 1:  POST /api/auth/login                 username + password
         → 200  {success: true, ...}                       (no MFA, fully logged in)
         → 200  {mfa_required: true, challenge: "..."}     (MFA needed)
         → 401  {detail: "..."}                            (rejected)
         → 423  {detail: "...", locked_until: ts}          (account/IP locked)
         → 403  {detail: "...", lan_only_violation: true}  (must be on LAN)

Step 2:  POST /api/auth/login/mfa             challenge + code
         → 200  {success: true, ...}                       (TOTP or recovery OK)
         → 401  {detail: "..."}                            (bad code)

MFA enrolment (self-service, while already logged in)
-----------------------------------------------------
POST   /api/auth/mfa/enrol/start                 → returns secret + otpauth URI
POST   /api/auth/mfa/enrol/finish                → confirm with TOTP, get recovery codes
POST   /api/auth/mfa/disable                     → self-disable (re-prompts password)
POST   /api/auth/mfa/recovery-codes/regenerate   → new set, invalidates old
GET    /api/auth/mfa/status                      → state for current user

Admin
-----
GET    /api/auth/lockouts                        list locked accounts
POST   /api/auth/lockouts/{username}/unlock      force-unlock
POST   /api/auth/users/{username}/disable-mfa    admin: blow away MFA for a user
GET    /api/auth/network                         show network policy

Plus everything from the previous round (user/group/token CRUD).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from modules.auth import AuthManager, KNOWN_SCOPES
from modules.auth_middleware import (
    Principal, get_principal, issue_session_cookie,
    require_authenticated, require_scope,
)
from modules.auth_secure import SecureAuthManager, LAN_ONLY_SCOPE
from modules.auth_network import NetworkResolver

logger = logging.getLogger("routes.auth")


# --- request models --------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=1, max_length=200)
    remember: bool = True


class MFALoginRequest(BaseModel):
    challenge: str = Field(..., min_length=8, max_length=256)
    code: str = Field(..., min_length=4, max_length=20)
    remember: bool = True


class MFAEnrolFinishRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


class DisableMFARequest(BaseModel):
    # Re-prompt for password to confirm dangerous self-action
    password: str = Field(..., min_length=1, max_length=200)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: Optional[str] = None
    groups: List[str] = Field(default_factory=list)
    extra_scopes: List[str] = Field(default_factory=list)
    description: str = ""


class UpdateUserRequest(BaseModel):
    password: Optional[str] = None
    groups: Optional[List[str]] = None
    extra_scopes: Optional[List[str]] = None
    disabled: Optional[bool] = None
    description: Optional[str] = None


class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=32)
    scopes: List[str] = Field(default_factory=list)
    description: str = ""


class UpdateGroupRequest(BaseModel):
    scopes: Optional[List[str]] = None
    description: Optional[str] = None


class IssueTokenRequest(BaseModel):
    username: Optional[str] = None
    label: str = Field(..., min_length=1, max_length=64)
    scopes: Optional[List[str]] = None
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)
    device_id: Optional[str] = None


# --- registration ----------------------------------------------------------

def register_auth_routes(
        app: FastAPI,
        auth_manager_getter: Callable[[], AuthManager],
        secure_manager_getter: Callable[[], SecureAuthManager],
        network_resolver_getter: Callable[[], NetworkResolver],
        secret_getter: Callable[[], bytes],
):
    """
    Register auth routes.

    Args:
        app: FastAPI instance
        auth_manager_getter: returns AuthManager
        secure_manager_getter: returns SecureAuthManager
        network_resolver_getter: returns NetworkResolver
        secret_getter: returns the current session-cookie HMAC secret
    """

    def _auth() -> AuthManager:
        m = auth_manager_getter()
        if not m:
            raise HTTPException(503, "Auth not initialised")
        return m

    def _sec() -> SecureAuthManager:
        m = secure_manager_getter()
        if not m:
            raise HTTPException(503, "Auth not initialised")
        return m

    def _net() -> NetworkResolver:
        m = network_resolver_getter()
        if not m:
            raise HTTPException(503, "Network resolver not configured")
        return m

    def _set_session(response: Response, username: str, remember: bool):
        cookie = issue_session_cookie(username, secret_getter())
        max_age = 30 * 24 * 3600 if remember else None
        response.set_cookie(
            key="zmm_session",
            value=cookie,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )

    # ---- public --------------------------------------------------------

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, request: Request, response: Response):
        sec = _sec()
        net = _net()
        client_ip = net.resolve(request)
        is_lan = net.is_lan(client_ip)

        outcome = await sec.begin_login(
            req.username, req.password, client_ip, is_lan,
        )

        if outcome.success and outcome.user:
            _set_session(response, outcome.user.username, req.remember)
            return {
                "success": True,
                "username": outcome.user.username,
                "scopes": sorted(
                    _auth().resolve_user_scopes(outcome.user.username)
                ),
                "mfa_required": False,
            }

        if outcome.mfa_required:
            return {
                "success": False,
                "mfa_required": True,
                "challenge": outcome.challenge,
            }

        if outcome.locked_until:
            raise HTTPException(
                status_code=423,        # Locked
                detail=outcome.reason,
                headers={
                    "Retry-After": str(int(outcome.locked_until - time.time())),
                },
            )

        if outcome.lan_only_violation:
            raise HTTPException(403, outcome.reason)

        raise HTTPException(401, outcome.reason or "Invalid credentials")

    @app.post("/api/auth/login/mfa")
    async def login_mfa(
            req: MFALoginRequest, request: Request, response: Response,
    ):
        sec = _sec()
        net = _net()
        client_ip = net.resolve(request)

        outcome = await sec.complete_mfa(req.challenge, req.code, client_ip)

        if outcome.success and outcome.user:
            _set_session(response, outcome.user.username, req.remember)
            return {
                "success": True,
                "username": outcome.user.username,
                "scopes": sorted(
                    _auth().resolve_user_scopes(outcome.user.username)
                ),
            }

        if outcome.locked_until:
            raise HTTPException(
                status_code=423,
                detail=outcome.reason,
                headers={
                    "Retry-After": str(int(outcome.locked_until - time.time())),
                },
            )
        raise HTTPException(401, outcome.reason or "Invalid MFA code")

    @app.post("/api/auth/logout")
    async def logout(response: Response):
        response.delete_cookie("zmm_session", path="/")
        return {"success": True}

    @app.get("/api/auth/whoami")
    async def whoami(request: Request):
        p = get_principal(request)
        if not p:
            return {"authenticated": False}
        sec = _sec()
        return {
            "authenticated": True,
            "username": p.user.username,
            "scopes": sorted(p.scopes),
            "auth_method": p.auth_method,
            "token_id": p.token.token_hash[:12] if p.token else None,
            "mfa": sec.mfa_status(p.user.username),
            "is_lan": _net().is_lan(_net().resolve(request)),
        }

    # ---- MFA enrolment (self-service) ----------------------------------

    @app.post("/api/auth/mfa/enrol/start")
    async def mfa_start(
            principal: Principal = Depends(require_authenticated),
    ):
        # Bearer-token sessions can't enrol MFA — must be cookie session
        # (the user must have a real interactive session)
        if principal.auth_method != "cookie":
            raise HTTPException(
                403,
                "MFA enrolment requires an interactive web session "
                "(not bearer token).",
            )
        try:
            secret, uri = await _sec().begin_enrolment(principal.user.username)
        except KeyError:
            raise HTTPException(404, "User not found")
        return {
            "success": True,
            "secret": secret,
            "otpauth_uri": uri,
            "issuer": "ZMM",
            "account": principal.user.username,
        }

    @app.post("/api/auth/mfa/enrol/finish")
    async def mfa_finish(
            body: MFAEnrolFinishRequest,
            principal: Principal = Depends(require_authenticated),
    ):
        if principal.auth_method != "cookie":
            raise HTTPException(403, "MFA enrolment requires interactive session.")
        try:
            recovery = await _sec().finish_enrolment(
                principal.user.username, body.code,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Recovery codes shown ONCE
        return {
            "success": True,
            "recovery_codes": recovery,
            "warning": "These recovery codes are shown only once. "
                       "Store them somewhere safe.",
        }

    @app.post("/api/auth/mfa/disable")
    async def mfa_self_disable(
            body: DisableMFARequest,
            principal: Principal = Depends(require_authenticated),
    ):
        # Require password re-confirmation
        if not _auth().verify_password(principal.user.username, body.password):
            raise HTTPException(401, "Password incorrect")
        await _sec().disable_mfa(principal.user.username)
        return {"success": True}

    @app.post("/api/auth/mfa/recovery-codes/regenerate")
    async def mfa_regen_recovery(
            principal: Principal = Depends(require_authenticated),
    ):
        try:
            codes = await _sec().regenerate_recovery_codes(
                principal.user.username,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "success": True,
            "recovery_codes": codes,
            "warning": "Old recovery codes have been invalidated.",
        }

    @app.get("/api/auth/mfa/status")
    async def mfa_status(
            principal: Principal = Depends(require_authenticated),
    ):
        return _sec().mfa_status(principal.user.username)

    # ---- admin: lockouts and MFA reset ---------------------------------

    @app.get("/api/auth/lockouts")
    async def list_lockouts(_=Depends(require_scope("admin"))):
        return {"locked": _sec().list_locked_accounts()}

    @app.post("/api/auth/lockouts/{username}/unlock")
    async def admin_unlock(
            username: str,
            _=Depends(require_scope("admin")),
    ):
        was_locked = _sec().admin_unlock(username)
        return {"success": True, "was_locked": was_locked}

    @app.post("/api/auth/users/{username}/disable-mfa")
    async def admin_disable_mfa(
            username: str,
            _=Depends(require_scope("admin")),
    ):
        if username not in _auth().users:
            raise HTTPException(404, "User not found")
        await _sec().disable_mfa(username)
        return {"success": True}

    @app.get("/api/auth/network")
    async def network_info(_=Depends(require_scope("admin"))):
        net = _net()
        return net.describe()

    # ---- users (admin) -------------------------------------------------

    @app.get("/api/auth/users")
    async def list_users(_=Depends(require_scope("admin"))):
        # Augment with MFA status so admin UI can see who's enrolled
        sec = _sec()
        users = _auth().list_users()
        for u in users:
            u["mfa"] = sec.mfa_status(u["username"])
        return {"users": users}

    @app.post("/api/auth/users")
    async def create_user(
            req: CreateUserRequest,
            _=Depends(require_scope("admin")),
    ):
        try:
            user = await _auth().create_user(
                username=req.username,
                password=req.password,
                groups=req.groups,
                extra_scopes=req.extra_scopes,
                description=req.description,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"success": True, "user": user.public_view()}

    @app.patch("/api/auth/users/{username}")
    async def update_user(
            username: str,
            req: UpdateUserRequest,
            principal: Principal = Depends(require_authenticated),
    ):
        mgr = _auth()
        is_admin = "admin" in principal.scopes
        is_self = principal.user.username == username

        if not is_admin and not is_self:
            raise HTTPException(403, "Cannot modify another user")
        if not is_admin and (
                req.groups is not None
                or req.extra_scopes is not None
                or req.disabled is not None
        ):
            raise HTTPException(403, "Only admins can change roles or status")

        if (
                req.disabled and is_self
                and "admin" in mgr.resolve_user_scopes(username)
        ):
            others = [
                u for n, u in mgr.users.items()
                if n != username and "admin" in mgr.resolve_user_scopes(n)
            ]
            if not others:
                raise HTTPException(400, "Cannot disable the last admin")

        try:
            changes = req.dict(exclude_unset=True)
            user = await mgr.update_user(username, **changes)
        except KeyError:
            raise HTTPException(404, "User not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"success": True, "user": user.public_view()}

    @app.delete("/api/auth/users/{username}")
    async def delete_user(
            username: str,
            principal: Principal = Depends(require_scope("admin")),
    ):
        if username == principal.user.username:
            raise HTTPException(400, "Cannot delete your own account")
        try:
            await _auth().delete_user(username)
        except KeyError:
            raise HTTPException(404, "User not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Cascade: clear MFA record too
        sec = _sec()
        sec.mfa.pop(username, None)
        _auth()._save_locked()
        return {"success": True}

    # ---- groups (admin) ------------------------------------------------

    @app.get("/api/auth/groups")
    async def list_groups(_=Depends(require_scope("admin"))):
        return {"groups": _auth().list_groups()}

    @app.post("/api/auth/groups")
    async def create_group(
            req: CreateGroupRequest, _=Depends(require_scope("admin")),
    ):
        try:
            grp = await _auth().create_group(req.name, req.scopes, req.description)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"success": True, "group": grp.to_dict()}

    @app.patch("/api/auth/groups/{name}")
    async def update_group(
            name: str, req: UpdateGroupRequest, _=Depends(require_scope("admin")),
    ):
        try:
            changes = req.dict(exclude_unset=True)
            grp = await _auth().update_group(name, **changes)
        except KeyError:
            raise HTTPException(404, "Group not found")
        return {"success": True, "group": grp.to_dict()}

    @app.delete("/api/auth/groups/{name}")
    async def delete_group(name: str, _=Depends(require_scope("admin"))):
        try:
            await _auth().delete_group(name)
        except KeyError:
            raise HTTPException(404, "Group not found")
        return {"success": True}

    # ---- scopes --------------------------------------------------------

    @app.get("/api/auth/scopes")
    async def list_scopes(_=Depends(require_authenticated)):
        # Add network:lan_only to known scopes for the picker
        all_scopes = dict(KNOWN_SCOPES)
        all_scopes[LAN_ONLY_SCOPE] = (
            "Account can only sign in from the home LAN. "
            "Skip this for accounts that need remote access."
        )
        return {
            "scopes": [
                {"name": k, "description": v} for k, v in all_scopes.items()
            ]
        }

    # ---- tokens --------------------------------------------------------

    @app.get("/api/auth/tokens")
    async def list_tokens(
            principal: Principal = Depends(require_authenticated),
            username: Optional[str] = None,
    ):
        mgr = _auth()
        is_admin = "admin" in principal.scopes
        if username and username != principal.user.username and not is_admin:
            raise HTTPException(403, "Cannot list other users' tokens")
        target = username or (None if is_admin else principal.user.username)
        return {"tokens": mgr.list_tokens(target)}

    @app.post("/api/auth/tokens")
    async def issue_token(
            req: IssueTokenRequest,
            principal: Principal = Depends(require_authenticated),
    ):
        mgr = _auth()
        is_admin = "admin" in principal.scopes
        target_user = req.username or principal.user.username
        if target_user != principal.user.username and not is_admin:
            raise HTTPException(403, "Cannot issue tokens for another user")
        try:
            expires = (req.expires_in_days * 86400) if req.expires_in_days else None
            plaintext, rec = await mgr.issue_token(
                username=target_user,
                label=req.label,
                scopes=req.scopes,
                expires_in_s=expires,
                device_id=req.device_id,
            )
        except KeyError:
            raise HTTPException(404, "User not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"success": True, "token": plaintext, "record": rec.public_view()}

    @app.delete("/api/auth/tokens/{token_id}")
    async def revoke_token(
            token_id: str,
            principal: Principal = Depends(require_authenticated),
    ):
        mgr = _auth()
        target = None
        for h, t in mgr.tokens.items():
            if h == token_id or h.startswith(token_id):
                target = t
                break
        if not target:
            raise HTTPException(404, "Token not found")
        is_admin = "admin" in principal.scopes
        if target.user != principal.user.username and not is_admin:
            raise HTTPException(403, "Cannot revoke another user's token")
        try:
            await mgr.delete_token(token_id)
        except KeyError:
            raise HTTPException(404, "Token not found")
        return {"success": True}

    logger.info("Auth routes registered (MFA + lockout + LAN-aware)")