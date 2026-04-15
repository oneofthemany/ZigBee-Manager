"""
aqara_agl001_schedule_patch.py
==============================
Runtime patch for a crash in the vendored `zhaquirks.xiaomi.aqara.thermostat_agl001`
quirk when the Aqara E1 thermostat (lumi.airrtc.agl001) reports a schedule with a
zero-initialised or out-of-range time field.

The quirk's ScheduleEvent._validate_time raises `ValueError: Time must be between
00:00 and 23:59`, which propagates up through `handle_cluster_general_request`,
aborting the whole packet. This affects temperature/setpoint reporting even
though the bad field is *only* in the schedule block.

This module wraps ScheduleSettings.__new__ to silently replace any event that
fails to parse with a sentinel "unset" event, so the rest of the attributes
continue to decode normally.

Usage:
    Import this module once at startup (e.g. in main.py before zigbee_service
    is created):
        from handlers import aqara_agl001_schedule_patch  # noqa: F401

It is idempotent and safe to import multiple times.
"""
import logging

logger = logging.getLogger("modules.aqara_agl001_patch")

_PATCHED = False


def apply_patch() -> bool:
    """Install the runtime patch. Returns True if applied, False if skipped."""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from zhaquirks.xiaomi.aqara import thermostat_agl001 as mod
    except ImportError:
        logger.debug("agl001 quirk not installed — patch skipped")
        return False

    ScheduleSettings = getattr(mod, "ScheduleSettings", None)
    ScheduleEvent = getattr(mod, "ScheduleEvent", None)
    if ScheduleSettings is None or ScheduleEvent is None:
        logger.warning("agl001 quirk structure changed — patch cannot be applied")
        return False

    # Keep originals for fallback to pristine behaviour on valid data
    _original_read_event = getattr(ScheduleSettings, "_read_event", None)
    if _original_read_event is None:
        logger.warning("ScheduleSettings._read_event not found — patch cannot be applied")
        return False

    def _safe_read_event(cls, value, i):
        try:
            return _original_read_event(value, i)
        except (ValueError, TypeError, IndexError) as e:
            # Log once per device per hour, roughly — just a debug line here
            logger.debug(f"agl001 schedule event {i} invalid ({e!r}); substituting placeholder")
            # Return a sentinel "unset" event the caller can tolerate
            try:
                # Try to build a default ScheduleEvent with safe values
                # Event layout: 8 bytes per event (varies by firmware)
                safe_buf = b"\x00" * 8
                # Set time to 00:00 and temp to 20.0°C (200 in 0.1°C units)
                # Exact layout isn't critical — we just need something parseable
                return ScheduleEvent(safe_buf) if hasattr(ScheduleEvent, "__init__") else None
            except Exception:
                return None

    # Patch as a classmethod-compatible wrapper
    # The original is a @staticmethod in the quirk; wrap accordingly
    import functools

    @staticmethod
    @functools.wraps(_original_read_event)
    def _patched_read_event(value, i):
        try:
            return _original_read_event(value, i)
        except (ValueError, TypeError, IndexError) as e:
            logger.debug(
                f"agl001 ScheduleEvent {i} decode failed ({e!r}); returning None placeholder"
            )
            return None

    ScheduleSettings._read_event = _patched_read_event

    # Also wrap __new__ so that if any _read_event returns None, the containing
    # ScheduleSettings construction doesn't blow up when it tries to use it.
    _original_new = ScheduleSettings.__new__

    def _patched_new(cls, value):
        try:
            return _original_new(cls, value)
        except (ValueError, TypeError, IndexError) as e:
            logger.warning(
                f"agl001 ScheduleSettings decode failed entirely ({e!r}); "
                "returning empty schedule so the rest of the attribute report can be processed"
            )
            # Return a benign empty instance. We use object.__new__ to bypass
            # the custom __new__ that would re-raise.
            obj = object.__new__(cls)
            # Populate events with None placeholders so downstream code can cope
            if hasattr(cls, "__init__"):
                try:
                    obj.events = [None] * 4  # E1 exposes 4 schedule events
                except Exception:
                    pass
            return obj

    ScheduleSettings.__new__ = _patched_new

    _PATCHED = True
    logger.info("Aqara E1 (agl001) ScheduleSettings patch applied — invalid schedules will no longer abort packet processing")
    return True


# Auto-apply on import
apply_patch()