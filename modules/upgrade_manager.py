"""
Upgrade Manager — blue-green container upgrades for ZMM.

Architecture:
  - App polls GitHub tags API for new versions
  - App writes trigger files to a shared volume directory
  - Host-side watcher (systemd-path unit OR polling fallback) picks up triggers
  - Host-side upgrade.sh executes: build new image → swap containers → rollback on failure
  - State is persisted in ~/.zigbee-matter-manager/state/version.json

The running app NEVER directly calls podman/docker. All container operations
happen on the host via the trigger mechanism. This keeps the container
unprivileged and works across any Linux + podman/docker combo.

Files on disk (in the upgrade shared volume — /app/data/upgrade inside container):
  trigger          — created by app; action + payload; watcher deletes after reading
  status.json      — watcher writes progress; app polls
  build.log        — full build output; app streams to UI
  lock             — prevents concurrent upgrade operations

State (~/.zigbee-matter-manager/state/version.json):
  Installed version, previous version, auto-update prefs, last check time
"""
import asyncio
import json
import logging
import os
import platform
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("modules.upgrade_manager")

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# /app/data/upgrade — shared with host via bind mount
UPGRADE_DIR = os.path.join(APP_DIR, "data", "upgrade")
TRIGGER_FILE = os.path.join(UPGRADE_DIR, "trigger")
STATUS_FILE = os.path.join(UPGRADE_DIR, "status.json")
BUILD_LOG_FILE = os.path.join(UPGRADE_DIR, "build.log")
LOCK_FILE = os.path.join(UPGRADE_DIR, "lock")

# /app/data/state — persistent app state (version tracking)
STATE_DIR = os.path.join(APP_DIR, "data", "state")
VERSION_STATE_FILE = os.path.join(STATE_DIR, "version.json")

# Baked into image at build time
APP_VERSION_FILE = os.path.join(APP_DIR, "VERSION")

# GitHub repo (can be overridden via config)
DEFAULT_REPO = "oneofthemany/ZigBee-Matter-Manager"
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# VALID STATES
# ---------------------------------------------------------------------------
VALID_STATES = {
    "idle",          # Nothing happening
    "checking",      # Polling GitHub for new versions
    "building",      # Host is building new image
    "ready_to_swap", # New image built successfully, awaiting swap
    "swapping",      # Container swap in progress
    "rolling_back",  # Rollback in progress
    "failed",        # Last operation failed
}

VALID_ACTIONS = {"install_watcher", "build", "swap", "rollback", "cancel", "gc"}


# ---------------------------------------------------------------------------
# VERSION PARSING / COMPARISON
# ---------------------------------------------------------------------------
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_version(v: str) -> Optional[Tuple[int, int, int]]:
    """Parse 'v1.2.3' or '1.2.3' (with optional pre-release) into (1, 2, 3)."""
    if not v:
        return None
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def normalise_version(v: str) -> str:
    """Strip leading 'v'. 'v1.2.3' -> '1.2.3'."""
    if not v:
        return ""
    return v.strip().lstrip("vV")


