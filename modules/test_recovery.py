"""
Test and Recovery System for Code Editor (batched)
===================================================
Safe deployment of code changes with automatic rollback.

Supports BOTH single-file (legacy) and multi-file batch deploys.
All files in a batch share one backup group and are rolled back
together. This is required when edits span dependent files
(e.g. adding a new module + updating an import site).

Flow:
  1. User stages N files → presses "Test"
  2. System backs up every existing file, writes every new one,
     records a single pending batch
  3. Frontend files: WebSocket reload → confirm dialog
     Python files: Service restart → startup health check → confirm
  4. Confirm → pending cleared, backups kept
  5. Timeout OR restart fails → atomic rollback of the whole batch

Pending state is persisted to disk so it survives service restarts
and is consumed by boot_guard.py on failed boots.

Pending file:  <APP_DIR>/data/.test_pending
Backup dir:    <APP_DIR>/.editor_backups/
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any

logger = logging.getLogger("editor.test_recovery")

PROJECT_ROOT = Path("/app")
BACKUP_DIR = PROJECT_ROOT / ".editor_backups"
DATA_DIR = PROJECT_ROOT / "data"
# Shared with boot_guard.py — must stay in sync
PENDING_FILE = DATA_DIR / ".test_pending"

# How long the user has to confirm after restart (seconds)
CONFIRM_TIMEOUT = 120

# File extensions that require a service restart to take effect
RESTART_EXTS = {".py", ".yaml", ".yml"}


class TestRecoveryManager:
    """Manages batched test deployments with automatic rollback."""

    def __init__(self, event_emitter: Optional[Callable] = None):
        self._emit = event_emitter
        self._confirm_task: Optional[asyncio.Task] = None
        self._pending: Optional[dict] = None

    # =========================================================================
    # PENDING STATE (survives restarts)
    # =========================================================================

    def _save_pending(self, state: dict):
        self._pending = state
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            PENDING_FILE.write_text(json.dumps(state, indent=2))
            paths = [f.get("path") for f in state.get("files", [])]
            logger.info(f"Pending test batch saved: {paths}")
        except Exception as e:
            logger.error(f"Failed to save pending state: {e}")

    def _load_pending(self) -> Optional[dict]:
        if not PENDING_FILE.exists():
            return None
        try:
            state = json.loads(PENDING_FILE.read_text())
            # Migrate legacy single-file shape: {path, backup, backup_name, action, ...}
            if "files" not in state and "path" in state:
                state = {
                    "files": [{
                        "path": state.get("path"),
                        "backup": state.get("backup"),
                        "backup_name": state.get("backup_name"),
                        "was_new": False,
                    }],
                    "action": state.get("action", "restart"),
                    "deployed_at": state.get("deployed_at", time.time()),
                    "timeout": state.get("timeout", CONFIRM_TIMEOUT),
                    "confirmed": state.get("confirmed", False),
                }
            self._pending = state
            return state
        except Exception as e:
            logger.error(f"Failed to load pending state: {e}")
            return None

    def _clear_pending(self):
        self._pending = None
        try:
            if PENDING_FILE.exists():
                PENDING_FILE.unlink()
            logger.info("Pending test state cleared")
        except Exception as e:
            logger.error(f"Failed to clear pending state: {e}")

    def get_pending(self) -> Optional[dict]:
        if self._pending:
            return self._pending
        return self._load_pending()

    # =========================================================================
    # DEPLOY — Batch
    # =========================================================================

    async def deploy_test_batch(self, files: List[Dict[str, Any]]) -> dict:
        """
        Deploy a batch of file changes for testing.

        files: [{"path": "...", "content": "..."}, ...]

        Backs up every existing file, writes every file, records a single
        pending batch. If any write fails, all writes already performed
        are rolled back.
        """
        if not files:
            return {"success": False, "error": "No files provided"}

        # Block if a batch is already pending
        existing = self.get_pending()
        if existing:
            pending_paths = [f.get("path") for f in existing.get("files", [])]
            return {
                "success": False,
                "error": (
                    f"A test batch is already pending ({len(pending_paths)} file(s): "
                    f"{', '.join(pending_paths[:3])}"
                    f"{'...' if len(pending_paths) > 3 else ''}). "
                    "Confirm or rollback first."
                ),
            }

        # Validate + resolve all paths first (atomic-ish)
        resolved: List[Dict[str, Any]] = []
        for entry in files:
            path = entry.get("path")
            content = entry.get("content")
            if not path or content is None:
                return {"success": False, "error": f"Invalid file entry: {entry}"}
            full = (PROJECT_ROOT / path).resolve()
            if not str(full).startswith(str(PROJECT_ROOT.resolve())):
                return {"success": False, "error": f"Path outside project: {path}"}
            resolved.append({"path": path, "content": content, "full": full})

        # Decide action: restart if ANY file requires it, else reload
        action = "reload"
        for r in resolved:
            if r["full"].suffix.lower() in RESTART_EXTS:
                action = "restart"
                break

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        batch_tag = f"batch_{ts}"

        # Phase 1: back up every file that currently exists
        backups_created: List[Dict[str, Any]] = []
        try:
            for r in resolved:
                full = r["full"]
                safe_name = r["path"].replace("/", "_").replace("\\", "_")
                backup_name = f"{safe_name}.{ts}.{batch_tag}.test_recovery.bak"
                backup_path = BACKUP_DIR / backup_name

                was_new = not full.exists()
                if not was_new:
                    shutil.copy2(full, backup_path)
                else:
                    # Sentinel marker for new-file rollback (empty)
                    backup_path.write_text("")

                backups_created.append({
                    "path": r["path"],
                    "full": full,
                    "backup": str(backup_path),
                    "backup_name": backup_name,
                    "was_new": was_new,
                })
            logger.info(f"Test batch backups created ({batch_tag}): {len(backups_created)} file(s)")
        except Exception as e:
            # Couldn't even make backups — nothing to undo
            return {"success": False, "error": f"Backup failed: {e}"}

        # Phase 2: write every file. If any write fails, restore from backups
        # for files we already wrote and abort.
        written: List[Dict[str, Any]] = []
        for r, b in zip(resolved, backups_created):
            try:
                r["full"].parent.mkdir(parents=True, exist_ok=True)
                r["full"].write_text(r["content"], encoding="utf-8")
                written.append(b)
            except Exception as e:
                logger.error(f"Batch write failed for {r['path']}: {e} — undoing partial batch")
                # Undo: restore anything already written
                for w in written:
                    try:
                        if w["was_new"]:
                            Path(w["full"]).unlink(missing_ok=True)
                        else:
                            shutil.copy2(w["backup"], w["full"])
                    except Exception as ue:
                        logger.error(f"Partial-undo failed for {w['path']}: {ue}")
                return {"success": False, "error": f"Write failed on {r['path']}: {e}"}

        # Phase 3: persist the pending batch (stripped of `full` Path objects)
        file_records = [
            {
                "path": b["path"],
                "backup": b["backup"],
                "backup_name": b["backup_name"],
                "was_new": b["was_new"],
            }
            for b in backups_created
        ]

        self._save_pending({
            "files": file_records,
            "batch_tag": batch_tag,
            "action": action,
            "deployed_at": time.time(),
            "timeout": CONFIRM_TIMEOUT,
            "confirmed": False,
        })

        # For frontend batches, the rollback timer starts immediately.
        # For restart batches, the new process picks up the timer in
        # check_pending_on_startup().
        if action == "reload":
            self._start_confirm_timer()

        return {
            "success": True,
            "action": action,
            "count": len(file_records),
            "files": [f["path"] for f in file_records],
            "batch_tag": batch_tag,
            "timeout": CONFIRM_TIMEOUT,
            "message": (
                f"{len(file_records)} file(s) deployed. "
                f"{'Service will restart.' if action == 'restart' else 'Page will reload.'} "
                f"You have {CONFIRM_TIMEOUT}s to confirm."
            ),
        }

    # Backwards-compat wrapper
    async def deploy_test(self, path: str, content: str) -> dict:
        """Legacy single-file entry point. Prefer deploy_test_batch()."""
        result = await self.deploy_test_batch([{"path": path, "content": content}])
        if result.get("success"):
            # Flatten for old callers
            result["path"] = path
            result["backup"] = result.get("files", [path])[0] if result.get("files") else None
        return result

    # =========================================================================
    # RESTART
    # =========================================================================

    async def trigger_restart(self) -> dict:
        pending = self.get_pending()
        if not pending or pending.get("action") != "restart":
            return {"success": False, "error": "No restart-type test pending"}

        logger.warning("Test deployment: restarting service...")

        async def _do_restart():
            await asyncio.sleep(1)
            python = sys.executable
            os.execl(python, python, *sys.argv)

        asyncio.create_task(_do_restart())
        return {"success": True, "message": "Restarting..."}

    # =========================================================================
    # STARTUP CHECK
    # =========================================================================

    def check_pending_on_startup(self):
        pending = self._load_pending()
        if not pending:
            return None

        if pending.get("confirmed"):
            self._clear_pending()
            return None

        elapsed = time.time() - pending.get("deployed_at", 0)
        remaining = max(0, pending.get("timeout", CONFIRM_TIMEOUT) - elapsed)
        paths = [f.get("path") for f in pending.get("files", [])]

        if remaining <= 0:
            logger.warning(f"Test timeout expired during restart — rolling back batch: {paths}")
            self._do_rollback(pending)
            return {"rolled_back": True, "files": paths}

        logger.info(f"Pending batch: {paths} — {int(remaining)}s remaining to confirm")
        self._start_confirm_timer(remaining)

        return {
            "pending": True,
            "files": paths,
            "remaining": int(remaining),
            "action": pending.get("action"),
        }

    # =========================================================================
    # CONFIRM / ROLLBACK
    # =========================================================================

    async def confirm(self) -> dict:
        pending = self.get_pending()
        if not pending:
            return {"success": False, "error": "No pending test"}

        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()

        paths = [f.get("path") for f in pending.get("files", [])]
        self._clear_pending()
        logger.info(f"Test batch confirmed: {paths}")

        if self._emit:
            try:
                await self._emit("test_recovery", {"status": "confirmed", "files": paths})
            except Exception:
                pass

        return {
            "success": True,
            "files": paths,
            "message": f"{len(paths)} file(s) confirmed and kept.",
        }

    async def rollback(self) -> dict:
        pending = self.get_pending()
        if not pending:
            return {"success": False, "error": "No pending test to rollback"}

        result = self._do_rollback(pending)

        if self._emit:
            try:
                await self._emit("test_recovery", {
                    "status": "rolled_back",
                    "files": [f.get("path") for f in pending.get("files", [])],
                })
            except Exception:
                pass

        if pending.get("action") == "restart":
            result["needs_restart"] = True
        return result

    def _do_rollback(self, pending: dict) -> dict:
        files = pending.get("files", [])
        if not files:
            self._clear_pending()
            return {"success": False, "error": "Pending state has no files"}

        restored: List[str] = []
        errors: List[str] = []

        for entry in files:
            path = entry.get("path")
            backup = entry.get("backup")
            was_new = entry.get("was_new", False)

            target = PROJECT_ROOT / path
            bpath = Path(backup) if backup else None

            try:
                if was_new:
                    # Newly-created file — delete it
                    if target.exists():
                        target.unlink()
                    restored.append(path)
                    logger.info(f"Rollback: deleted new file {path}")
                elif bpath and bpath.exists():
                    shutil.copy2(bpath, target)
                    restored.append(path)
                    logger.info(f"Rollback: restored {path} from {bpath}")
                else:
                    errors.append(f"{path}: backup missing ({backup})")
                    logger.error(f"Rollback: backup missing for {path}: {backup}")
            except Exception as e:
                errors.append(f"{path}: {e}")
                logger.error(f"Rollback failed for {path}: {e}")

        self._clear_pending()

        return {
            "success": len(errors) == 0,
            "restored": restored,
            "errors": errors,
            "message": f"Rolled back {len(restored)}/{len(files)} file(s)"
                       + (f" ({len(errors)} error(s))" if errors else ""),
        }

    # =========================================================================
    # CONFIRM TIMER
    # =========================================================================

    def _start_confirm_timer(self, timeout: float = None):
        if timeout is None:
            timeout = CONFIRM_TIMEOUT
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        self._confirm_task = asyncio.create_task(self._confirm_timeout(timeout))

    async def _confirm_timeout(self, timeout: float):
        try:
            logger.info(f"Rollback timer started: {int(timeout)}s")

            remaining = timeout
            while remaining > 0:
                if self._emit and self._pending:
                    try:
                        await self._emit("test_recovery", {
                            "status": "pending",
                            "remaining": int(remaining),
                            "files": [f.get("path") for f in self._pending.get("files", [])],
                        })
                    except Exception:
                        pass
                wait = min(10, remaining)
                await asyncio.sleep(wait)
                remaining -= wait

            pending = self.get_pending()
            if pending and not pending.get("confirmed"):
                paths = [f.get("path") for f in pending.get("files", [])]
                logger.warning(f"Test timeout expired — auto-rolling back batch: {paths}")
                self._do_rollback(pending)

                if self._emit:
                    try:
                        await self._emit("test_recovery", {
                            "status": "auto_rollback",
                            "files": paths,
                        })
                    except Exception:
                        pass

                if pending.get("action") == "restart":
                    logger.warning("Auto-rollback: restarting service to apply...")
                    await asyncio.sleep(2)
                    python = sys.executable
                    os.execl(python, python, *sys.argv)

        except asyncio.CancelledError:
            logger.info("Rollback timer cancelled (user confirmed)")


# Singleton
_manager: Optional[TestRecoveryManager] = None


def get_test_recovery_manager(event_emitter=None) -> TestRecoveryManager:
    global _manager
    if _manager is None:
        _manager = TestRecoveryManager(event_emitter)
    return _manager