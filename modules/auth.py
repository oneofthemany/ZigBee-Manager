"""
ZMM Authentication & Authorization

Concepts
--------
- **User**         A human identity. Has a username, optionally a password
                   (for browser login), zero or more group memberships, and
                   zero or more issued API tokens.
- **Group**        A named bag of scopes. Users inherit the union of scopes
                   from every group they belong to, plus any directly
                   assigned scopes on the user.
- **Token**        An opaque bearer token (32 bytes, base64url) belonging
                   to one user. Has a label (e.g. "Sean's Pixel"), optional
                   expiry, optional scope subset narrower than the owning
                   user, and an optional `device_id` (free-form, e.g. an
                   Android Settings.Secure.ANDROID_ID for revocation UX).
- **Scope**        A dotted string like `presence:write:sean` or `device:*`.
                   Wildcards match any segment at that position.

Threat model
------------
- We are NOT a public auth provider; the gateway is on a home LAN with
  optional remote exposure. The bar is "an attacker on the network can't
  spoof presence, and a stolen device token can be revoked individually."
- Tokens are stored hashed (SHA-256). The plaintext is shown ONCE at issue.
- Passwords are stored as PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte
  salt, base64-encoded. No external password-hashing dep needed.
- Tokens are 256 bits of entropy from `secrets.token_urlsafe(32)`.
- We do NOT implement OAuth, OIDC, JWT, refresh tokens, or rotation here.
  Tokens are static until revoked or expired. This is by design: simple
  enough to reason about, sufficient for the threat model.

Persistence
-----------
- `data/auth.yaml`  — single source of truth. Atomic writes via temp-file
                      rename. Loaded once at start, mutations save eagerly.

Bootstrap
---------
- If the file doesn't exist on start, an `admin` user is created with a
  random password printed to logs ONCE. The user can then change it via
  the UI. This avoids hardcoded defaults.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
import yaml
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("modules.auth")

CONFIG_PATH = Path("./data/auth.yaml")

# --- Scope definitions ----------------------------------------------------

# Built-in scopes shipped with ZMM. Custom scopes are allowed but these are
# the ones the UI surfaces and the routes consult.
KNOWN_SCOPES: Dict[str, str] = {
    "admin":                  "Full access (implies every other scope).",
    "device:read":            "Read device state, configs and lists.",
    "device:write":           "Send commands to devices, change settings.",
    "automation:read":        "View automations and rule definitions.",
    "automation:write":       "Create, modify, delete automations.",
    "group:read":             "View Zigbee groups.",
    "group:write":            "Create, modify, delete Zigbee groups.",
    "matter:read":            "View Matter nodes and clusters.",
    "matter:write":           "Commission, remove, control Matter devices.",
    "system:read":            "View system status, telemetry, logs.",
    "system:write":           "Restart services, edit config, run upgrades.",
    "presence:read":          "Read all presence users' state.",
    "presence:write":         "Update any presence user's location.",
    # Per-user presence scopes are checked dynamically as
    # presence:write:<user_id>.  e.g. "presence:write:sean".
}

# Built-in groups created on first run if no auth.yaml exists.
DEFAULT_GROUPS: Dict[str, List[str]] = {
    "admins":   ["admin"],
    "users":    ["device:read", "device:write",
                 "automation:read", "automation:write",
                 "group:read", "group:write",
                 "matter:read", "matter:write",
                 "system:read",
                 "presence:read", "presence:write:*"],
    "viewers":  ["device:read", "automation:read", "group:read",
                 "matter:read", "system:read", "presence:read"],
    "mobile":   ["presence:read"],   # phones get scoped tokens, not group access
}


# --- Hashing helpers ------------------------------------------------------

PBKDF2_ITER = 200_000
PBKDF2_SALT_BYTES = 16


def hash_password(plain: str) -> str:
    """Return PBKDF2-HMAC-SHA256 hash; format: pbkdf2$<iter>$<salt_b64>$<hash_b64>."""
    salt = os.urandom(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, PBKDF2_ITER)
    return "pbkdf2${}${}${}".format(
        PBKDF2_ITER,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(plain: str, encoded: str) -> bool:
    if not encoded or not encoded.startswith("pbkdf2$"):
        return False
    try:
        _, iter_s, salt_b64, hash_b64 = encoded.split("$", 3)
        iters = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def hash_token(plain: str) -> str:
    """SHA-256 of the token string. Tokens have enough entropy not to need salt."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """256 bits of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


# --- Scope matching -------------------------------------------------------

