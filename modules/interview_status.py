"""
interview_status.py — observe and report device interview state.

This module never drives the interview; it only observes. zigpy owns the
actual interview state machine. We compute a higher-level status for the
frontend based on:

  - zigpy.device.is_initialized
  - zigpy.device.node_desc (presence and contents)
  - zigpy.device.endpoints (count and per-endpoint cluster population)
  - last_seen on the wrapper (recent traffic = device probably awake)
  - join_at timestamp recorded when the device first joined

Thresholds reflect what's reasonable for the Zigbee protocol, not arbitrary
choices:

  - apsAckWaitDuration is ~1.6s; full interview is typically 5-15 round
    trips. Mains devices that don't complete in 60s have a real problem.
  - Battery devices may legitimately take much longer because they sleep
    between transmissions, but if the user is keeping the device awake
    (recent traffic is the proxy for this) the same 60s budget applies
    plus headroom for poll intervals.
  - 24 hours of "interviewing" is excessive regardless. At that point the
    device should be considered failed and the user advised to re-pair.

The state machine is purely derivative — call get_status(ieee) any time and
get the current truth. Cached values are only kept to detect transitions
for emitting WebSocket events; they're never the source of truth.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .service import ZigManService

logger = logging.getLogger("modules.interview_status")


# ---------------------------------------------------------------------------
# Thresholds (seconds)
# ---------------------------------------------------------------------------

# Mains-powered device should complete interview in this window. Beyond it,
# something is wrong (firmware, route, configuration).
_MAINS_FAILED_AFTER = 60

# Battery device with recent traffic is considered "user holding it awake".
# Same budget as mains plus headroom for the device's own poll interval.
_BATTERY_STALLED_AFTER = 120

# A packet within this window means the device is currently active.
_RECENT_TRAFFIC_WINDOW = 5

# Battery device with no recent traffic stays "interviewing" until this
# total window expires. Past this, mark stalled regardless of activity —
# 30 minutes is far longer than any legitimate sleepy interview.
_BATTERY_TOTAL_STALL_LIMIT = 30 * 60

# After this point, no device should still be interviewing. Mark failed.
_HARD_FAIL_AFTER = 24 * 60 * 60


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Interview state values returned to callers / frontend
STATE_UNKNOWN = "unknown"           # No zigpy device or not yet observable
STATE_INTERVIEWING = "interviewing" # zigpy still working, within budget
STATE_INTERVIEWED = "interviewed"   # is_initialized == True
STATE_STALLED = "stalled"           # Past budget, but device active recently
STATE_FAILED = "failed"             # Past hard limit, needs re-pairing


@dataclass
class InterviewSnapshot:
    """One observation of a device's interview state."""
    ieee: str
    state: str
    advice: str
    current_step: Optional[str]      # 'node_descriptor' / 'active_endpoints' / 'simple_descriptor_ep_N' / None
    elapsed_s: int                   # Seconds since join
    is_battery: Optional[bool]       # None if unknown
    last_seen_s_ago: Optional[int]   # None if never seen
    # What we know — both raw and resolved where applicable
    facts: dict = field(default_factory=dict)
    # What we don't know yet
    missing: dict = field(default_factory=dict)
    # Action availability hints for the frontend
    can_retry: bool = False
    can_repair: bool = False         # "Delete and re-pair" only safe when failed

    def to_dict(self) -> dict:
        return {
            "ieee": self.ieee,
            "state": self.state,
            "advice": self.advice,
            "current_step": self.current_step,
            "elapsed_s": self.elapsed_s,
            "is_battery": self.is_battery,
            "last_seen_s_ago": self.last_seen_s_ago,
            "facts": self.facts,
            "missing": self.missing,
            "can_retry": self.can_retry,
            "can_repair": self.can_repair,
        }


