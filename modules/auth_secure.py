"""
Auth manager extension — adds MFA persistence + secure-login orchestration.

This wraps the original AuthManager (modules.auth.AuthManager) without
modifying it, by holding a reference and adding methods that compose:
  - password verification
  - brute-force tracking
  - MFA record lookup and challenge issuance
  - LAN-only scope enforcement
  - constant-time response delays

The AuthManager.load() / _save_locked() persistence is extended to round-trip
the `mfa` section in auth.yaml. We do this by attaching a SecureAuthManager
that intercepts save and adds the MFA section.

Why a wrapper instead of editing auth.py:
  Smaller diff, easier to review, easier to back out if an upgrade goes
  sideways. The original file from the previous round is untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from modules.auth import AuthManager, User, hash_password, verify_password
from modules.auth_mfa import (
    BruteForceTracker, MFARecord, MFAChallengeStore,
    constant_time_login, generate_recovery_codes, generate_totp_secret,
    hash_recovery_code, totp_provisioning_uri, verify_totp,
    LOGIN_MIN_DURATION_S,
)

logger = logging.getLogger("modules.auth_secure")


# Special scope the middleware checks for LAN-only enforcement
LAN_ONLY_SCOPE = "network:lan_only"


@dataclass
class LoginOutcome:
    """Result of the password+MFA flow. Either:
       - success=True, user=...  → issue session cookie
       - mfa_required=True, challenge=...  → ask for second-factor
       - success=False, reason=...  → reject (with optional lockout info)
    """
    success: bool = False
    user: Optional[User] = None
    mfa_required: bool = False
    challenge: Optional[str] = None
    reason: Optional[str] = None
    locked_until: Optional[float] = None
    lan_only_violation: bool = False


class SecureAuthManager:
    """Composes AuthManager + MFA records + brute-force + LAN-only checks."""

    def __init__(self, auth: AuthManager):
        self.auth = auth
        self.mfa: Dict[str, MFARecord] = {}
        self.bruteforce = BruteForceTracker()
        self.challenges = MFAChallengeStore()
        # Patch the underlying manager's save to also persist MFA records.
        self._wrap_save()
        self._load_mfa()

    # ---- persistence ---------------------------------------------------

    def _wrap_save(self) -> None:
        """Patch the wrapped AuthManager._save_locked to round-trip MFA."""
        original_save = self.auth._save_locked

    def patched_save() -> None:
        """Write users + groups + tokens + mfa in a single atomic save."""
        try:
            path = self.auth.config_path
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "groups": [g.to_dict() for g in self.auth.groups.values()],
                "users":  [u.to_dict() for u in self.auth.users.values()],
                "tokens": [t.to_dict() for t in self.auth.tokens.values()],
                "mfa":    [r.to_dict() for r in self.mfa.values()],
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
            import os
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"Failed to persist auth+MFA records: {e}")

        self.auth._save_locked = patched_save

    def _load_mfa(self) -> None:
        path = self.auth.config_path
        if not path.exists():
            return
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for r in raw.get("mfa", []) or []:
                rec = MFARecord.from_dict(r)
                self.mfa[rec.username] = rec
            if self.mfa:
                enabled = sum(1 for r in self.mfa.values() if r.enabled)
                logger.info(
                    f"MFA loaded: {len(self.mfa)} records, "
                    f"{enabled} enabled"
                )
        except Exception as e:
            logger.error(f"Failed to load MFA records: {e}")

    # ---- password + MFA orchestration ----------------------------------

    async def begin_login(
            self,
            username: str,
            password: str,
            client_ip: str,
            is_lan: bool,
    ) -> LoginOutcome:
        """
        First step: verify password, check lockouts, decide if MFA is needed.

        Returns one of:
          - success=True with .user when password OK and no MFA enrolled
          - mfa_required=True with .challenge when password OK and TOTP enrolled
          - success=False with .reason on any failure path

        Always burns at least LOGIN_MIN_DURATION_S of wall time to prevent
        timing-based username enumeration.
        """
        started_at = time.time()

        # Per-IP rate limit: cheap pre-check before doing anything expensive
        if self.bruteforce.is_ip_rate_limited(client_ip):
            logger.warning(f"[bruteforce] IP {client_ip} rate-limited")
            await constant_time_login(started_at)
            return LoginOutcome(
                success=False,
                reason="Too many login attempts from this IP. "
                       "Please wait a few minutes.",
            )

        # Per-account lockout
        locked, unlock_at = self.bruteforce.is_locked(username)
        if locked:
            await constant_time_login(started_at)
            mins = max(1, int((unlock_at - time.time()) / 60))
            return LoginOutcome(
                success=False,
                reason=(
                    f"Account locked. Try again in {mins} "
                    f"minute{'s' if mins != 1 else ''}."
                ),
                locked_until=unlock_at,
            )

        # Password check (also handles unknown user → constant-time)
        user = self.auth.verify_password(username, password)
        if not user:
            self.bruteforce.record_attempt(username, client_ip, succeeded=False)
            await constant_time_login(started_at)
            return LoginOutcome(success=False, reason="Invalid credentials")

        # LAN-only check — applies regardless of MFA state
        if not is_lan:
            user_scopes = self.auth.resolve_user_scopes(username)
            if LAN_ONLY_SCOPE in user_scopes:
                # Friendly message — see design doc
                self.bruteforce.record_attempt(
                    username, client_ip, succeeded=False)
                await constant_time_login(started_at)
                logger.warning(
                    f"[network] {username} attempted login from non-LAN "
                    f"({client_ip}) but holds {LAN_ONLY_SCOPE}"
                )
                return LoginOutcome(
                    success=False,
                    reason="This account can only sign in from "
                           "your home network.",
                    lan_only_violation=True,
                )

        # Does this user have MFA enrolled?
        rec = self.mfa.get(username)
        if rec and rec.enabled and rec.secret:
            challenge = self.challenges.issue(username)
            # NOTE: we don't record success/failure yet — MFA still pending.
            # Failure attribution happens at the MFA-verify step.
            return LoginOutcome(
                success=False,        # not yet — caller knows by mfa_required
                mfa_required=True,
                challenge=challenge,
                user=user,
            )

        # Successful single-factor login
        self.bruteforce.record_attempt(username, client_ip, succeeded=True)
        await constant_time_login(started_at)
        return LoginOutcome(success=True, user=user)

    async def complete_mfa(
            self,
            challenge: str,
            code: str,
            client_ip: str,
    ) -> LoginOutcome:
        """
        Second step of MFA login: verify TOTP or recovery code against the
        challenge, complete the login on success.
        """
        started_at = time.time()
        username = self.challenges.consume(challenge)
        if not username:
            await constant_time_login(started_at)
            return LoginOutcome(
                success=False,
                reason="MFA challenge invalid or expired. "
                       "Please sign in again.",
            )

        rec = self.mfa.get(username)
        if not rec or not rec.enabled or not rec.secret:
            await constant_time_login(started_at)
            return LoginOutcome(success=False, reason="MFA not enrolled")

        user = self.auth.users.get(username)
        if not user or user.disabled:
            await constant_time_login(started_at)
            return LoginOutcome(success=False, reason="Account unavailable")

        # Try TOTP first
        if verify_totp(rec.secret, code):
            rec.last_used_at = time.time()
            self.bruteforce.record_attempt(username, client_ip, succeeded=True)
            self.auth._save_locked()
            await constant_time_login(started_at)
            return LoginOutcome(success=True, user=user)

        # Try recovery code (single-use, hashed match)
        h = hash_recovery_code(code)
        if h in rec.recovery_code_hashes and h not in rec.used_recovery_hashes:
            rec.used_recovery_hashes.append(h)
            rec.last_used_at = time.time()
            self.bruteforce.record_attempt(username, client_ip, succeeded=True)
            self.auth._save_locked()
            remaining = (
                    len(rec.recovery_code_hashes)
                    - len(rec.used_recovery_hashes)
            )
            logger.warning(
                f"[mfa] {username} used a recovery code "
                f"({remaining} remaining)"
            )
            await constant_time_login(started_at)
            return LoginOutcome(success=True, user=user)

        # Failure — count toward the SAME lockout bucket as password failures
        self.bruteforce.record_attempt(username, client_ip, succeeded=False)
        await constant_time_login(started_at)
        return LoginOutcome(success=False, reason="Invalid MFA code")

    # ---- enrolment -----------------------------------------------------

    async def begin_enrolment(self, username: str) -> Tuple[str, str]:
        """
        Generate a fresh TOTP secret and store it as 'pending'. The user
        scans the QR code in their authenticator and confirms with one
        valid code via finish_enrolment(). Until then, MFA is not active.

        Returns (secret_b32, otpauth_uri).
        """
        if username not in self.auth.users:
            raise KeyError(username)
        secret = generate_totp_secret()
        rec = self.mfa.get(username) or MFARecord(username=username)
        rec.pending_secret = secret
        rec.pending_started_at = time.time()
        self.mfa[username] = rec
        self.auth._save_locked()
        uri = totp_provisioning_uri(secret, account_name=username, issuer="ZMM")
        return secret, uri

    async def finish_enrolment(
            self, username: str, code: str,
    ) -> List[str]:
        """
        Verify the user can read the TOTP they just set up, then activate
        MFA and return ten plaintext recovery codes (shown ONCE).
        """
        rec = self.mfa.get(username)
        if not rec or not rec.pending_secret:
            raise ValueError("No pending enrolment for this user")
        # Tolerate slow typers: ±2 windows
        if not verify_totp(rec.pending_secret, code, window=2):
            raise ValueError("Code did not verify — check phone time and try again")
        rec.secret = rec.pending_secret
        rec.pending_secret = None
        rec.pending_started_at = None
        rec.enabled = True
        rec.enrolled_at = time.time()
        # Generate recovery codes
        plaintext = generate_recovery_codes()
        rec.recovery_code_hashes = [hash_recovery_code(c) for c in plaintext]
        rec.used_recovery_hashes = []
        self.auth._save_locked()
        logger.info(f"[mfa] {username} enrolled TOTP")
        return plaintext

    async def disable_mfa(self, username: str) -> None:
        """Admin or self disable. Wipes secret and recovery codes."""
        rec = self.mfa.get(username)
        if not rec:
            return
        rec.enabled = False
        rec.secret = None
        rec.pending_secret = None
        rec.recovery_code_hashes = []
        rec.used_recovery_hashes = []
        rec.enrolled_at = None
        self.auth._save_locked()
        logger.info(f"[mfa] {username} disabled MFA")

    async def regenerate_recovery_codes(self, username: str) -> List[str]:
        rec = self.mfa.get(username)
        if not rec or not rec.enabled:
            raise ValueError("MFA not enabled")
        plaintext = generate_recovery_codes()
        rec.recovery_code_hashes = [hash_recovery_code(c) for c in plaintext]
        rec.used_recovery_hashes = []
        self.auth._save_locked()
        return plaintext

    # ---- introspection -------------------------------------------------

    def mfa_status(self, username: str) -> Dict[str, Any]:
        rec = self.mfa.get(username)
        if not rec:
            return {"username": username, "enabled": False, "enrolled": False}
        remaining = (
            len(rec.recovery_code_hashes) - len(rec.used_recovery_hashes)
            if rec.recovery_code_hashes else 0
        )
        return {
            "username": username,
            "enabled": rec.enabled,
            "enrolled": bool(rec.secret),
            "pending_enrolment": bool(rec.pending_secret),
            "enrolled_at": rec.enrolled_at,
            "last_used_at": rec.last_used_at,
            "recovery_codes_remaining": remaining,
        }

    def list_locked_accounts(self) -> List[Dict[str, Any]]:
        return self.bruteforce.list_locked()

    def admin_unlock(self, username: str) -> bool:
        return self.bruteforce.admin_unlock(username)


# --- Singleton ------------------------------------------------------------

_secure: Optional[SecureAuthManager] = None


def get_secure_auth_manager() -> Optional[SecureAuthManager]:
    return _secure


def set_secure_auth_manager(s: SecureAuthManager) -> None:
    global _secure
    _secure = s