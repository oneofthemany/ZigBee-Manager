"""
Boot Guard Hooks — called from main.py to signal successful startup.

Only uses stdlib. Safe to import from anywhere.
"""

import json
import logging
import os

logger = logging.getLogger("boot_guard")

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_DIR, "data")
FAILURE_FILE = os.path.join(DATA_DIR, ".boot_failures")
PENDING_FILE = os.path.join(DATA_DIR, ".test_pending")


def clear_boot_failure_counter():
    """
    Called after the application starts successfully.
    Removes the boot failure counter so the next restart
    doesn't trigger a rollback.
    """
    try:
        if os.path.isfile(FAILURE_FILE):
            with open(FAILURE_FILE) as f:
                data = json.load(f)
            os.remove(FAILURE_FILE)
            logger.info(f"Boot failure counter cleared (was {data.get('count', '?')} for {data.get('path', '?')})")
        else:
            logger.debug("No boot failure counter to clear")
    except Exception as e:
        logger.warning(f"Failed to clear boot failure counter: {e}")