def compare_versions(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if equal, 1 if a>b. Unknown = lowest."""
    pa = parse_version(a)
    pb = parse_version(b)
    if pa is None and pb is None:
        return 0
    if pa is None:
        return -1
    if pb is None:
        return 1
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# ---------------------------------------------------------------------------
# ARCHITECTURE DETECTION
# ---------------------------------------------------------------------------
def detect_architecture() -> str:
    """Return a normalised architecture string matching image tag conventions."""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7") or m == "armv7l":
        return "armv7"
    return m  # unknown — pass through


# ---------------------------------------------------------------------------
# STATE PERSISTENCE
# ---------------------------------------------------------------------------
DEFAULT_STATE = {
    "current_version": "unknown",
    "current_image_tag": "",
    "installed_at": None,
    "previous_version": None,
    "previous_image_tag": None,
    "latest_available": None,
    "latest_release_notes": None,
    "latest_release_url": None,
    "last_check": None,
    "auto_update": False,
    "auto_update_window": {"start": "03:00", "end": "05:00"},
    "channel": "stable",
    "retention_count": 2,
    "repo": DEFAULT_REPO,
    "upgrade_state": "idle",
    "upgrade_error": None,
    "architecture": detect_architecture(),
    "watcher_installed": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_dirs():
    os.makedirs(UPGRADE_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


def load_state() -> Dict[str, Any]:
    """Load version state, merging with defaults so missing keys don't explode."""
    _ensure_dirs()
    state = dict(DEFAULT_STATE)

    # Seed current_version from baked VERSION file if it exists
    if os.path.exists(APP_VERSION_FILE):
        try:
            with open(APP_VERSION_FILE, "r") as f:
                ver = f.read().strip()
                if ver:
                    state["current_version"] = normalise_version(ver)
        except Exception as e:
            logger.warning(f"Failed to read VERSION file: {e}")

    if os.path.exists(VERSION_STATE_FILE):
        try:
            with open(VERSION_STATE_FILE, "r") as f:
                saved = json.load(f) or {}
            # Merge saved over defaults (preserve defaults for missing keys)
            for k, v in saved.items():
                state[k] = v
            # Make sure current_version from VERSION file wins if it's set
            if os.path.exists(APP_VERSION_FILE):
                try:
                    with open(APP_VERSION_FILE, "r") as f:
                        ver = f.read().strip()
                        if ver:
                            state["current_version"] = normalise_version(ver)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to load version state ({VERSION_STATE_FILE}): {e}")

    # Always refresh architecture at load time
    state["architecture"] = detect_architecture()
    return state


def save_state(state: Dict[str, Any]):
    """Atomic write of state file."""
    _ensure_dirs()
    tmp = VERSION_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=False)
        os.replace(tmp, VERSION_STATE_FILE)
    except Exception as e:
        logger.error(f"Failed to save version state: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def update_state(**changes) -> Dict[str, Any]:
    """Load state, apply partial update, save."""
    state = load_state()
    for k, v in changes.items():
        state[k] = v
    save_state(state)
    return state


# ---------------------------------------------------------------------------
# TRIGGER / STATUS (the IPC between container and host watcher)
# ---------------------------------------------------------------------------
def read_status() -> Dict[str, Any]:
    """Read host-reported status. Returns a default dict if missing."""
    _ensure_dirs()
    default = {
        "state": "idle",
        "target_version": None,
        "started_at": None,
        "updated_at": None,
        "progress_percent": 0,
        "current_step": "",
        "error": None,
    }
    if not os.path.exists(STATUS_FILE):
        return default
    try:
        with open(STATUS_FILE, "r") as f:
            data = json.load(f) or {}
        # Merge with defaults
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        logger.warning(f"Failed to read status.json: {e}")
        return default


def _is_lock_stale(lock_info: str) -> bool:
    """
    A lock is stale if:
      - The PID it claims is no longer running, OR
      - The lock is older than 60 minutes (a build can run that long, but if
        we exceed 60 min with no host activity, something has gone wrong).

    Lock file format: "PID TIMESTAMP ACTION" (written by upgrade.sh)
    """
    if not lock_info.strip():
        return True
    parts = lock_info.split(maxsplit=2)

    # Check PID liveness
    if parts and parts[0].isdigit():
        pid = int(parts[0])
        try:
            # Sending signal 0 checks process existence without signalling.
            os.kill(pid, 0)
            # Process exists — lock is live
        except ProcessLookupError:
            logger.info(f"Lock holder PID {pid} is not running — lock is stale")
            return True
        except PermissionError:
            # PID exists but is owned by another user — treat as live to be safe
            pass
        except Exception:
            # Any other error: be conservative, treat as live
            pass

    # Check age (best-effort fallback)
    if len(parts) >= 2:
        try:
            ts = parts[1].rstrip("Z")
            held_at = datetime.fromisoformat(ts)
            age = (datetime.utcnow() - held_at).total_seconds()
            if age > 3600:  # 60 min
                logger.info(f"Lock is older than 60min (age={age:.0f}s) — treating as stale")
                return True
        except Exception:
            pass

    return False


def clear_stale_lock() -> bool:
    """
    Detect and remove a stale lock file. Returns True if a stale lock was
    cleared, False if the lock is live or no lock exists.
    """
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        with open(LOCK_FILE, "r") as f:
            lock_info = f.read().strip()
    except Exception:
        lock_info = ""
    if _is_lock_stale(lock_info):
        try:
            os.remove(LOCK_FILE)
            logger.warning(f"Removed stale lock: {lock_info!r}")
            return True
        except Exception as e:
            logger.error(f"Failed to remove stale lock: {e}")
    return False


def _ensure_trigger_path_is_file_compatible():
    """
    Defensive: if `trigger` exists as a directory (e.g. created by systemd
    MakeDirectory=true), remove it so we can write a file in its place.
    Also clean up any leftover .tmp from a previous failed write.
    """
    if os.path.isdir(TRIGGER_FILE):
        logger.warning(
            f"Found {TRIGGER_FILE} as a directory — removing. "
            "This is usually caused by systemd path unit with MakeDirectory=true."
        )
        try:
            import shutil
            shutil.rmtree(TRIGGER_FILE)
        except Exception as e:
            logger.error(f"Could not remove {TRIGGER_FILE} directory: {e}")
    # Clean up stale .tmp from a previous os.replace that failed
    tmp = TRIGGER_FILE + ".tmp"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass


def write_trigger(action: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    """
    Write a trigger file for the host watcher.

    Returns False if a live lock is held (another op is genuinely in progress).
    Stale locks are auto-cleared.
    """
    _ensure_dirs()
    _ensure_trigger_path_is_file_compatible()
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action: {action}")

    if os.path.exists(LOCK_FILE):
        # Auto-clear stale locks; only refuse if the lock is actually live
        if clear_stale_lock():
            pass  # cleared, fall through to write trigger
        else:
            try:
                with open(LOCK_FILE, "r") as f:
                    lock_info = f.read().strip()
            except Exception:
                lock_info = "unknown"
            logger.warning(f"Upgrade lock held ({lock_info}); cannot write trigger")
            return False

    trigger = {
        "action": action,
        "payload": payload or {},
        "requested_at": _now_iso(),
        "requested_by": "zmm-app",
    }
    tmp = TRIGGER_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(trigger, f, indent=2)
        os.replace(tmp, TRIGGER_FILE)
        logger.info(f"Wrote upgrade trigger: {action}")
        return True
    except Exception as e:
        logger.error(f"Failed to write trigger: {e}")
        return False


def read_build_log(max_lines: int = 500) -> List[str]:
    """Read the last N lines of the build log."""
    if not os.path.exists(BUILD_LOG_FILE):
        return []
    try:
        with open(BUILD_LOG_FILE, "r", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-max_lines:]]
    except Exception as e:
        logger.warning(f"Failed to read build log: {e}")
        return []


def watcher_installed() -> bool:
    """
    Heuristic check: is the host-side watcher set up?

    We look for a marker file the install script drops when it completes.
    """
    marker = os.path.join(UPGRADE_DIR, ".watcher_installed")
    return os.path.exists(marker)


# ---------------------------------------------------------------------------
# GITHUB POLLING
# ---------------------------------------------------------------------------
async def fetch_latest_release(repo: str, channel: str = "stable") -> Optional[Dict[str, Any]]:
    """
    Fetch latest release/tag info from GitHub.

    channel: "stable" (releases/latest) or "prerelease" (top of tags list)
    """
    if channel == "stable":
        url = f"{GITHUB_API_BASE}/repos/{repo}/releases/latest"
    else:
        url = f"{GITHUB_API_BASE}/repos/{repo}/tags"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "zigbee-matter-manager-upgrade",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"GitHub API returned {resp.status}: {body[:200]}")
                    return None
                data = await resp.json()
    except Exception as e:
        logger.warning(f"GitHub API fetch failed: {e}")
        return None

    if channel == "stable":
        # Single release object
        if not isinstance(data, dict):
            return None
        return {
            "version": normalise_version(data.get("tag_name") or ""),
            "tag": data.get("tag_name"),
            "notes": data.get("body") or "",
            "url": data.get("html_url"),
            "published_at": data.get("published_at"),
            "tarball_url": data.get("tarball_url"),
        }
    else:
        # List of tags — pick the first one that looks like semver
        if not isinstance(data, list) or not data:
            return None
        for t in data:
            v = normalise_version(t.get("name") or "")
            if parse_version(v):
                return {
                    "version": v,
                    "tag": t.get("name"),
                    "notes": "",
                    "url": f"https://github.com/{repo}/releases/tag/{t.get('name')}",
                    "published_at": None,
                    "tarball_url": t.get("tarball_url"),
                }
        return None


