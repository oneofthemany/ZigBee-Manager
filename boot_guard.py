#!/usr/bin/env python3
"""
Boot Guard — Pre-startup rollback for failed test deployments.
================================================================
Runs BEFORE main.py via the launcher (or systemd ExecStartPre).
Uses ONLY stdlib so it cannot be broken by bad code deploys.

Supports BATCH pending state written by modules/test_recovery.py.

Flow:
  1. Check if a test deployment is pending (data/.test_pending)
  2. If pending, check the boot-fail counter (data/.boot_failures)
  3. If counter >= MAX_FAILURES, the previous start attempt failed:
     → Restore every backup listed in the batch (or delete new files)
     → Remove the pending marker and counter
     → Exit 0 (launcher starts main.py with restored code)
  4. If counter doesn't exist, this is the first start after deploy:
     → Create counter with count=1
     → Exit 0 (let main.py try to start)
  5. If no test pending, ensure counter is clean:
     → Remove any stale counter file
     → Exit 0

Files:
  data/.test_pending    — written by test_recovery.py (batch schema)
  data/.boot_failures   — written/read by this script only
  .editor_backups/      — backup files created by test_recovery.py
"""

import json
import logging
import os
import shutil
import sys
import time

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BACKUP_DIR = os.path.join(APP_DIR, ".editor_backups")

PENDING_FILE = os.path.join(DATA_DIR, ".test_pending")
FAILURE_FILE = os.path.join(DATA_DIR, ".boot_failures")
LOG_FILE = os.path.join(APP_DIR, "logs", "boot_guard.log")

MAX_FAILURES_BEFORE_ROLLBACK = 1

# ----------------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------------

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - boot_guard - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("boot_guard")


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def _read_json(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _remove(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _normalise_batch(pending: dict) -> list:
    """
    Return a list of file entries: [{path, backup, was_new}, ...]
    Supports both the new batch schema and the legacy single-file schema.
    """
    if "files" in pending and isinstance(pending["files"], list):
        return pending["files"]
    # Legacy: single file at top level
    if "path" in pending:
        return [{
            "path": pending.get("path"),
            "backup": pending.get("backup") or pending.get("backup_path"),
            "backup_name": pending.get("backup_name"),
            "was_new": False,
        }]
    return []


def _resolve_backup_path(entry: dict) -> str:
    """Find the actual backup file on disk for a batch entry."""
    backup = entry.get("backup")
    if backup and os.path.isabs(backup) and os.path.isfile(backup):
        return backup
    if backup:
        candidate = os.path.join(BACKUP_DIR, os.path.basename(backup))
        if os.path.isfile(candidate):
            return candidate
    # Fallback: pattern search by path
    path = entry.get("path", "")
    safe_name = path.replace("/", "_").replace("\\", "_")
    pattern_prefix = f"{safe_name}."
    if os.path.isdir(BACKUP_DIR):
        candidates = sorted(
            [f for f in os.listdir(BACKUP_DIR)
             if f.startswith(pattern_prefix) and "test_recovery" in f],
            reverse=True,
        )
        if candidates:
            return os.path.join(BACKUP_DIR, candidates[0])
    return ""


def _rollback_entry(entry: dict) -> bool:
    """Restore or delete a single file. Returns True on success."""
    path = entry.get("path")
    was_new = entry.get("was_new", False)
    if not path:
        log.error(f"Batch entry missing 'path': {entry}")
        return False

    target = os.path.join(APP_DIR, path)

    if was_new:
        # This file did not exist before the test — delete it
        try:
            if os.path.isfile(target):
                os.remove(target)
                log.info(f"ROLLBACK: deleted new file {path}")
            return True
        except Exception as e:
            log.error(f"Failed to delete new file {path}: {e}")
            return False

    backup_path = _resolve_backup_path(entry)
    if not backup_path:
        log.error(f"ROLLBACK: no backup found for {path}")
        return False

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(backup_path, target)
        log.info(f"ROLLBACK: {path} restored from {backup_path}")
        return True
    except Exception as e:
        log.error(f"Rollback copy failed for {path}: {e}")
        return False


# ----------------------------------------------------------------------------
# MAIN LOGIC
# ----------------------------------------------------------------------------

def main():
    log.info("Boot guard running...")

    pending = _read_json(PENDING_FILE)
    failures = _read_json(FAILURE_FILE)

    # No pending → clean up and exit
    if not pending:
        if os.path.isfile(FAILURE_FILE):
            _remove(FAILURE_FILE)
            log.info("No test pending — cleaned stale failure counter")
        else:
            log.info("No test pending — nothing to do")
        return 0

    entries = _normalise_batch(pending)
    if not entries:
        log.warning("Pending file has no recognisable files — clearing")
        _remove(PENDING_FILE)
        _remove(FAILURE_FILE)
        return 0

    fail_count = failures.get("count", 0)
    paths = [e.get("path") for e in entries]
    log.info(f"Test pending: {len(entries)} file(s) {paths} (failures: {fail_count})")

    # Need to rollback?
    if fail_count >= MAX_FAILURES_BEFORE_ROLLBACK:
        log.warning(
            f"Boot failure threshold reached ({fail_count}) "
            f"— rolling back batch of {len(entries)} file(s)"
        )

        ok_count = 0
        fail_count_rb = 0
        for entry in entries:
            if _rollback_entry(entry):
                ok_count += 1
            else:
                fail_count_rb += 1

        # Always clear markers — even partial rollback is better than a loop
        _remove(PENDING_FILE)
        _remove(FAILURE_FILE)
        log.info(
            f"Rollback complete: {ok_count} restored, {fail_count_rb} failed — markers cleared"
        )
        return 0

    # First failure (or no failure yet) → increment counter
    new_count = fail_count + 1
    _write_json(FAILURE_FILE, {
        "count": new_count,
        "files": paths,
        "last_attempt": time.time(),
    })
    log.info(f"Boot failure counter set to {new_count} for batch: {paths}")
    return 0


# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        code = main()
        sys.exit(code)
    except Exception as e:
        log.critical(f"Boot guard crashed: {e}", exc_info=True)
        # Never block the service from starting
        sys.exit(0)