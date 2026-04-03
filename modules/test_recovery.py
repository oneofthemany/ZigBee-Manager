"""
Test and Recovery System for Code Editor
=========================================
Provides safe deployment of code changes with automatic rollback.

Flow:
  1. User edits file in editor → presses "Test"
  2. System saves backup → writes file → records pending test
  3. Frontend files: WebSocket triggers reload → confirm dialog
     Python files: Service restart → startup health check → confirm dialog
  4. If user confirms → pending state cleared, backup kept
  5. If timeout expires OR restart fails → automatic rollback from backup

Pending test state is persisted to disk so it survives service restarts.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("editor.test_recovery")

PROJECT_ROOT = Path("/opt/zigbee_manager")
BACKUP_DIR = PROJECT_ROOT / ".editor_backups"
PENDING_FILE = PROJECT_ROOT / ".editor_pending_test.json"

# How long the user has to confirm after restart (seconds)
CONFIRM_TIMEOUT = 120


class TestRecoveryManager:
    """Manages test deployments with automatic rollback."""

    def __init__(self, event_emitter: Optional[Callable] = None):
        self._emit = event_emitter
        self._confirm_task: Optional[asyncio.Task] = None
        self._pending: Optional[dict] = None

    # =========================================================================
    # PENDING STATE (survives restarts)
    # =========================================================================

    def _save_pending(self, state: dict):
        """Write pending test state to disk."""
        self._pending = state
        try:
            PENDING_FILE.write_text(json.dumps(state, indent=2))
            logger.info(f"Pending test state saved: {state.get('path')}")
        except Exception as e:
            logger.error(f"Failed to save pending state: {e}")

    def _load_pending(self) -> Optional[dict]:
        """Load pending test state from disk (if any)."""
        if not PENDING_FILE.exists():
            return None
        try:
            state = json.loads(PENDING_FILE.read_text())
            self._pending = state
            return state
        except Exception as e:
            logger.error(f"Failed to load pending state: {e}")
            return None

    def _clear_pending(self):
        """Clear pending test state."""
        self._pending = None
        try:
            if PENDING_FILE.exists():
                PENDING_FILE.unlink()
            logger.info("Pending test state cleared")
        except Exception as e:
            logger.error(f"Failed to clear pending state: {e}")

    def get_pending(self) -> Optional[dict]:
        """Get current pending test (if any)."""
        if self._pending:
            return self._pending
        return self._load_pending()

    # =========================================================================
    # DEPLOY (called when user clicks "Test")
    # =========================================================================

    async def deploy_test(self, path: str, content: str) -> dict:
        """
        Deploy a file change for testing.

        1. Creates backup of current file
        2. Writes new content
        3. Records pending test state
        4. Returns action type (reload / restart)
        """
        # Check for existing pending test
        existing = self.get_pending()
        if existing:
            return {
                "success": False,
                "error": f"A test is already pending for {existing.get('path')}. Confirm or rollback first."
            }

        full_path = (PROJECT_ROOT / path).resolve()
        if not str(full_path).startswith(str(PROJECT_ROOT.resolve())):
            return {"success": False, "error": "Invalid path"}

        # Determine action type
        ext = full_path.suffix.lower()
        if ext in ('.py', '.yaml', '.yml'):
            action = "restart"
        elif ext in ('.js', '.css', '.html'):
            action = "reload"
        elif ext in ('.json',):
            action = "reload"
        else:
            action = "reload"

        # Create backup
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = path.replace("/", "_").replace("\\", "_")
        backup_name = f"{safe_name}.{ts}.test_recovery.bak"
        backup_path = BACKUP_DIR / backup_name

        if full_path.exists():
            shutil.copy2(full_path, backup_path)
            logger.info(f"Test backup created: {backup_path}")
        else:
            # New file — backup is empty marker
            backup_path.write_text("")

        # Write new content
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        except Exception as e:
            # Restore backup immediately if write fails
            if backup_path.exists() and backup_path.stat().st_size > 0:
                shutil.copy2(backup_path, full_path)
            return {"success": False, "error": f"Write failed: {e}"}

        # Save pending state
        self._save_pending({
            "path": path,
            "backup": str(backup_path),
            "backup_name": backup_name,
            "action": action,
            "deployed_at": time.time(),
            "timeout": CONFIRM_TIMEOUT,
            "confirmed": False,
        })

        # For frontend files, start the confirm timeout immediately
        if action == "reload":
            self._start_confirm_timer()

        return {
            "success": True,
            "action": action,
            "path": path,
            "backup": backup_name,
            "timeout": CONFIRM_TIMEOUT,
            "message": f"File deployed. {'Service will restart.' if action == 'restart' else 'Page will reload.'} "
                       f"You have {CONFIRM_TIMEOUT}s to confirm."
        }

    # =========================================================================
    # RESTART (for Python files)
    # =========================================================================

    async def trigger_restart(self) -> dict:
        """Restart the service. Called after deploy_test for Python files."""
        pending = self.get_pending()
        if not pending or pending.get("action") != "restart":
            return {"success": False, "error": "No restart-type test pending"}

        logger.warning("Test deployment: restarting service...")

        async def _do_restart():
            await asyncio.sleep(1)  # Let response go out
            python = sys.executable
            os.execl(python, python, *sys.argv)

        asyncio.create_task(_do_restart())
        return {"success": True, "message": "Restarting..."}

    # =========================================================================
    # STARTUP CHECK (called on app boot)
    # =========================================================================

    def check_pending_on_startup(self):
        """
        Called during app startup. If there's a pending Python test,
        start the confirm timer. If the timer expires, rollback.
        """
        pending = self._load_pending()
        if not pending:
            return None

        if pending.get("confirmed"):
            self._clear_pending()
            return None

        elapsed = time.time() - pending.get("deployed_at", 0)
        remaining = max(0, CONFIRM_TIMEOUT - elapsed)

        if remaining <= 0:
            # Timeout already expired during restart — rollback immediately
            logger.warning(f"Test timeout expired during restart — rolling back {pending.get('path')}")
            self._do_rollback(pending)
            return {"rolled_back": True, "path": pending.get("path")}

        logger.info(f"Pending test found: {pending.get('path')} — {int(remaining)}s remaining to confirm")
        self._start_confirm_timer(remaining)

        return {
            "pending": True,
            "path": pending.get("path"),
            "remaining": int(remaining),
            "action": pending.get("action"),
        }

    # =========================================================================
    # CONFIRM
    # =========================================================================

    async def confirm(self) -> dict:
        """User confirms the test deployment is working."""
        pending = self.get_pending()
        if not pending:
            return {"success": False, "error": "No pending test"}

        # Cancel the rollback timer
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()

        path = pending.get("path")
        self._clear_pending()

        logger.info(f"Test confirmed: {path}")

        if self._emit:
            try:
                await self._emit("test_recovery", {
                    "status": "confirmed",
                    "path": path,
                })
            except:
                pass

        return {"success": True, "path": path, "message": "Changes confirmed and kept."}

    # =========================================================================
    # ROLLBACK
    # =========================================================================

    async def rollback(self) -> dict:
        """Manually rollback to the backup."""
        pending = self.get_pending()
        if not pending:
            return {"success": False, "error": "No pending test to rollback"}

        result = self._do_rollback(pending)

        if self._emit:
            try:
                await self._emit("test_recovery", {
                    "status": "rolled_back",
                    "path": pending.get("path"),
                })
            except:
                pass

        # If it was a Python file, need to restart again
        if pending.get("action") == "restart":
            result["needs_restart"] = True

        return result

    def _do_rollback(self, pending: dict) -> dict:
        """Execute the actual rollback."""
        path = pending.get("path")
        backup = pending.get("backup")

        if not backup or not Path(backup).exists():
            self._clear_pending()
            return {"success": False, "error": "Backup file not found"}

        target = PROJECT_ROOT / path
        backup_path = Path(backup)

        try:
            if backup_path.stat().st_size == 0:
                # Was a new file — delete it
                if target.exists():
                    target.unlink()
                logger.info(f"Rolled back: deleted new file {path}")
            else:
                shutil.copy2(backup_path, target)
                logger.info(f"Rolled back: {path} from {backup}")

            self._clear_pending()
            return {"success": True, "path": path, "message": f"Rolled back {path}"}

        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # CONFIRM TIMER
    # =========================================================================

    def _start_confirm_timer(self, timeout: float = None):
        """Start a timer that auto-rollbacks if not confirmed."""
        if timeout is None:
            timeout = CONFIRM_TIMEOUT

        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()

        self._confirm_task = asyncio.create_task(self._confirm_timeout(timeout))

    async def _confirm_timeout(self, timeout: float):
        """Wait for confirmation. If timeout expires, rollback."""
        try:
            logger.info(f"Rollback timer started: {int(timeout)}s")

            # Emit countdown updates
            remaining = timeout
            while remaining > 0:
                if self._emit:
                    try:
                        await self._emit("test_recovery", {
                            "status": "pending",
                            "remaining": int(remaining),
                            "path": self._pending.get("path") if self._pending else "",
                        })
                    except:
                        pass

                wait = min(10, remaining)
                await asyncio.sleep(wait)
                remaining -= wait

            # Timeout expired — rollback
            pending = self.get_pending()
            if pending and not pending.get("confirmed"):
                logger.warning(f"Test timeout expired — auto-rolling back {pending.get('path')}")
                self._do_rollback(pending)

                if self._emit:
                    try:
                        await self._emit("test_recovery", {
                            "status": "auto_rollback",
                            "path": pending.get("path"),
                        })
                    except:
                        pass

                # If Python file, restart to apply rollback
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