async def check_for_updates(force: bool = False) -> Dict[str, Any]:
    """
    Check GitHub for a newer version.

    Returns a dict with keys:
      update_available (bool), latest_version, current_version, notes, url
    """
    state = load_state()
    repo = state.get("repo") or DEFAULT_REPO
    channel = state.get("channel") or "stable"

    # Rate-limit: don't check more than once per hour unless forced
    if not force:
        last = state.get("last_check")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:
                    logger.debug("Skipping update check (rate-limited)")
                    return _build_update_result(state)
            except Exception:
                pass

    release = await fetch_latest_release(repo, channel)
    if not release:
        update_state(last_check=_now_iso())
        return _build_update_result(load_state(), error="GitHub API unreachable")

    latest = release["version"]
    current = state.get("current_version") or "0.0.0"
    is_newer = compare_versions(latest, current) > 0

    save_state({
        **state,
        "latest_available": latest if is_newer else None,
        "latest_release_notes": release.get("notes") if is_newer else None,
        "latest_release_url": release.get("url") if is_newer else None,
        "last_check": _now_iso(),
    })

    return _build_update_result(load_state())


def _build_update_result(state: Dict[str, Any], error: Optional[str] = None) -> Dict[str, Any]:
    current = state.get("current_version") or "unknown"
    latest = state.get("latest_available")
    update_available = bool(latest) and compare_versions(latest, current) > 0

    return {
        "update_available": update_available,
        "current_version": current,
        "latest_version": latest,
        "notes": state.get("latest_release_notes") or "",
        "url": state.get("latest_release_url"),
        "last_check": state.get("last_check"),
        "architecture": state.get("architecture"),
        "upgrade_state": state.get("upgrade_state") or "idle",
        "watcher_installed": state.get("watcher_installed", False) or watcher_installed(),
        "error": error,
    }


