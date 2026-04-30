"""
ZMM MFA & brute-force protection.

Implements:
- TOTP (RFC 6238) with no external deps — uses stdlib hmac/hashlib only.
- Recovery codes — 10 single-use codes, hashed at rest.
- Per-account exponential lockout (1 → 5 → 15 → 60 minutes, capped).
- Per-IP sliding-window rate limiter.
- Constant-ish-time login response delay to mask "user exists" timing.
- otpauth:// URI generation for QR-code enrolment.

Why no external deps:
  pyotp, qrcode, etc. are well-engineered, but adding deps to a self-hosted
  gateway is friction. RFC 6238 is 30 lines once you've got HMAC. The QR
  code is rendered client-side by an existing JS lib (the UI uses one; if
  not, we ship a minimal SVG generator separately).

Storage:
  MFA records live alongside auth in data/auth.yaml under a `mfa` section,
  one record per user. Recovery code hashes are sha256 (codes have enough
  entropy to skip salting).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.auth_mfa")

# --- Constants -------------------------------------------------------------

TOTP_DIGITS = 6
TOTP_PERIOD = 30          # seconds
TOTP_WINDOW = 1           # ± steps tolerated for clock skew
TOTP_SECRET_BYTES = 20    # 160 bits — RFC recommendation

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 8   # ~13 chars after b32 encode

# Brute-force defaults — overridable via config
LOCKOUT_THRESHOLDS = [
    (3, 60),         # 3 failures → 1 min
    (5, 5 * 60),     # 5 failures → 5 min
    (8, 15 * 60),    # 8 failures → 15 min
    (12, 60 * 60),   # 12 failures → 1 hour (cap)
]
ATTEMPT_WINDOW_S = 30 * 60     # rolling window for counting failures
IP_RATE_WINDOW_S = 5 * 60      # IP rate-limit window
IP_RATE_MAX = 30               # max attempts per IP per window
LOGIN_MIN_DURATION_S = 0.25    # constant-time floor

# MFA challenge token TTL
MFA_CHALLENGE_TTL_S = 300      # 5 minutes — generous for typing in code


# --- Base32 helpers --------------------------------------------------------

def _b32_encode(data: bytes) -> str:
    """Standard RFC 4648 base32, uppercase, no padding (authenticator apps
    are usually fine with or without padding; we strip it for cleanliness)."""
    return base64.b32encode(data).decode("ascii").rstrip("=")


def _b32_decode(s: str) -> bytes:
    """Tolerant base32 decode — uppercase, strip whitespace, re-pad."""
    s = s.strip().upper().replace(" ", "").replace("-", "")
    pad = (-len(s)) % 8
    return base64.b32decode(s + ("=" * pad))


# --- TOTP (RFC 6238) -------------------------------------------------------

def generate_totp_secret() -> str:
    """Return a base32-encoded 160-bit TOTP secret."""
    return _b32_encode(os.urandom(TOTP_SECRET_BYTES))


def _hotp(secret_bytes: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """RFC 4226 HOTP. Counter is the time step for TOTP."""
    msg = struct.pack(">Q", counter)
    h = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = (
            ((h[offset] & 0x7F) << 24)
            | ((h[offset + 1] & 0xFF) << 16)
            | ((h[offset + 2] & 0xFF) << 8)
            | (h[offset + 3] & 0xFF)
    )
    code = code_int % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(
        secret_b32: str,
        code: str,
        timestamp: Optional[float] = None,
        window: int = TOTP_WINDOW,
) -> bool:
    """Return True if `code` matches `secret` within ±`window` time steps."""
    if not code or not secret_b32:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    try:
        secret_bytes = _b32_decode(secret_b32)
    except Exception:
        return False

    now = timestamp if timestamp is not None else time.time()
    counter = int(now // TOTP_PERIOD)
    for offset in range(-window, window + 1):
        candidate = _hotp(secret_bytes, counter + offset)
        if hmac.compare_digest(candidate, code):
            return True
    return False


def totp_provisioning_uri(
        secret_b32: str,
        account_name: str,
        issuer: str = "ZMM",
) -> str:
    """
    Build the otpauth:// URI that authenticator apps consume.
    QR code is generated client-side from this string.
    """
    label = f"{issuer}:{account_name}"
    params = {
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(TOTP_DIGITS),
        "period": str(TOTP_PERIOD),
    }
    query = urllib.parse.urlencode(params)
    return f"otpauth://totp/{urllib.parse.quote(label)}?{query}"


# --- Recovery codes --------------------------------------------------------

def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> List[str]:
    """
    Returns plaintext codes formatted XXXX-XXXX (4-4) — friendly to type.
    Plaintext should only ever be returned to the user once.
    """
    codes = []
    for _ in range(count):
        raw = os.urandom(RECOVERY_CODE_BYTES)
        b32 = _b32_encode(raw)
        # Slice to 8 chars, group as 4-4 for readability
        b32 = b32[:8]
        codes.append(f"{b32[:4]}-{b32[4:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Normalize then sha256. Codes have ~64 bits entropy so no salt needed."""
    norm = code.strip().upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(b"zmm-recovery:" + norm.encode("ascii")).hexdigest()


