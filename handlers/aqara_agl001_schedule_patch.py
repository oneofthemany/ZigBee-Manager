"""
Runtime fix for overly-strict time validation in zhaquirks' Aqara E1 (agl001)
thermostat schedule parser.

The quirk's ScheduleEvent._validate_time raises ValueError when the device
reports a schedule slot with an unset / sentinel time field, which aborts the
entire attribute report (losing setpoint, temperature, running_state, etc).

This module replaces _validate_time with a no-op, so out-of-range times are
accepted silently. We never write schedules from this codebase — reads are
the only path — so tolerating garbage time bytes is safe.

Import once at startup:
    from handlers import aqara_agl001_schedule_patch  # noqa: F401
"""
import logging

logger = logging.getLogger("modules.aqara_agl001_patch")

try:
    from zhaquirks.xiaomi.aqara.thermostat_agl001 import ScheduleEvent

    # Preserve original for debugging
    _original = ScheduleEvent._validate_time

    def _tolerant_validate_time(self, time_value):
        # Accept any value; log the out-of-range ones at DEBUG
        if not (0 <= int(time_value) <= 1439):
            logger.debug(
                f"agl001 schedule time out of range ({time_value}) — accepted anyway"
            )

    ScheduleEvent._validate_time = _tolerant_validate_time
    logger.info("Aqara E1 (agl001) _validate_time patched — schedule decode no longer raises")

except ImportError:
    logger.debug("agl001 quirk not installed — patch skipped")
except AttributeError:
    logger.warning("ScheduleEvent._validate_time not found — quirk structure changed, patch skipped")