# ---------------------------------------------------------------------------
# HIGH-LEVEL OPERATIONS (called by routes)
# ---------------------------------------------------------------------------
def request_build(target_version: str) -> Tuple[bool, str]:
    """
    Request a background image build for target_version.

    Returns (success, message).
    """
    state = load_state()

    if not watcher_installed() and not state.get("watcher_installed"):
        return False, (
            "Host-side upgrade watcher is not installed. "
            "Please run the install-watcher action first, or run "
            "`bash ~/zigbee-matter-manager/scripts/install_watcher.sh` on the host."
        )

    tv = normalise_version(target_version)
    if not parse_version(tv):
        return False, f"Invalid version: {target_version}"

    current = state.get("current_version") or "0.0.0"
    if compare_versions(tv, current) <= 0:
        return False, f"Target version {tv} is not newer than current {current}"

    # Clear any lingering "failed" state from a previous attempt — the user
    # is explicitly retrying, so the old failure is no longer relevant.
    reset_status(only_if_failed=True)

    payload = {
        "target_version": tv,
        "architecture": state.get("architecture") or detect_architecture(),
        "repo": state.get("repo") or DEFAULT_REPO,
    }

    ok = write_trigger("build", payload)
    if not ok:
        return False, "Another upgrade operation is in progress"

    update_state(upgrade_state="building", upgrade_error=None)
    return True, f"Build requested for v{tv}"


def request_swap() -> Tuple[bool, str]:
    """Request an atomic container swap to the pre-built new image."""
    status = read_status()
    cur_state = status.get("state")

    # If the host status shows "failed" but we have a freshly-built image
    # ready (which happens when a previous swap failed and rolled back —
    # the image is still there, only the container start failed), allow
    # retrying. The user is explicitly asking to swap again.
    if cur_state == "failed" and status.get("target_version"):
        # Check the failed-but-image-exists case implicitly by clearing
        # status and letting upgrade.sh verify image existence
        logger.info("Retrying swap after previous failure — clearing failed state")
        reset_status(only_if_failed=True)
        # Re-read after reset
        status = read_status()
        cur_state = "ready_to_swap"  # we'll let the host script verify
        target_version = load_state().get("latest_available") or status.get("target_version")
        if not target_version:
            return False, "Cannot retry — no target version known. Click Build again."
    elif cur_state != "ready_to_swap":
        return False, f"Not ready to swap (status: {cur_state})"

    target_version = status.get("target_version") or load_state().get("latest_available")

    ok = write_trigger("swap", {"target_version": target_version})
    if not ok:
        return False, "Another upgrade operation is in progress"

    update_state(upgrade_state="swapping")
    return True, "Swap requested"