# --- Data model ------------------------------------------------------------

@dataclass
class MFARecord:
    """Per-user MFA configuration."""
    username: str
    enabled: bool = False
    secret: Optional[str] = None              # base32 TOTP secret
    pending_secret: Optional[str] = None      # secret during enrolment
    pending_started_at: Optional[float] = None
    recovery_code_hashes: List[str] = field(default_factory=list)
    used_recovery_hashes: List[str] = field(default_factory=list)
    enrolled_at: Optional[float] = None
    last_used_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MFARecord":
        return cls(
            username=d["username"],
            enabled=bool(d.get("enabled", False)),
            secret=d.get("secret"),
            pending_secret=d.get("pending_secret"),
            pending_started_at=d.get("pending_started_at"),
            recovery_code_hashes=list(d.get("recovery_code_hashes") or []),
            used_recovery_hashes=list(d.get("used_recovery_hashes") or []),
            enrolled_at=d.get("enrolled_at"),
            last_used_at=d.get("last_used_at"),
        )


# --- Brute-force tracker ---------------------------------------------------

@dataclass
class _Attempt:
    ts: float
    succeeded: bool


class BruteForceTracker:
    """
    Per-username and per-IP failure tracking. Memory-resident; not persisted
    by default. Process restart resets state, which is acceptable — most
    attacks happen within minutes, and persisting across restarts is a
    defence-in-depth nice-to-have not a requirement.
    """

    def __init__(
            self,
            thresholds: List[Tuple[int, int]] = LOCKOUT_THRESHOLDS,
            attempt_window_s: float = ATTEMPT_WINDOW_S,
            ip_rate_window_s: float = IP_RATE_WINDOW_S,
            ip_rate_max: int = IP_RATE_MAX,
    ):
        # Sorted ascending by failure-count so the highest applicable
        # threshold wins. Take a copy so caller can't mutate.
        self.thresholds = sorted(thresholds, key=lambda t: t[0])
        self.attempt_window_s = attempt_window_s
        self.ip_rate_window_s = ip_rate_window_s
        self.ip_rate_max = ip_rate_max

        # username → deque[_Attempt]
        self._attempts: Dict[str, Deque[_Attempt]] = {}
        # ip → deque[float]   (timestamps of any attempt)
        self._ip_attempts: Dict[str, Deque[float]] = {}
        # username → unlock_at_ts
        self._locked_until: Dict[str, float] = {}

    def _prune(self, dq: Deque, cutoff: float, key=lambda x: x.ts) -> None:
        while dq and key(dq[0]) < cutoff:
            dq.popleft()

    def is_locked(self, username: str) -> Tuple[bool, Optional[float]]:
        until = self._locked_until.get(username)
        if not until:
            return False, None
        if time.time() >= until:
            self._locked_until.pop(username, None)
            return False, None
        return True, until

    def is_ip_rate_limited(self, ip: str) -> bool:
        if not ip:
            return False
        dq = self._ip_attempts.get(ip)
        if not dq:
            return False
        now = time.time()
        # Inline prune of timestamps deque (no `.ts` attribute)
        while dq and dq[0] < now - self.ip_rate_window_s:
            dq.popleft()
        return len(dq) >= self.ip_rate_max

    def record_attempt(
            self, username: str, ip: Optional[str], succeeded: bool,
    ) -> Tuple[bool, Optional[float]]:
        """
        Record one login attempt. Returns (locked_now, unlock_at_ts).
        Successful attempts clear the username's failure history.
        """
        now = time.time()

        # Track IP regardless of success — tight rate limit on volume
        if ip:
            ip_dq = self._ip_attempts.setdefault(ip, deque())
            ip_dq.append(now)

        if username:
            user_dq = self._attempts.setdefault(username, deque())
            self._prune(user_dq, now - self.attempt_window_s)

            if succeeded:
                # Wipe history on success — no point holding stale failures
                user_dq.clear()
                self._locked_until.pop(username, None)
                return False, None

            user_dq.append(_Attempt(ts=now, succeeded=False))
            failure_count = len(user_dq)

            # Find the highest threshold this count meets
            applicable_lock = None
            for threshold, lock_s in self.thresholds:
                if failure_count >= threshold:
                    applicable_lock = lock_s

            if applicable_lock is not None:
                unlock_at = now + applicable_lock
                # Don't reduce an existing longer lock
                cur = self._locked_until.get(username, 0)
                if unlock_at > cur:
                    self._locked_until[username] = unlock_at
                logger.warning(
                    f"[bruteforce] {username} locked for "
                    f"{applicable_lock}s after {failure_count} failures"
                )
                return True, unlock_at

        return False, None

    def admin_unlock(self, username: str) -> bool:
        """Force-unlock a user (admin override). Returns True if was locked."""
        had = username in self._locked_until
        self._locked_until.pop(username, None)
        self._attempts.pop(username, None)
        if had:
            logger.info(f"[bruteforce] {username} admin-unlocked")
        return had

    def list_locked(self) -> List[Dict[str, Any]]:
        now = time.time()
        out = []
        for u, until in list(self._locked_until.items()):
            if now >= until:
                self._locked_until.pop(u, None)
                continue
            failures = len(self._attempts.get(u, []))
            out.append({
                "username": u,
                "unlock_at": until,
                "remaining_s": int(until - now),
                "failure_count": failures,
            })
        return out


