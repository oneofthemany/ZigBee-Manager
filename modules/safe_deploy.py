"""
Safe Deploy - Backup, Validate, Restart, Health-Check, Rollback
================================================================
Replaces the naive os.execl restart with a systemd-aware pipeline:

  1. Snapshot current working code to ./backups/<timestamp>/
  2. Validate all .py files (syntax check via py_compile)
  3. Restart via systemctl (so systemd tracks the process)
  4. Health-check loop (poll /api/devices for 200 OK)
  5. Auto-rollback if health check fails within timeout

API:
  POST /api/system/deploy         — Full deploy pipeline (backup + restart + health)
  POST /api/system/rollback       — Manual rollback to last backup
  GET  /api/system/deploy/status  — Current deploy state
  GET  /api/system/backups        — List available backups

Note: Restart via systemctl requires the service user to have passwordless
sudo for the specific systemctl commands. Add to /etc/sudoers.d/:
  sean ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart zigbee_manager
  sean ALL=(ALL) NOPASSWD: /usr/bin/systemctl status zigbee_manager
"""

import asyncio
import glob
import json
import logging
import os
import py_compile
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("modules.safe_deploy")

router = APIRouter(prefix="/api/system", tags=["system"])

# ============================================================================
# CONFIGURATION
# ============================================================================

BACKUP_DIR = os.environ.get('ZMM_BACKUP_DIR', '/app/data/backups')
MAX_BACKUPS = 10
HEALTH_CHECK_URL = "http://localhost:8000/api/devices"
HEALTH_CHECK_TIMEOUT = 60       # seconds to wait for healthy restart
HEALTH_CHECK_INTERVAL = 3       # seconds between health polls
SERVICE_NAME = "zigbee_manager"

# Files/dirs to back up (relative to APP_DIR)
BACKUP_TARGETS = [
    "*.py",
    "modules/",
    "handlers/",
    "routes/",
    "static/js/",
    "static/css/",
    "static/index.html",
    "static/sw.js",
]

# Files/dirs to NEVER back up or restore
BACKUP_EXCLUDE = [
    "backups/",
    "data/",
    "config/",
    "logs/",
    "__pycache__/",
    "*.pyc",
    ".git/",
    "venv/",
]

# Deploy state (in-memory, reset on restart)
_deploy_state = {
    "status": "idle",           # idle | backing_up | validating | restarting | health_checking | rolling_back | complete | failed
    "message": "",
    "backup_id": None,
    "started_at": None,
    "completed_at": None,
    "validation_errors": [],
    "health_checks": 0,
}


# ============================================================================
# REGISTRATION
# ============================================================================

def register_deploy_routes(app, service_name: str = SERVICE_NAME):
    """Register safe deploy routes on the FastAPI app."""
    global SERVICE_NAME
    SERVICE_NAME = service_name
    os.makedirs(BACKUP_DIR, exist_ok=True)
    app.include_router(router)
    logger.info("Safe deploy routes registered")


# ============================================================================
# BACKUP
# ============================================================================