def request_rollback() -> Tuple[bool, str]:
    """Request a rollback to the previous image."""
    state = load_state()
    prev = state.get("previous_version")
    prev_tag = state.get("previous_image_tag")
    if not prev or not prev_tag:
        return False, "No previous version available for rollback"

    # Clear stale failed state on retry
    reset_status(only_if_failed=True)

    ok = write_trigger("rollback", {
        "previous_version": prev,
        "previous_image_tag": prev_tag,
    })
    if not ok:
        return False, "Another upgrade operation is in progress"

    update_state(upgrade_state="rolling_back")
    return True, f"Rollback requested to v{prev}"


def request_cancel() -> Tuple[bool, str]:
    """Request cancellation of an in-progress build."""
    ok = write_trigger("cancel", {})
    if not ok:
        return False, "Could not write cancel trigger (lock held; try again)"
    return True, "Cancel requested"


def reset_status(only_if_failed: bool = True) -> Dict[str, Any]:
    """
    Reset the host status file and the in-app upgrade_state to idle.

    Used to dismiss a stale "Failed" banner after the user has acknowledged
    the failure. By default only resets if the current state is "failed",
    so we never accidentally clobber an active operation.

    Returns the new state dict.
    """
    _ensure_dirs()

    if only_if_failed:
        host_status = read_status()
        host_state = host_status.get("state") or "idle"
        app_state = load_state()
        app_upgrade_state = app_state.get("upgrade_state") or "idle"

        # Only clear if BOTH are failed/idle — never wipe an active operation
        if host_state not in ("failed", "idle") or app_upgrade_state not in ("failed", "idle"):
            logger.warning(
                f"Refusing to reset status while operation is active "
                f"(host={host_state}, app={app_upgrade_state})"
            )
            return load_state()

    # Reset the host-side status file to a clean idle
    idle_status = {
        "state": "idle",
        "target_version": None,
        "started_at": None,
        "updated_at": _now_iso(),
        "progress_percent": 0,
        "current_step": "",
        "error": None,
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idle_status, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        logger.error(f"Failed to reset status.json: {e}")

    # Reset the app-side state too
    return update_state(upgrade_state="idle", upgrade_error=None)


def request_gc() -> Tuple[bool, str]:
    """Request garbage collection of old images per retention count."""
    state = load_state()
    retention = int(state.get("retention_count") or 2)
    if retention < 1:
        retention = 1
    ok = write_trigger("gc", {"retention_count": retention})
    if not ok:
        return False, "Lock held; try again"
    return True, f"GC requested (keep {retention})"


def request_install_watcher() -> Tuple[bool, str]:
    """
    Request host-side watcher installation.

    This uses a pre-seeded install script that must already be present
    on the host filesystem — we can't install it ourselves from inside
    the container. See scripts/install_watcher.sh.

    The "trigger" here is a self-install marker the user must invoke
    from the host; we just provide clear instructions.
    """
    # Detect whether the watcher script is visible via a bind-mount
    visible_script = os.path.join(UPGRADE_DIR, "install_watcher.sh")
    if os.path.exists(visible_script):
        # Host-side install can poll for this trigger and auto-run
        ok = write_trigger("install_watcher", {})
        if not ok:
            return False, "Lock held; try again"
        return True, "Watcher install requested"

    return False, (
        "Watcher install script not found in shared volume. "
        "Run this on the host to install the watcher:\n"
        "  curl -fsSL https://raw.githubusercontent.com/"
        "oneofthemany/ZigBee-Matter-Manager/main/scripts/install_watcher.sh | bash"
    )


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
VALID_CHANNELS = {"stable", "prerelease"}


def update_settings(
        auto_update: Optional[bool] = None,
        channel: Optional[str] = None,
        retention_count: Optional[int] = None,
        auto_window_start: Optional[str] = None,
        auto_window_end: Optional[str] = None,
        repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Update upgrade-related settings."""
    state = load_state()

    if auto_update is not None:
        state["auto_update"] = bool(auto_update)
    if channel is not None:
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Invalid channel: {channel}")
        state["channel"] = channel
    if retention_count is not None:
        rc = int(retention_count)
        if rc < 1:
            rc = 1
        if rc > 20:
            rc = 20
        state["retention_count"] = rc
    if auto_window_start is not None:
        state.setdefault("auto_update_window", {})["start"] = auto_window_start
    if auto_window_end is not None:
        state.setdefault("auto_update_window", {})["end"] = auto_window_end
    if repo is not None and repo.strip():
        state["repo"] = repo.strip()

    save_state(state)
    return state


# ---------------------------------------------------------------------------
# BACKGROUND TASKS (called from main.py lifespan)
# ---------------------------------------------------------------------------
async def periodic_check_loop(
        interval_hours: float = 6,
        broadcast_fn=None,
):
    """
    Background loop: check for updates every `interval_hours`.

    broadcast_fn: optional async callable that accepts a dict, used to push
    update notifications to the web UI via the WebSocket manager.
    """
    # Small initial delay so we don't fight for CPU during boot
    await asyncio.sleep(60)

    while True:
        try:
            state = load_state()
            # Only check if auto_update is enabled OR user has not opted out of checking
            # (notification is separate from install)
            result = await check_for_updates(force=False)

            if result.get("update_available") and broadcast_fn:
                try:
                    await broadcast_fn({
                        "type": "upgrade_available",
                        "payload": {
                            "current_version": result.get("current_version"),
                            "latest_version": result.get("latest_version"),
                            "notes": (result.get("notes") or "")[:500],
                            "url": result.get("url"),
                        },
                    })
                except Exception as e:
                    logger.debug(f"broadcast_fn failed: {e}")

            # Auto-update: only if enabled and we're inside the quiet window
            state = load_state()
            if (
                    state.get("auto_update")
                    and result.get("update_available")
                    and _inside_quiet_window(state.get("auto_update_window") or {})
                    and state.get("upgrade_state") == "idle"
            ):
                logger.info("Auto-update triggered for v%s", result.get("latest_version"))
                request_build(result.get("latest_version"))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Periodic update check error: {e}")

        await asyncio.sleep(interval_hours * 3600)


async def status_watcher_loop(broadcast_fn=None, poll_seconds: float = 2.0):
    """
    Background loop: watch status.json and broadcast changes via WebSocket.

    Mirrors status changes into version.json so the UI has a single source of truth.
    """
    last_status = None
    while True:
        try:
            status = read_status()
            app_state = load_state()

            # Sync upgrade_state into version.json
            if status.get("state") and status.get("state") != app_state.get("upgrade_state"):
                update_state(upgrade_state=status.get("state"),
                             upgrade_error=status.get("error"))

            # Broadcast if changed
            if broadcast_fn and status != last_status:
                try:
                    await broadcast_fn({
                        "type": "upgrade_status",
                        "payload": {
                            "status": status,
                        },
                    })
                except Exception as e:
                    logger.debug(f"broadcast_fn failed: {e}")
                last_status = dict(status)

            # If just completed a swap, shift current_version forward
            if (
                    status.get("state") == "idle"
                    and status.get("target_version")
                    and compare_versions(status["target_version"],
                                         app_state.get("current_version") or "0.0.0") > 0
            ):
                # The new container is us — so current_version is already updated
                # from the new image's VERSION file. Just make sure previous_version
                # is recorded (if the host didn't do it).
                pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Status watcher error: {e}")

        await asyncio.sleep(poll_seconds)


def _inside_quiet_window(window: Dict[str, str]) -> bool:
    """Return True if the current local time is inside the HH:MM quiet window."""
    try:
        start = window.get("start") or "03:00"
        end = window.get("end") or "05:00"
        sh, sm = [int(x) for x in start.split(":")]
        eh, em = [int(x) for x in end.split(":")]
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        if start_mins <= end_mins:
            return start_mins <= now_mins <= end_mins
        # Window wraps midnight (e.g. 23:00 - 05:00)
        return now_mins >= start_mins or now_mins <= end_mins
    except Exception:
        return False