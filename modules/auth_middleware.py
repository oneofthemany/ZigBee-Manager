"""
FastAPI middleware and dependency helpers for auth.

Two ways routes get authorised:

1. **Middleware** runs on every HTTP request and:
   - Bypasses unauthenticated paths (login, healthcheck, static assets,
     the legacy WebSocket if it doesn't yet enforce auth).
   - For everything else, looks for a Bearer token (Authorization header)
     or a session cookie (`zmm_session`).
   - On success, attaches `request.state.principal = (User, scopes_set,
     token_or_None)`. On failure, returns 401 unless the route is in the
     "anonymous-allowed" list.

2. **`require_scope(scope)` dependency** — call from route signatures to
   enforce a specific scope. The middleware does the auth, the dependency
   does the authz.

We support BOTH bearer tokens AND session cookies because:
- The browser UI uses the cookie (set on /api/auth/login).
- The Android app, curl, MQTT, anything programmatic uses bearer tokens.

Cookies are signed with HMAC-SHA256 using a secret derived from the auth
file mtime+inode. This is good enough to prevent forgery without needing
a separate secret-management story; the secret rotates automatically when
the file is replaced (e.g. restored from backup → all sessions invalidated,
which is the desired behaviour).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Awaitable, Callable, Iterable, Optional, Set, Tuple

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from modules.auth import AuthManager, User, TokenRecord, scope_matches

logger = logging.getLogger("modules.auth_middleware")


# --- secret derivation -----------------------------------------------------

def _derive_session_secret(path: str) -> bytes:
    """Derive an HMAC secret stable across saves but invalidated on file replacement.

    We use just the inode (not mtime) so routine writes don't rotate the
    secret. A backup-restore replaces the file → new inode → all sessions
    invalidated, which is the desired behaviour."""
    try:
        st = os.stat(path)
        material = f"{st.st_ino}".encode("ascii")
    except OSError:
        material = b"zmm-no-auth-file"
    return hashlib.sha256(b"zmm-session-secret:" + material).digest()

def _sign_session(username: str, issued_at: int, secret: bytes) -> str:
    """Cookie format: base64url(username|issued_at|hmac_sig)."""
    body = f"{username}|{issued_at}".encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + b"|" + sig).decode("ascii").rstrip("=")


def _verify_session(token: str, secret: bytes,
                    max_age_s: int = 30 * 24 * 3600) -> Optional[str]:
    """Returns username if the cookie is valid and unexpired, else None."""
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        # body|sig — need to find the last '|' separator that delimits sig.
        # Split from the right because sig is fixed-length 32 bytes.
        sig = raw[-32:]
        body = raw[: -33]  # also drops the separator
        expected = hmac.new(secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        username, issued_str = body.decode("utf-8").split("|", 1)
        issued = int(issued_str)
        if time.time() - issued > max_age_s:
            return None
        return username
    except Exception:
        return None


# --- principal type --------------------------------------------------------

class Principal:
    """The authenticated identity for one request."""
    __slots__ = ("user", "scopes", "token", "auth_method")

    def __init__(self, user: User, scopes: Set[str],
                 token: Optional[TokenRecord] = None,
                 auth_method: str = "bearer"):
        self.user = user
        self.scopes = scopes
        self.token = token
        self.auth_method = auth_method     # "bearer" | "cookie"

    def __repr__(self) -> str:
        return f"<Principal {self.user.username} via {self.auth_method}>"


# --- middleware ------------------------------------------------------------

# Paths that are accessible without authentication. Globs allowed.
ANONYMOUS_PATHS: Tuple[str, ...] = (
    "/api/auth/login",
    "/api/auth/whoami",          # returns 200 anonymous if no creds
    "/api/system/health",        # health check used by container
    "/api/system/status",        # very basic status — read-only
)

ANONYMOUS_PREFIXES: Tuple[str, ...] = (
    "/static/",
    "/favicon",
    "/manifest.json",
    "/sw.js",
    "/api-docs",                 # docs viewer is read-only static html/js
    "/api/routes",
    "/routes",
)


def _is_anonymous_path(path: str, no_admin_yet: bool = False) -> bool:
    if path in ANONYMOUS_PATHS:
        return True
    for p in ANONYMOUS_PREFIXES:
        if path.startswith(p):
            return True
    if path == "/" or path == "/index.html":
        return True
    # First-run gate: setup wizard endpoints are anonymous *only* while
    # no admin user exists. Self-closes the moment one is created.
    if no_admin_yet and path.startswith("/api/setup/"):
        return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Attach `request.state.principal` if credentials are valid.
    Reject (401) requests that hit a non-anonymous path with no/bad creds,
    UNLESS auth is in 'soft mode' (legacy compatibility).
    """

    def __init__(self, app, auth_manager: AuthManager, enforce: bool = True):
        super().__init__(app)
        self.auth = auth_manager
        self.enforce = enforce
        self._secret_path = str(auth_manager.config_path)
        self._cached_secret: Optional[bytes] = None
        self._cached_secret_ino: Optional[int] = None

    def _secret(self) -> bytes:
        try:
            ino = os.stat(self._secret_path).st_ino
        except OSError:
            ino = 0
        if self._cached_secret is None or ino != self._cached_secret_ino:
            self._cached_secret = _derive_session_secret(self._secret_path)
            self._cached_secret_ino = ino
        return self._cached_secret

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # WebSocket upgrade requests skip middleware in Starlette by design
        # but be defensive
        if request.scope.get("type") != "http":
            return await call_next(request)

        request.state.principal = None

        # Try to authenticate even on anonymous paths so /api/auth/whoami
        # can return useful info.
        principal = self._try_authenticate(request)
        if principal:
            request.state.principal = principal

        no_admin_yet = not any(
            (not u.disabled) and ("admins" in u.groups or "admin" in u.extra_scopes)
            for u in self.auth.users.values()
        )
        if _is_anonymous_path(path, no_admin_yet=no_admin_yet):
            return await call_next(request)

        if not self.enforce:
            # Soft mode for migration. Log but don't block.
            if not principal:
                logger.warning(f"[auth-soft] anonymous request to {path}")
            return await call_next(request)

        if not principal:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Authentication required",
                    "auth_required": True,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    def _try_authenticate(self, request: Request) -> Optional[Principal]:
        # 1. Bearer token
        auth_h = request.headers.get("authorization") or ""
        if auth_h.lower().startswith("bearer "):
            tok = auth_h[7:].strip()
            verified = self.auth.verify_token(tok)
            if verified:
                user, token_rec, scopes = verified
                return Principal(user, scopes, token_rec, "bearer")

        # 2. Session cookie
        cookie = request.cookies.get("zmm_session")
        if cookie:
            username = _verify_session(cookie, self._secret())
            if username:
                user = self.auth.users.get(username)
                if user and not user.disabled:
                    scopes = self.auth.resolve_user_scopes(username)
                    return Principal(user, scopes, None, "cookie")

        return None