def _create_backup() -> str:
    """
    Snapshot current code to backups/<timestamp>/.
    Returns the backup ID (timestamp string).
    """
    backup_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, backup_id)
    os.makedirs(backup_path, exist_ok=True)

    count = 0
    for target in BACKUP_TARGETS:
        src = os.path.join(APP_DIR, target)

        if target.endswith("/"):
            # Directory — copy tree
            if os.path.isdir(src):
                dst = os.path.join(backup_path, target.rstrip("/"))
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git"))
                count += 1
        elif "*" in target:
            # Glob pattern
            for f in glob.glob(src):
                relpath = os.path.relpath(f, APP_DIR)
                dst = os.path.join(backup_path, relpath)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(f, dst)
                count += 1
        elif os.path.isfile(src):
            relpath = os.path.relpath(src, APP_DIR)
            dst = os.path.join(backup_path, relpath)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            count += 1

    # Write manifest
    manifest = {
        "backup_id": backup_id,
        "created_at": datetime.now().isoformat(),
        "file_count": count,
        "app_dir": APP_DIR,
    }
    with open(os.path.join(backup_path, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info(f"Backup created: {backup_id} ({count} items)")

    # Prune old backups
    _prune_backups()

    return backup_id


def _prune_backups():
    """Keep only the most recent MAX_BACKUPS."""
    backups = sorted(Path(BACKUP_DIR).iterdir(), key=lambda p: p.name, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        if old.is_dir() and (old / "manifest.json").exists():
            shutil.rmtree(old)
            logger.info(f"Pruned old backup: {old.name}")


def _restore_backup(backup_id: str) -> Dict[str, Any]:
    """
    Restore code from a backup. Returns result dict.
    """
    backup_path = os.path.join(BACKUP_DIR, backup_id)
    manifest_path = os.path.join(backup_path, "manifest.json")

    if not os.path.isfile(manifest_path):
        return {"success": False, "error": f"Backup not found: {backup_id}"}

    count = 0
    errors = []

    for item in Path(backup_path).rglob("*"):
        if item.name == "manifest.json":
            continue
        if not item.is_file():
            continue

        relpath = item.relative_to(backup_path)
        dst = os.path.join(APP_DIR, str(relpath))

        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(str(item), dst)
            count += 1
        except Exception as e:
            errors.append(f"{relpath}: {e}")

    logger.info(f"Restored backup {backup_id}: {count} files, {len(errors)} errors")
    return {"success": len(errors) == 0, "restored": count, "errors": errors}


def _list_backups() -> List[Dict]:
    """List all available backups."""
    backups = []
    if not os.path.isdir(BACKUP_DIR):
        return backups

    for d in sorted(Path(BACKUP_DIR).iterdir(), reverse=True):
        manifest_path = d / "manifest.json"
        if d.is_dir() and manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                # Calculate size
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                manifest["size_mb"] = round(size / (1024 * 1024), 2)
                backups.append(manifest)
            except Exception:
                pass

    return backups


# ============================================================================
# VALIDATION
# ============================================================================

def _validate_python() -> List[str]:
    """
    Syntax-check all .py files in the app directory.
    Returns list of error strings (empty = all valid).
    """
    errors = []

    for py_file in Path(APP_DIR).rglob("*.py"):
        # Skip backups, venv, pycache
        rel = str(py_file.relative_to(APP_DIR))
        if any(rel.startswith(exc.rstrip("/")) for exc in BACKUP_EXCLUDE):
            continue

        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{rel}: {e}")

    return errors


def _validate_js() -> List[str]:
    """
    Basic JS validation — check for obvious syntax issues.
    Full validation would require node/eslint; this just catches
    common problems like unclosed strings or brackets.
    """
    errors = []
    js_dir = os.path.join(APP_DIR, "static", "js")

    if not os.path.isdir(js_dir):
        return errors

    for js_file in Path(js_dir).rglob("*.js"):
        try:
            content = js_file.read_text(encoding="utf-8")
            # Check balanced braces/brackets/parens
            counts = {"(": 0, ")": 0, "{": 0, "}": 0, "[": 0, "]": 0}
            in_string = None
            prev_char = ""
            for ch in content:
                if in_string:
                    if ch == in_string and prev_char != "\\":
                        in_string = None
                elif ch in ("'", '"', '`'):
                    in_string = ch
                elif ch in counts:
                    counts[ch] += 1
                prev_char = ch

            if counts["("] != counts[")"]:
                errors.append(f"{js_file.name}: unbalanced parentheses ({counts['(']} open, {counts[')']} close)")
            if counts["{"] != counts["}"]:
                errors.append(f"{js_file.name}: unbalanced braces ({counts['{']} open, {counts['}']} close)")
            if counts["["] != counts["]"]:
                errors.append(f"{js_file.name}: unbalanced brackets ({counts['[']} open, {counts[']']} close)")
        except Exception as e:
            errors.append(f"{js_file.name}: read error: {e}")

    return errors


# ============================================================================
# RESTART & HEALTH CHECK
# ============================================================================

def _restart_service() -> Dict[str, Any]:
    """Restart via systemctl. Requires sudoers entry."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr.strip()}
        return {"success": True}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "systemctl restart timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _health_check(timeout: int = HEALTH_CHECK_TIMEOUT) -> bool:
    """
    Poll the API until it responds 200 or timeout.
    Returns True if healthy, False if timed out.
    """
    import aiohttp

    deadline = time.time() + timeout
    checks = 0

    # Wait a few seconds for the process to start
    await asyncio.sleep(5)

    while time.time() < deadline:
        checks += 1
        _deploy_state["health_checks"] = checks

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HEALTH_CHECK_URL,
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        logger.info(f"Health check passed after {checks} attempts")
                        return True
        except Exception:
            pass

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    logger.error(f"Health check failed after {checks} attempts ({timeout}s)")
    return False


# ============================================================================
# DEPLOY PIPELINE
# ============================================================================

async def _run_deploy(skip_validation: bool = False):
    """
    Full deploy pipeline (runs as background task).

    1. Backup current code
    2. Validate Python + JS syntax
    3. Restart service via systemctl
    4. Health-check the new process
    5. Rollback if health check fails
    """
    global _deploy_state

    try:
        # Step 1: Backup
        _deploy_state.update(status="backing_up", message="Creating backup...")
        backup_id = _create_backup()
        _deploy_state["backup_id"] = backup_id

        # Step 2: Validate
        if not skip_validation:
            _deploy_state.update(status="validating", message="Validating Python syntax...")
            py_errors = _validate_python()
            js_errors = _validate_js()
            all_errors = py_errors + js_errors
            _deploy_state["validation_errors"] = all_errors

            if py_errors:
                # Python syntax errors are fatal — don't restart
                _deploy_state.update(
                    status="failed",
                    message=f"Validation failed: {len(py_errors)} Python error(s)",
                    completed_at=time.time()
                )
                logger.error(f"Deploy aborted — Python syntax errors: {py_errors}")
                return

            if js_errors:
                # JS errors are warnings — log but proceed
                logger.warning(f"JS validation warnings: {js_errors}")
                _deploy_state["message"] = f"JS warnings: {len(js_errors)} (proceeding)"

        # Step 3: Restart
        _deploy_state.update(status="restarting", message="Restarting service...")
        logger.info("Deploy: restarting service...")

        # NOTE: This endpoint is being served by the CURRENT process.
        # After systemctl restart, THIS process will be killed by systemd
        # and a new one started. The health check runs in the NEW process
        # only if we use a detached approach.
        #
        # Instead, we use a two-phase approach:
        # - Write a deploy marker file with the backup_id
        # - Restart via systemctl
        # - The NEW process checks for the marker on startup and runs health validation
        # - If health fails, the new process restores the backup and restarts again

        _write_deploy_marker(backup_id)
        restart_result = _restart_service()

        if not restart_result["success"]:
            # Restart command itself failed — don't need rollback, old process still running
            _remove_deploy_marker()
            _deploy_state.update(
                status="failed",
                message=f"Restart failed: {restart_result['error']}",
                completed_at=time.time()
            )
            return

        # After this point, systemd kills us. The new process handles the rest.
        _deploy_state.update(status="restarting", message="Service restarting...")

    except Exception as e:
        logger.error(f"Deploy pipeline error: {e}", exc_info=True)
        _deploy_state.update(
            status="failed",
            message=str(e),
            completed_at=time.time()
        )


# ============================================================================
# DEPLOY MARKER (survives restart)
# ============================================================================

DEPLOY_MARKER = os.path.join(APP_DIR, "data", ".deploy_pending")


def _write_deploy_marker(backup_id: str):
    """Write marker so the new process knows a deploy is in progress."""
    os.makedirs(os.path.dirname(DEPLOY_MARKER), exist_ok=True)
    with open(DEPLOY_MARKER, "w") as f:
        json.dump({
            "backup_id": backup_id,
            "timestamp": time.time(),
        }, f)


def _read_deploy_marker() -> Optional[Dict]:
    """Read deploy marker if it exists."""
    if not os.path.isfile(DEPLOY_MARKER):
        return None
    try:
        with open(DEPLOY_MARKER) as f:
            return json.load(f)
    except Exception:
        return None


def _remove_deploy_marker():
    """Remove the deploy marker."""
    try:
        os.remove(DEPLOY_MARKER)
    except FileNotFoundError:
        pass


async def check_deploy_on_startup():
    """
    Called during app startup. If a deploy marker exists, this is a
    freshly-restarted process after a deploy. Run self-health-check
    and rollback if we're broken.

    Wire this into main.py lifespan AFTER all services are initialised.
    """
    marker = _read_deploy_marker()
    if not marker:
        return

    backup_id = marker.get("backup_id")
    logger.info(f"Deploy marker found — post-deploy health check (backup: {backup_id})")

    # Give everything a moment to fully initialise
    await asyncio.sleep(3)

    # Self-check: can we serve /api/devices?
    healthy = False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_CHECK_URL,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                healthy = (resp.status == 200)
    except Exception as e:
        logger.error(f"Post-deploy self-check failed: {e}")

    if healthy:
        logger.info("Post-deploy health check PASSED — deploy successful")
        _remove_deploy_marker()
    else:
        logger.critical(f"Post-deploy health check FAILED — rolling back to {backup_id}")
        _remove_deploy_marker()  # Remove first to prevent rollback loop

        if backup_id:
            result = _restore_backup(backup_id)
            logger.info(f"Rollback result: {result}")

            # Restart again with the restored code
            logger.info("Restarting after rollback...")
            _restart_service()


# ============================================================================
# API ROUTES
# ============================================================================

@router.post("/deploy")
async def deploy(skip_validation: bool = False):
    """
    Trigger safe deploy: backup → validate → restart → health-check → rollback.
    """
    global _deploy_state

    if _deploy_state["status"] in ("backing_up", "validating", "restarting", "health_checking"):
        raise HTTPException(409, "Deploy already in progress")

    _deploy_state = {
        "status": "starting",
        "message": "Deploy pipeline starting...",
        "backup_id": None,
        "started_at": time.time(),
        "completed_at": None,
        "validation_errors": [],
        "health_checks": 0,
    }

    # Run the pipeline (it will restart us, so it won't fully complete in this process)
    asyncio.create_task(_run_deploy(skip_validation=skip_validation))

    return {"success": True, "message": "Deploy pipeline started", "status": _deploy_state}


@router.post("/rollback")
async def rollback(backup_id: Optional[str] = None):
    """Manual rollback to a specific backup or the most recent."""
    backups = _list_backups()
    if not backups:
        raise HTTPException(404, "No backups available")

    if not backup_id:
        backup_id = backups[0]["backup_id"]

    result = _restore_backup(backup_id)
    if not result["success"]:
        raise HTTPException(500, result.get("error", "Rollback failed"))

    return {"success": True, "backup_id": backup_id, **result}


@router.get("/deploy/status")
async def deploy_status():
    """Get current deploy pipeline state."""
    return _deploy_state


@router.get("/backups")
async def list_backups():
    """List available code backups."""
    return {"backups": _list_backups()}