class InterviewStatusTracker:
    """
    Per-service tracker. Records join times and the last emitted state so
    we know when to emit transition events.

    Attach one to the service:
        self.interview_status = InterviewStatusTracker(self)
    """

    def __init__(self, service: "ZigManService"):
        self.service = service
        # ieee → join timestamp (epoch seconds). Set on device_joined,
        # cleared on remove. None if device existed before tracker started
        # (we'll fall back to a synthetic value).
        self._join_at: dict[str, float] = {}
        # ieee → last emitted state. Used to detect state transitions for
        # WebSocket events.
        self._last_state: dict[str, str] = {}
        # ieee → currently-running interview step (set by record_step).
        # Only meaningful while a retry_interview is in flight.
        self._current_step: dict[str, str] = {}
        # ieee → step start timestamp for elapsed computation
        self._step_started: dict[str, float] = {}

    # ----- lifecycle hooks -----

    def on_device_joined(self, ieee: str) -> None:
        """Call from service.device_joined."""
        self._join_at[ieee] = time.time()
        self._last_state.pop(ieee, None)
        self._current_step.pop(ieee, None)
        self._step_started.pop(ieee, None)

    def on_device_removed(self, ieee: str) -> None:
        """Call from service.remove path so stale state doesn't leak."""
        self._join_at.pop(ieee, None)
        self._last_state.pop(ieee, None)
        self._current_step.pop(ieee, None)
        self._step_started.pop(ieee, None)

    def record_step(self, ieee: str, step: Optional[str]) -> None:
        """
        Mark which interview step is currently running for live UI.

        Pass None when no step is active (e.g., interview finished).
        """
        if step is None:
            self._current_step.pop(ieee, None)
            self._step_started.pop(ieee, None)
        else:
            self._current_step[ieee] = step
            self._step_started[ieee] = time.time()
        self._maybe_emit(ieee)

    def emit_for(self, ieee: str) -> None:
        """Force an emission for this ieee (e.g., after a step completes)."""
        snapshot = self.get_status(ieee)
        if snapshot is None:
            return
        self._emit(snapshot)

    # ----- public API -----

    def get_status(self, ieee: str) -> Optional[InterviewSnapshot]:
        """
        Compute the current snapshot for one device.

        Returns None only if the IEEE is completely unknown (not in
        service.devices). Otherwise returns a snapshot — the device may
        still be in any state including unknown.
        """
        if ieee not in self.service.devices:
            return None

        wrapper = self.service.devices[ieee]
        zdev = wrapper.zigpy_dev
        now = time.time()

        # --- Elapsed since join ---
        join_at = self._join_at.get(ieee)
        if join_at is None:
            # Device existed before tracker started — synthesise a join
            # time from last_seen so elapsed is non-negative. If we have
            # neither, default to "now" so it looks like a fresh join.
            last_seen_ms = getattr(wrapper, "last_seen", 0) or 0
            join_at = (last_seen_ms / 1000.0) if last_seen_ms else now
            self._join_at[ieee] = join_at
        elapsed_s = int(now - join_at)

        # --- Last seen ---
        last_seen_ms = getattr(wrapper, "last_seen", 0) or 0
        last_seen_s_ago: Optional[int] = None
        if last_seen_ms:
            last_seen_s_ago = max(0, int(now - last_seen_ms / 1000.0))

        # --- Power source from Node Descriptor (only fact, no inference) ---
        is_battery: Optional[bool] = None
        nd = getattr(zdev, "node_desc", None)
        if nd is not None:
            try:
                # If is_mains_powered is True, definitely not battery.
                # If is_receiver_on_when_idle is True, also definitely
                # not a sleepy device.
                mains = bool(getattr(nd, "is_mains_powered", False))
                always_on = bool(getattr(nd, "is_receiver_on_when_idle", False))
                is_battery = not (mains or always_on)
            except Exception:
                is_battery = None

        recently_active = (
                last_seen_s_ago is not None
                and last_seen_s_ago < _RECENT_TRAFFIC_WINDOW
        )

        # --- Compute state ---
        if zdev.is_initialized:
            state = STATE_INTERVIEWED
            advice = "Device is fully interviewed and ready."
        elif elapsed_s >= _HARD_FAIL_AFTER:
            state = STATE_FAILED
            advice = (
                "This device has not completed setup after 24 hours. "
                "Delete it and re-pair."
            )
        elif is_battery is False:
            # Mains device — strict deadline
            if elapsed_s >= _MAINS_FAILED_AFTER:
                state = STATE_FAILED
                advice = (
                    f"Mains-powered device did not complete interview within "
                    f"{_MAINS_FAILED_AFTER}s. Check the device's connection, "
                    f"or delete and re-pair."
                )
            else:
                state = STATE_INTERVIEWING
                advice = (
                    f"Interview in progress ({elapsed_s}s of "
                    f"{_MAINS_FAILED_AFTER}s budget)."
                )
        elif is_battery is True:
            # Battery device — patience required
            if elapsed_s >= _BATTERY_TOTAL_STALL_LIMIT:
                state = STATE_STALLED
                advice = (
                    "This battery device has been interviewing for over "
                    "30 minutes. Wake it manually (press its button or "
                    "open the valve) and click Re-Interview."
                )
            elif elapsed_s >= _BATTERY_STALLED_AFTER and recently_active:
                state = STATE_STALLED
                advice = (
                    "Device is awake but interview hasn't progressed. "
                    "Click Re-Interview while keeping the device awake."
                )
            else:
                state = STATE_INTERVIEWING
                if recently_active:
                    advice = (
                        f"Interview in progress ({elapsed_s}s elapsed). "
                        f"Device is currently awake."
                    )
                else:
                    advice = (
                        f"Waiting for the device to wake up. "
                        f"({elapsed_s}s since join)"
                    )
        else:
            # Power source unknown — node descriptor not yet retrieved.
            # Use mains thresholds as the conservative path.
            if elapsed_s >= _MAINS_FAILED_AFTER and not recently_active:
                state = STATE_STALLED
                advice = (
                    "Initial communication has not started. The device may "
                    "be out of range, off, or asleep. Wake it and click "
                    "Re-Interview."
                )
            else:
                state = STATE_INTERVIEWING
                advice = "Waiting for the device to send its Node Descriptor."

        # --- Current step ---
        current_step = self._current_step.get(ieee)

        # --- Facts dict ---
        facts = self._gather_facts(zdev, wrapper)

        # --- Missing dict ---
        missing = self._gather_missing(zdev)

        # --- Action flags ---
        can_retry = state in (STATE_INTERVIEWING, STATE_STALLED, STATE_FAILED)
        can_repair = state == STATE_FAILED

        return InterviewSnapshot(
            ieee=ieee,
            state=state,
            advice=advice,
            current_step=current_step,
            elapsed_s=elapsed_s,
            is_battery=is_battery,
            last_seen_s_ago=last_seen_s_ago,
            facts=facts,
            missing=missing,
            can_retry=can_retry,
            can_repair=can_repair,
        )

    def get_all_pending(self) -> list[dict]:
        """
        Return snapshots for every device not in INTERVIEWED state.

        Useful for the frontend's "needs attention" indicator on the
        device list.
        """
        out: list[dict] = []
        for ieee in list(self.service.devices.keys()):
            snap = self.get_status(ieee)
            if snap is None:
                continue
            if snap.state != STATE_INTERVIEWED:
                out.append(snap.to_dict())
        return out

    # ----- internals -----

    def _gather_facts(self, zdev, wrapper) -> dict:
        """
        Collect every fact zigpy currently knows about a device.

        Each fact has both raw value and resolved/human-readable form
        where applicable. We never invent values — if zigpy doesn't have
        a piece of data, we omit the key entirely.
        """
        facts: dict = {}

        # Network address
        if getattr(zdev, "nwk", None) is not None:
            facts["nwk"] = {
                "raw": int(zdev.nwk),
                "name": f"0x{int(zdev.nwk):04x}",
            }

        # Manufacturer code from Node Descriptor + zigpy's own name lookup
        nd = getattr(zdev, "node_desc", None)
        if nd is not None:
            mfr_code = getattr(nd, "manufacturer_code", None)
            if mfr_code is not None:
                facts["manufacturer_code"] = {
                    "raw": int(mfr_code),
                    "name": self._lookup_manufacturer_name(int(mfr_code)),
                }
            # Logical type — enum gives us the human form for free
            lt = getattr(nd, "logical_type", None)
            if lt is not None:
                facts["logical_type"] = {
                    "raw": int(lt) if hasattr(lt, "__int__") else None,
                    "name": getattr(lt, "name", str(lt)),
                }
            # Power & always-on flags
            for attr in (
                    "is_mains_powered",
                    "is_receiver_on_when_idle",
                    "is_router",
                    "is_end_device",
                    "is_coordinator",
                    "is_full_function_device",
                    "is_alternate_pan_coordinator",
                    "is_security_capable",
            ):
                val = getattr(nd, attr, None)
                if val is not None:
                    facts[attr] = {"raw": bool(val), "name": "Yes" if val else "No"}
            # Frequency band (enum)
            fb = getattr(nd, "frequency_band", None)
            if fb is not None:
                facts["frequency_band"] = {
                    "raw": int(fb) if hasattr(fb, "__int__") else None,
                    "name": getattr(fb, "name", str(fb)),
                }
            # Buffer sizes — raw integers, no resolved form
            for attr in (
                    "maximum_buffer_size",
                    "maximum_incoming_transfer_size",
                    "maximum_outgoing_transfer_size",
                    "server_mask",
            ):
                val = getattr(nd, attr, None)
                if val is not None:
                    facts[attr] = {"raw": int(val), "name": str(int(val))}

        # Manufacturer / model from device's own Basic cluster
        if getattr(zdev, "manufacturer", None):
            facts["manufacturer"] = {
                "raw": str(zdev.manufacturer),
                "name": str(zdev.manufacturer),
            }
        if getattr(zdev, "model", None):
            facts["model"] = {
                "raw": str(zdev.model),
                "name": str(zdev.model),
            }

        # Quirk class if any
        quirk_name = getattr(wrapper, "quirk_name", None)
        if quirk_name and quirk_name != "None":
            facts["quirk"] = {"raw": str(quirk_name), "name": str(quirk_name)}

        return facts

    def _gather_missing(self, zdev) -> dict:
        """
        Describe what zigpy hasn't discovered yet about this device.

        Frontend can use this to show "what's still pending" in plain English.
        """
        missing: dict = {}

        if getattr(zdev, "node_desc", None) is None:
            missing["node_descriptor"] = (
                "The device hasn't replied with its Node Descriptor yet. "
                "Without this we don't know whether it's mains or battery, "
                "or its capabilities."
            )

        endpoints = getattr(zdev, "endpoints", {}) or {}
        non_zdo = [ep_id for ep_id in endpoints if ep_id != 0]
        if not non_zdo:
            missing["active_endpoints"] = (
                "No endpoints have been discovered yet. The device hasn't "
                "told us what functionality it offers."
            )
        else:
            ep_details = []
            try:
                import zigpy.endpoint
                new_status = zigpy.endpoint.Status.NEW
            except Exception:
                new_status = None

            for ep_id in non_zdo:
                ep = endpoints[ep_id]
                in_count = len(getattr(ep, "in_clusters", {}) or {})
                out_count = len(getattr(ep, "out_clusters", {}) or {})
                status = getattr(ep, "status", None)
                stuck = (new_status is not None and status == new_status)
                if stuck or (in_count == 0 and out_count == 0):
                    ep_details.append({
                        "endpoint_id": ep_id,
                        "in_clusters": in_count,
                        "out_clusters": out_count,
                        "status": getattr(status, "name", str(status)),
                        "issue": "Simple Descriptor incomplete",
                    })
            if ep_details:
                missing["incomplete_endpoints"] = ep_details

        return missing

    def _lookup_manufacturer_name(self, code: int) -> str:
        """
        Resolve a Zigbee manufacturer code to a name using zigpy's own
        registry. Never invent names — if zigpy doesn't recognise it,
        return the hex code as the "name" so the user has something to
        Google.
        """
        try:
            from zigpy.types.named import ManufacturerID
            try:
                return ManufacturerID(code).name
            except (ValueError, KeyError):
                pass
        except ImportError:
            pass
        return f"0x{code:04x}"

    def _maybe_emit(self, ieee: str) -> None:
        """Emit a status update if state changed since last emission."""
        snapshot = self.get_status(ieee)
        if snapshot is None:
            return
        prev = self._last_state.get(ieee)
        if prev != snapshot.state:
            self._last_state[ieee] = snapshot.state
            self._emit(snapshot)
        else:
            # State unchanged but step might have — emit anyway for live UI
            self._emit(snapshot)

    def _emit(self, snapshot: InterviewSnapshot) -> None:
        try:
            self.service._emit_sync(
                "interview_status_update", snapshot.to_dict()
            )
        except Exception as e:
            logger.debug(f"Failed to emit interview status: {e}")