def scope_matches(required: str, granted: Iterable[str]) -> bool:
    """
    True if any granted scope satisfies `required`.

    Rules:
      - 'admin' always matches.
      - Wildcards: 'device:*' matches 'device:read', 'device:write',
        and 'device:write:thing'.
      - Per-resource wildcards stop at separators, so 'presence:write:*'
        matches 'presence:write:sean' but not 'presence:read:sean'.
    """
    req_parts = required.split(":")
    for g in granted:
        if g == "admin":
            return True
        if g == required:
            return True
        gp = g.split(":")
        if len(gp) > len(req_parts):
            continue
        ok = True
        for i, seg in enumerate(gp):
            if seg == "*":
                continue
            if seg != req_parts[i]:
                ok = False
                break
        if ok and len(gp) == len(req_parts):
            return True
        # Allow shorter granted to cover deeper required only via explicit '*'
        if ok and len(gp) < len(req_parts) and gp[-1] == "*":
            return True
    return False


# --- Data model -----------------------------------------------------------

@dataclass
class Group:
    name: str
    scopes: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TokenRecord:
    token_hash: str           # sha256 of plaintext (storage)
    label: str                # human label, e.g. "Sean's Pixel"
    user: str                 # username this token belongs to
    scopes: List[str]         # subset granted to this token
    created_at: float
    last_used_at: Optional[float] = None
    expires_at: Optional[float] = None
    device_id: Optional[str] = None    # opaque device identifier
    revoked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_active(self, now: Optional[float] = None) -> bool:
        if self.revoked:
            return False
        if self.expires_at is not None:
            now = now or time.time()
            if now >= self.expires_at:
                return False
        return True

    def public_view(self) -> Dict[str, Any]:
        d = self.to_dict()
        d.pop("token_hash", None)
        # Short identifier so the UI can address the token without exposing
        # the hash. Stable for the token's lifetime.
        d["id"] = self.token_hash[:12]
        return d


@dataclass
class User:
    username: str
    password_hash: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    extra_scopes: List[str] = field(default_factory=list)   # direct grants
    disabled: bool = False
    created_at: float = field(default_factory=time.time)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def public_view(self) -> Dict[str, Any]:
        d = self.to_dict()
        d.pop("password_hash", None)
        d["has_password"] = bool(self.password_hash)
        return d


# --- Manager --------------------------------------------------------------

