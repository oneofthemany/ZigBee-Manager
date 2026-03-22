#!/usr/bin/env python3
"""
Boot Guard — Pre-startup rollback for failed test deployments.
================================================================
Runs BEFORE main.py via systemd ExecStartPre. Uses ONLY stdlib
(no application imports) so it cannot be broken by bad code deploys.

Flow:
  1. Check if a test deployment is pending (.test_pending file)
  2. If pending, check if a boot-fail counter exists (.boot_failures)
  3. If counter >= 1, the previous start attempt failed:
     → Restore the backup file
     → Remove the pending marker and counter
     → Log the rollback
     → Exit 0 (let main.py start with restored code)
  4. If counter doesn't exist, this is the first start after deploy:
     → Create counter with count=1
     → Exit 0 (let main.py try to start)
  5. If no test pending, ensure counter is clean:
     → Remove any stale counter file
     → Exit 0

The counter increments on each failed start. Since systemd calls
ExecStartPre before each start attempt, the flow is:

  Deploy → Restart #1:
    boot_guard: no counter, pending exists → create counter=1 → exit 0
    main.py: crashes (bad import, syntax, etc)
    systemd: records failure, tries Restart=on-failure

  Restart #2:
    boot_guard: counter=1, pending exists → ROLLBACK → remove both → exit 0
    main.py: starts with restored code → works
    app startup: clears .test_pending if it somehow survived

Files:
  .test_pending    — written by test_recovery.py deploy_test()
  .boot_failures   — written/read by this script only
  .editor_backups/ — backup files created by test_recovery.py

Install in systemd service file:
  ExecStartPre=/path/to/venv/bin/python3 /path/to/boot_guard.py
"""

import json
import logging
import os
import shutil
import sys
import time

# ============================================================================
# CONFIGURATION
# ============================================================================

# These must match the paths used by test_recovery.py
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BACKUP_DIR = os.path.join(APP_DIR, ".editor_backups")

PENDING_FILE = os.path.join(DATA_DIR, ".test_pending")
FAILURE_FILE = os.path.join(DATA_DIR, ".boot_failures")
LOG_FILE = os.path.join(APP_DIR, "logs", "boot_guard.log")

MAX_FAILURES_BEFORE_ROLLBACK = 1  # Rollback after 1 failed start

# ============================================================================
# LOGGING (standalone, no application logger)
# ============================================================================

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


# ============================================================================
# HELPERS
# ============================================================================

def _read_json(path: str) -> dict:
    """Read a JSON file, return empty dict on any error."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _write_json(path: str, data: dict):
    """Write a JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _remove(path: str):
    """Remove a file if it exists."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ============================================================================
# MAIN LOGIC
# ============================================================================

def main():
    log.info("Boot guard running...")

    pending = _read_json(PENDING_FILE)
    failures = _read_json(FAILURE_FILE)

    # ── No test pending → clean up and exit ──
    if not pending or not pending.get("path"):
        if os.path.isfile(FAILURE_FILE):
            _remove(FAILURE_FILE)
            log.info("No test pending — cleaned stale failure counter")
        else:
            log.info("No test pending — nothing to do")
        return 0

    # ── Test is pending ──
    path = pending.get("path", "?")
    backup = pending.get("backup", "")
    fail_count = failures.get("count", 0)

    log.info(f"Test pending: {path} (backup: {backup}, failures: {fail_count})")

    # ── Check if we need to rollback ──
    if fail_count >= MAX_FAILURES_BEFORE_ROLLBACK:
        log.warning(f"Boot failure threshold reached ({fail_count} failures) — rolling back {path}")

        # Find the backup file
        backup_path = None
        if backup:
            # backup field might be just a filename or a full path
            if os.path.isabs(backup):
                backup_path = backup
            else:
                backup_path = os.path.join(BACKUP_DIR, backup)

        # Also check the backup field in pending
        if not backup_path or not os.path.isfile(backup_path):
            backup_path = pending.get("backup_path")

        # Last resort: search for the backup by pattern
        if not backup_path or not os.path.isfile(backup_path):
            safe_name = path.replace("/", "_").replace("\\", "_")
            pattern = f"{safe_name}."
            if os.path.isdir(BACKUP_DIR):
                candidates = sorted(
                    [f for f in os.listdir(BACKUP_DIR)
                     if f.startswith(pattern) and "test_recovery" in f],
                    reverse=True,  # Most recent first
                )
                if candidates:
                    backup_path = os.path.join(BACKUP_DIR, candidates[0])

        if backup_path and os.path.isfile(backup_path):
            target = os.path.join(APP_DIR, path)
            try:
                shutil.copy2(backup_path, target)
                log.info(f"ROLLED BACK: {path} restored from {backup_path}")
            except Exception as e:
                log.error(f"Rollback copy failed: {e}")
                return 1
        else:
            log.error(f"Cannot rollback: no backup found for {path}")
            log.error(f"Searched: {backup_path}, BACKUP_DIR={BACKUP_DIR}")
            # Still remove the markers to prevent infinite loop
            _remove(PENDING_FILE)
            _remove(FAILURE_FILE)
            return 1

        # Clean up markers
        _remove(PENDING_FILE)
        _remove(FAILURE_FILE)
        log.info("Rollback complete — markers cleared")
        return 0

    # ── First failure (or no failure yet) → increment counter ──
    new_count = fail_count + 1
    _write_json(FAILURE_FILE, {
        "count": new_count,
        "path": path,
        "last_attempt": time.time(),
    })
    log.info(f"Boot failure counter set to {new_count} for {path}")
    return 0


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    try:
        code = main()
        sys.exit(code)
    except Exception as e:
        log.critical(f"Boot guard crashed: {e}", exc_info=True)
        # Exit 0 even on crash — don't prevent the service from starting
        # The app may still work even if the guard failed
        sys.exit(0)