# --- MFA challenge tokens (in-memory, short-lived) ------------------------

class MFAChallengeStore:
    """
    Tracks "user has passed password but not yet TOTP" sessions.
    A challenge is created on successful password verification; the client
    presents it along with a TOTP/recovery code on the second login step.

    Entries are short-lived (5 min) and consumed on use.
    """

    def __init__(self, ttl_s: float = MFA_CHALLENGE_TTL_S):
        self.ttl_s = ttl_s
        self._challenges: Dict[str, Tuple[str, float]] = {}   # ch → (user, exp)

    def issue(self, username: str) -> str:
        ch = secrets.token_urlsafe(24)
        self._challenges[ch] = (username, time.time() + self.ttl_s)
        self._gc()
        return ch

    def _gc(self) -> None:
        now = time.time()
        for k in list(self._challenges.keys()):
            if self._challenges[k][1] < now:
                del self._challenges[k]

    def consume(self, challenge: str) -> Optional[str]:
        """Returns username if challenge is valid; deletes it. Else None."""
        rec = self._challenges.pop(challenge, None)
        if not rec:
            return None
        username, exp = rec
        if time.time() >= exp:
            return None
        return username

    def peek(self, challenge: str) -> Optional[str]:
        """For debug only — don't use in production code paths."""
        rec = self._challenges.get(challenge)
        if not rec:
            return None
        if time.time() >= rec[1]:
            return None
        return rec[0]


# --- Constant-time response helper ----------------------------------------

async def constant_time_login(
        started_at: float,
        floor_s: float = LOGIN_MIN_DURATION_S,
) -> None:
    """
    Sleep so login responses always take at least `floor_s`. This stops
    timing attacks distinguishing "user does not exist" from "wrong password".
    """
    elapsed = time.time() - started_at
    remaining = floor_s - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)