class AuthManager:
    """
    Thread-safe-ish (single-asyncio-loop) auth store. All mutations go
    through `_save_locked()` so reads can stay lock-free for the hot path.
    """

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = Path(config_path)
        self._lock = asyncio.Lock()
        self.users: Dict[str, User] = {}
        self.groups: Dict[str, Group] = {}
        self.tokens: Dict[str, TokenRecord] = {}     # keyed by token_hash
        self._loaded = False

    # ---- lifecycle ------------------------------------------------------
    def load(self) -> None:
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    raw = yaml.safe_load(f) or {}
                for g in raw.get("groups", []) or []:
                    grp = Group(name=g["name"],
                                scopes=list(g.get("scopes") or []),
                                description=g.get("description") or "")
                    self.groups[grp.name] = grp
                for u in raw.get("users", []) or []:
                    user = User(
                        username=u["username"],
                        password_hash=u.get("password_hash"),
                        groups=list(u.get("groups") or []),
                        extra_scopes=list(u.get("extra_scopes") or []),
                        disabled=bool(u.get("disabled", False)),
                        created_at=float(u.get("created_at", time.time())),
                        description=u.get("description") or "",
                    )
                    self.users[user.username] = user
                for t in raw.get("tokens", []) or []:
                    tok = TokenRecord(
                        token_hash=t["token_hash"],
                        label=t.get("label") or "",
                        user=t["user"],
                        scopes=list(t.get("scopes") or []),
                        created_at=float(t.get("created_at", time.time())),
                        last_used_at=t.get("last_used_at"),
                        expires_at=t.get("expires_at"),
                        device_id=t.get("device_id"),
                        revoked=bool(t.get("revoked", False)),
                    )
                    self.tokens[tok.token_hash] = tok
                logger.info(
                    f"Auth loaded: {len(self.users)} users, "
                    f"{len(self.groups)} groups, {len(self.tokens)} tokens"
                )
            except Exception as e:
                logger.error(f"Failed to load auth.yaml: {e}")
        else:
            self._bootstrap()
        self._loaded = True

    def _bootstrap(self) -> None:
        """First-run setup: default groups + admin user with random password."""
        for name, scopes in DEFAULT_GROUPS.items():
            self.groups[name] = Group(name=name, scopes=list(scopes),
                                      description=f"Default {name} group")
        admin_password = secrets.token_urlsafe(12)
        self.users["admin"] = User(
            username="admin",
            password_hash=hash_password(admin_password),
            groups=["admins"],
            description="Initial administrator account",
        )
        self._save_locked()
        # Print exactly once. The admin password will not be recoverable.
        logger.warning("=" * 70)
        logger.warning("FIRST-RUN AUTH BOOTSTRAP")
        logger.warning(f"  Admin username: admin")
        logger.warning(f"  Admin password: {admin_password}")
        logger.warning("  Change it via Settings → Users as soon as possible.")
        logger.warning("=" * 70)

    def _save_locked(self) -> None:
        """Write atomically. Caller must hold _lock OR be in init."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "groups": [g.to_dict() for g in self.groups.values()],
                "users":  [u.to_dict() for u in self.users.values()],
                "tokens": [t.to_dict() for t in self.tokens.values()],
            }
            tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp, self.config_path)
            # Tighten perms — token hashes are hashes, but treat as secrets anyway
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"Failed to save auth.yaml: {e}")

    # ---- scope resolution ----------------------------------------------
    def resolve_user_scopes(self, username: str) -> Set[str]:
        u = self.users.get(username)
        if not u or u.disabled:
            return set()
        scopes: Set[str] = set(u.extra_scopes)
        for g in u.groups:
            grp = self.groups.get(g)
            if grp:
                scopes.update(grp.scopes)
        return scopes

    # ---- user CRUD ------------------------------------------------------
    async def create_user(
            self, username: str, password: Optional[str],
            groups: Optional[List[str]] = None,
            extra_scopes: Optional[List[str]] = None,
            description: str = "",
    ) -> User:
        async with self._lock:
            if username in self.users:
                raise ValueError(f"User '{username}' already exists")
            if not _is_valid_id(username):
                raise ValueError(
                    "Username must be 2-32 chars, alphanumeric/_/-")
            user = User(
                username=username,
                password_hash=hash_password(password) if password else None,
                groups=list(groups or []),
                extra_scopes=list(extra_scopes or []),
                description=description,
            )
            for g in user.groups:
                if g not in self.groups:
                    raise ValueError(f"Unknown group: {g}")
            self.users[username] = user
            self._save_locked()
            return user

    async def update_user(self, username: str, **changes: Any) -> User:
        async with self._lock:
            user = self.users.get(username)
            if not user:
                raise KeyError(username)
            if "password" in changes:
                pw = changes.pop("password")
                user.password_hash = hash_password(pw) if pw else None
            for key in ("groups", "extra_scopes", "disabled", "description"):
                if key in changes:
                    setattr(user, key, changes[key])
            for g in user.groups:
                if g not in self.groups:
                    raise ValueError(f"Unknown group: {g}")
            self._save_locked()
            return user

    async def delete_user(self, username: str) -> None:
        async with self._lock:
            if username not in self.users:
                raise KeyError(username)
            # Refuse to delete the last admin — bricking the system is bad UX
            remaining_admins = [
                u for name, u in self.users.items()
                if name != username and "admin" in self.resolve_user_scopes(name)
            ]
            if (
                    "admin" in self.resolve_user_scopes(username)
                    and not remaining_admins
            ):
                raise ValueError("Cannot delete the last admin user")
            del self.users[username]
            # Cascade: revoke all their tokens
            for h, t in list(self.tokens.items()):
                if t.user == username:
                    del self.tokens[h]
            self._save_locked()

    # ---- group CRUD -----------------------------------------------------
    async def create_group(self, name: str, scopes: List[str],
                           description: str = "") -> Group:
        async with self._lock:
            if name in self.groups:
                raise ValueError(f"Group '{name}' already exists")
            if not _is_valid_id(name):
                raise ValueError("Group name must be 2-32 chars, alphanumeric/_/-")
            grp = Group(name=name, scopes=list(scopes), description=description)
            self.groups[name] = grp
            self._save_locked()
            return grp

    async def update_group(self, name: str, **changes: Any) -> Group:
        async with self._lock:
            grp = self.groups.get(name)
            if not grp:
                raise KeyError(name)
            for key in ("scopes", "description"):
                if key in changes:
                    setattr(grp, key, changes[key])
            self._save_locked()
            return grp

    async def delete_group(self, name: str) -> None:
        async with self._lock:
            if name not in self.groups:
                raise KeyError(name)
            # Detach from every user before removing
            for u in self.users.values():
                if name in u.groups:
                    u.groups.remove(name)
            del self.groups[name]
            self._save_locked()

    # ---- token issuance / revocation ------------------------------------
    async def issue_token(
            self,
            username: str,
            label: str,
            scopes: Optional[List[str]] = None,
            expires_in_s: Optional[int] = None,
            device_id: Optional[str] = None,
    ) -> Tuple[str, TokenRecord]:
        """Returns (plaintext, record). Plaintext is shown ONCE."""
        async with self._lock:
            user = self.users.get(username)
            if not user:
                raise KeyError(username)
            if user.disabled:
                raise ValueError("User is disabled")

            # If scopes weren't specified, give the token the full set the
            # user has. Otherwise, intersect with the user's scopes — a
            # token can never grant more than the owning user has.
            user_scopes = self.resolve_user_scopes(username)
            if scopes is None:
                granted = sorted(user_scopes)
            else:
                granted = []
                for s in scopes:
                    if s == "admin" and "admin" not in user_scopes:
                        raise ValueError("Cannot grant admin to non-admin user")
                    if s in user_scopes or scope_matches(s, user_scopes):
                        granted.append(s)
                    else:
                        raise ValueError(f"User lacks scope: {s}")

            plaintext = generate_token()
            rec = TokenRecord(
                token_hash=hash_token(plaintext),
                label=label or "(unlabeled)",
                user=username,
                scopes=granted,
                created_at=time.time(),
                expires_at=time.time() + expires_in_s if expires_in_s else None,
                device_id=device_id,
            )
            self.tokens[rec.token_hash] = rec
            self._save_locked()
            return plaintext, rec

    async def revoke_token(self, token_id: str) -> None:
        """Revoke by short id (first 12 hex chars of hash) or full hash."""
        async with self._lock:
            target = None
            for h, t in self.tokens.items():
                if h == token_id or h.startswith(token_id):
                    target = h
                    break
            if not target:
                raise KeyError(token_id)
            self.tokens[target].revoked = True
            self._save_locked()

    async def delete_token(self, token_id: str) -> None:
        """Hard-delete a token record. Use revoke for audit-friendly removal."""
        async with self._lock:
            target = None
            for h in self.tokens:
                if h == token_id or h.startswith(token_id):
                    target = h
                    break
            if not target:
                raise KeyError(token_id)
            del self.tokens[target]
            self._save_locked()

    # ---- verification (hot path) ----------------------------------------
    def verify_token(self, plaintext: str) -> Optional[Tuple[User, TokenRecord, Set[str]]]:
        if not plaintext:
            return None
        h = hash_token(plaintext)
        t = self.tokens.get(h)
        if not t or not t.is_active():
            return None
        u = self.users.get(t.user)
        if not u or u.disabled:
            return None
        # The token's effective scopes are the intersection of the token's
        # configured scopes with what the user currently has. (Group changes
        # apply retroactively to live tokens — by design.)
        user_scopes = self.resolve_user_scopes(u.username)
        effective: Set[str] = set()
        for s in t.scopes:
            if s in user_scopes or scope_matches(s, user_scopes):
                effective.add(s)
        # Touch last_used (best effort, no save here — saved by a periodic
        # flush task in the manager wrapper if desired)
        t.last_used_at = time.time()
        return (u, t, effective)

    def verify_password(self, username: str, password: str) -> Optional[User]:
        u = self.users.get(username)
        if not u or u.disabled or not u.password_hash:
            return None
        if verify_password(password, u.password_hash):
            return u
        return None

    # ---- listings -------------------------------------------------------
    def list_users(self) -> List[Dict[str, Any]]:
        out = []
        for u in self.users.values():
            v = u.public_view()
            v["effective_scopes"] = sorted(self.resolve_user_scopes(u.username))
            out.append(v)
        return out

    def list_groups(self) -> List[Dict[str, Any]]:
        return [g.to_dict() for g in self.groups.values()]

    def list_tokens(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.tokens.values():
            if username and t.user != username:
                continue
            out.append(t.public_view())
        return out


# --- helpers ---------------------------------------------------------------

import re

_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,32}$")


def _is_valid_id(s: str) -> bool:
    return bool(s and _VALID_ID_RE.match(s))


# --- module singleton ------------------------------------------------------

_manager: Optional[AuthManager] = None


def get_auth_manager() -> Optional[AuthManager]:
    return _manager


def set_auth_manager(mgr: AuthManager) -> None:
    global _manager
    _manager = mgr