# --- dependency factory ----------------------------------------------------

def require_scope(scope: str):
    """
    FastAPI dependency that asserts the request principal has `scope`.

    Usage:

        @app.get("/api/devices")
        async def list_devices(_=Depends(require_scope("device:read"))):
            ...
    """
    async def dep(request: Request) -> Principal:
        principal: Optional[Principal] = getattr(
            request.state, "principal", None
        )
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope_matches(scope, principal.scopes):
            return principal
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient scope: {scope} required",
        )
    return dep


def require_any_scope(*scopes: str):
    """Allow access if ANY of the given scopes match."""
    async def dep(request: Request) -> Principal:
        principal: Optional[Principal] = getattr(
            request.state, "principal", None
        )
        if principal is None:
            raise HTTPException(
                status_code=401, detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        for s in scopes:
            if scope_matches(s, principal.scopes):
                return principal
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient scope: one of {list(scopes)} required",
        )
    return dep


def get_principal(request: Request) -> Optional[Principal]:
    """Return the authenticated principal or None. No exception raised."""
    return getattr(request.state, "principal", None)


def require_authenticated(request: Request) -> Principal:
    """Dependency: just need to be logged in, no specific scope."""
    p = get_principal(request)
    if p is None:
        raise HTTPException(
            status_code=401, detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return p


# --- session cookie helpers ------------------------------------------------

def issue_session_cookie(username: str, secret: bytes) -> str:
    return _sign_session(username, int(time.time()), secret)