"""
Heating Controller — Active control of receivers and TRVs.
==========================================================
Sits alongside HeatingAdvisor (which is read-only/analytical).
The Controller actually sends commands to make heating happen.

Model:
    Circuit (a receiver/zone valve calling for boiler heat)
      └── Room (a heated space with target temp)
            └── TRV(s) (regulate flow into that room's radiators)

Per-tick decision flow:
    1. Snapshot device states
    2. Classify each room: COLD / ONTARGET / HOT (with hysteresis)
    3. Decide each circuit: CALLING (any room cold) / IDLE (all rooms ok)
    4. Decide each TRV's setpoint:
         - room COLD          → setpoint = target          (open via own thermostat)
         - room HOT           → setpoint = current - 1.0   (force close, prevent stealing)
         - room ONTARGET      → setpoint = target          (idle)
    5. Apply receiver state changes (only if differ from last command)
    6. Apply TRV setpoint changes (only if differ from last command + larger than 0.5°C)

Behaviour matches user choices:
    - "Comfort first": every room can call for heat
    - "Absorb to target only": no overshoot allowed
    - "Force-close TRVs of hot rooms": prevent demand stealing

Config (config.yaml under heating.circuits):
  heating:
    circuits:
      - id: downstairs
        name: "Downstairs"
        receiver_ieee: "00:15:8d:00:00:aa:bb:cc"
        receiver_command: switch          # 'switch' (on/off) or 'thermostat'
        receiver_endpoint: 1              # optional endpoint id
        rooms:
          - id: living_room
            name: "Living Room"
            target_temp: 20.5
            night_setback: 17.0
            min_temp: 16.0
            trv_ieees: ["aa:bb:..."]
            schedule:
              - days: [mon,tue,wed,thu,fri]
                start: "07:00"
                end:   "22:00"
                temp:  20.5
          - id: kitchen
            name: "Kitchen"
            target_temp: 18.0
            trv_ieees: ["cc:dd:..."]
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.heating_controller")

# Hysteresis bands (°C) — prevents oscillation
COLD_BAND = 0.5     # room is COLD if temp < target - 0.5
HOT_BAND = 0.3      # room is HOT  if temp > target + 0.3

# Force-close offset — when shutting a TRV, set it to (current - this) so the
# TRV's own thermostat keeps the valve closed
FORCE_CLOSE_OFFSET = 1.0

# Minimum setpoint change worth sending (avoid hammering battery TRVs)
MIN_SETPOINT_DELTA = 0.5

# How often the controller loop runs
TICK_INTERVAL_SEC = 60

# Don't repeat the same setpoint command more often than this
COMMAND_COOLDOWN_SEC = 300

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── Helpers ────────────────────────────────────────────────────────
def _as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _parse_hhmm(s: str) -> Optional[int]:
    try:
        hh, mm = str(s).split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _device_state(dev: Any) -> Dict[str, Any]:
    """Extract state dict from either a dict-style or object-style device."""
    if isinstance(dev, dict):
        return dev.get("state") or {}
    return getattr(dev, "state", None) or {}


def _device_friendly_name(dev: Any, fallback: str) -> str:
    if isinstance(dev, dict):
        return dev.get("friendly_name") or dev.get("name") or fallback
    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
    if not name:
        service = getattr(dev, "service", None)
        if service is not None:
            fn_map = getattr(service, "friendly_names", None) or {}
            name = fn_map.get(fallback)
    return name or fallback


# ── Room state ─────────────────────────────────────────────────────
class RoomDecision:
    """Per-tick analysis of a single room."""

    __slots__ = (
        "room_id", "name", "target_temp", "current_temp",
        "status", "calling_for_heat", "trvs",
    )

    def __init__(self, room_id: str, name: str):
        self.room_id = room_id
        self.name = name
        self.target_temp: Optional[float] = None
        self.current_temp: Optional[float] = None
        self.status: str = "unknown"     # cold | ontarget | hot | unknown
        self.calling_for_heat: bool = False
        self.trvs: List[Dict] = []

    def to_dict(self) -> Dict:
        return {
            "room_id": self.room_id,
            "name": self.name,
            "target_temp": self.target_temp,
            "current_temp": self.current_temp,
            "status": self.status,
            "calling_for_heat": self.calling_for_heat,
            "trvs": self.trvs,
        }


# ── Controller ─────────────────────────────────────────────────────
class HeatingController:
    """Active control of multi-zone heating with TRV coordination."""

    def __init__(self, config: dict, device_getter: Callable,
                 command_sender: Callable, comfort_defaults: Optional[dict] = None):
        """
        Args:
            config: heating config block (will read 'circuits' and 'enabled')
            device_getter: callable returning {ieee: device}
            command_sender: async callable (ieee, command, value) -> coroutine
                            e.g. zigbee_service.send_command
            comfort_defaults: optional defaults for night_setback, min_temp, etc.
                              from heating.comfort
        """
        config = config or {}
        # Controller is enabled only if both heating.enabled AND heating.controller.enabled
        controller_cfg = config.get("controller") or {}
        self.enabled = bool(config.get("enabled", False)) and \
                       bool(controller_cfg.get("enabled", False))
        self.dry_run = bool(controller_cfg.get("dry_run", False))

        self._get_devices = device_getter
        self._send_command = command_sender

        defaults = comfort_defaults or {}
        self._default_target = _as_float(defaults.get("target_temp"), 21.0)
        self._default_setback = _as_float(defaults.get("night_setback"), 17.0)
        self._default_min = _as_float(defaults.get("min_temp"), 16.0)

        self.circuits = self._clean_circuits(config.get("circuits") or [])

        # Last-command tracking for cooldown / change detection
        # ieee -> (command, value, timestamp)
        self._last_command: Dict[str, Tuple[str, Any, float]] = {}

        # Last decision snapshot (for dashboard/API)
        self._last_decision: Dict[str, Any] = {}
        self._last_decision_ts: float = 0

        self._task: Optional[asyncio.Task] = None

        if self.enabled:
            mode = "DRY-RUN" if self.dry_run else "LIVE"
            n_rooms = sum(len(c["rooms"]) for c in self.circuits)
            n_trvs = sum(len(r["trv_ieees"]) for c in self.circuits for r in c["rooms"])
            logger.info(
                f"Heating Controller [{mode}]: "
                f"{len(self.circuits)} circuits, {n_rooms} rooms, {n_trvs} TRVs"
            )
        elif config.get("circuits"):
            logger.info("Heating Controller defined but not enabled (set heating.controller.enabled: true)")

    # ── Config normalisation ───────────────────────────────────────
    def _clean_circuits(self, circuits: list) -> List[Dict]:
        if not isinstance(circuits, list):
            return []
        out = []
        for c in circuits:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            cid = str(c.get("id") or c["name"]).lower().replace(" ", "_")
            rooms = self._clean_rooms(c.get("rooms") or [])
            out.append({
                "id": cid,
                "name": str(c["name"]),
                "receiver_ieee": str(c.get("receiver_ieee") or "").strip() or None,
                "receiver_command": str(c.get("receiver_command", "switch")).lower(),
                "receiver_endpoint": c.get("receiver_endpoint"),
                "rooms": rooms,
            })
        return out

    def _clean_rooms(self, rooms: list) -> List[Dict]:
        if not isinstance(rooms, list):
            return []
        out = []
        for r in rooms:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            rid = str(r.get("id") or r["name"]).lower().replace(" ", "_")
            trv_ieees = r.get("trv_ieees") or []
            if not isinstance(trv_ieees, list):
                trv_ieees = []
            schedule = r.get("schedule") or []
            if not isinstance(schedule, list):
                schedule = []
            clean_sched = []
            for slot in schedule:
                if not isinstance(slot, dict):
                    continue
                days = slot.get("days") or []
                if not isinstance(days, list):
                    days = []
                clean_sched.append({
                    "days": [d for d in days if d in DAY_KEYS],
                    "start": str(slot.get("start", "07:00")),
                    "end": str(slot.get("end", "22:00")),
                    "temp": _as_float(slot.get("temp"), 20.0),
                })
            out.append({
                "id": rid,
                "name": str(r["name"]),
                "target_temp": _as_float(r.get("target_temp"), self._default_target),
                "night_setback": _as_float(r.get("night_setback"), self._default_setback),
                "min_temp": _as_float(r.get("min_temp"), self._default_min),
                "trv_ieees": [str(i) for i in trv_ieees if i],
                "schedule": clean_sched,
            })
        return out

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self):
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._control_loop())
        logger.info("Heating Controller started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("Heating Controller stopped")

    async def _control_loop(self):
        # Initial delay so other services finish startup
        await asyncio.sleep(15)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Controller tick failed: {e}", exc_info=True)
            await asyncio.sleep(TICK_INTERVAL_SEC)

    # ── Public introspection ───────────────────────────────────────
    def get_state(self) -> Dict[str, Any]:
        """Last decision snapshot for the dashboard/API."""
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "last_tick_ts": self._last_decision_ts,
            "last_tick_age_seconds": (time.time() - self._last_decision_ts) if self._last_decision_ts else None,
            "circuits": self._last_decision.get("circuits", []),
        }

    async def force_tick(self) -> Dict[str, Any]:
        """Run one tick on demand (used by API for manual evaluate-now)."""
        await self._tick()
        return self.get_state()

    # ── Core control loop ──────────────────────────────────────────
    async def _tick(self):
        devices = self._snapshot_devices()
        now = datetime.now()
        circuits_out = []

        for circuit in self.circuits:
            room_decisions = []
            any_calling = False

            for room in circuit["rooms"]:
                decision = self._evaluate_room(room, devices, now)
                if decision.calling_for_heat:
                    any_calling = True
                room_decisions.append(decision)

            # Circuit-level decision
            should_call = any_calling
            receiver_action = await self._apply_receiver(circuit, should_call)

            # TRV decisions — pass any_calling so we can force-close hot rooms
            trv_actions = []
            for decision in room_decisions:
                room = next((r for r in circuit["rooms"] if r["id"] == decision.room_id), None)
                if room is None:
                    continue
                actions = await self._apply_trvs(room, decision, circuit_calling=should_call)
                trv_actions.extend(actions)
                # Decorate decision dict with the TRV setpoint commands made
                for trv in decision.trvs:
                    a = next((a for a in actions if a["ieee"] == trv["ieee"]), None)
                    if a:
                        trv["intended_setpoint"] = a["target_setpoint"]
                        trv["action"] = a["action"]

            circuits_out.append({
                "id": circuit["id"],
                "name": circuit["name"],
                "calling_for_heat": should_call,
                "receiver_ieee": circuit["receiver_ieee"],
                "receiver_action": receiver_action,
                "rooms": [d.to_dict() for d in room_decisions],
                "trv_actions": trv_actions,
            })

        self._last_decision = {"circuits": circuits_out}
        self._last_decision_ts = time.time()

    # ── Device snapshot ────────────────────────────────────────────
    def _snapshot_devices(self) -> Dict[str, Any]:
        try:
            raw = self._get_devices() or {}
        except Exception as e:
            logger.error(f"device_getter raised: {e}")
            return {}
        return {str(ieee): dev for ieee, dev in raw.items()}

    # ── Room evaluation ────────────────────────────────────────────
    def _evaluate_room(self, room: dict, devices: Dict[str, Any],
                       now: datetime) -> RoomDecision:
        decision = RoomDecision(room_id=room["id"], name=room["name"])
        decision.target_temp = self._effective_target(room, now)

        # Gather TRV temperatures
        temps = []
        trvs = []
        for ieee in room["trv_ieees"]:
            dev = devices.get(ieee)
            if dev is None:
                trvs.append({
                    "ieee": ieee,
                    "name": ieee,
                    "current_temp": None,
                    "current_setpoint": None,
                    "online": False,
                })
                continue
            state = _device_state(dev)
            temp = state.get("local_temperature") or state.get("current_temperature")
            setpoint = state.get("occupied_heating_setpoint") or state.get("target_temp")
            ftemp = _as_float(temp)
            if ftemp is not None:
                temps.append(ftemp)
            trvs.append({
                "ieee": ieee,
                "name": _device_friendly_name(dev, ieee),
                "current_temp": ftemp,
                "current_setpoint": _as_float(setpoint),
                "online": True,
            })

        decision.trvs = trvs
        decision.current_temp = round(sum(temps) / len(temps), 1) if temps else None

        # Classify with hysteresis
        if decision.current_temp is None or decision.target_temp is None:
            decision.status = "unknown"
            decision.calling_for_heat = False
        elif decision.current_temp < decision.target_temp - COLD_BAND:
            decision.status = "cold"
            decision.calling_for_heat = True
        elif decision.current_temp > decision.target_temp + HOT_BAND:
            decision.status = "hot"
            decision.calling_for_heat = False
        else:
            decision.status = "ontarget"
            decision.calling_for_heat = False

        return decision

    def _effective_target(self, room: dict, now: datetime) -> float:
        """Pick target from active schedule slot, fall back to night setback or default."""
        day = DAY_KEYS[now.weekday()]
        now_minutes = now.hour * 60 + now.minute

        for slot in room.get("schedule", []):
            if day not in (slot.get("days") or []):
                continue
            start_m = _parse_hhmm(slot.get("start", "00:00"))
            end_m = _parse_hhmm(slot.get("end", "23:59"))
            if start_m is None or end_m is None:
                continue
            in_slot = (start_m <= now_minutes < end_m) if start_m <= end_m \
                else (now_minutes >= start_m or now_minutes < end_m)
            if in_slot:
                return float(slot.get("temp", room["target_temp"]))

        # Overnight default setback (22:00 – 06:00)
        if now_minutes >= 22 * 60 or now_minutes < 6 * 60:
            return float(room.get("night_setback", self._default_setback))

        return float(room.get("target_temp", self._default_target))

    # ── Receiver control ───────────────────────────────────────────
    async def _apply_receiver(self, circuit: dict, should_call: bool) -> Dict[str, Any]:
        """
        Switch the receiver on/off. Idempotent — only sends if differs from
        the last command.
        """
        ieee = circuit.get("receiver_ieee")
        if not ieee:
            return {"sent": False, "reason": "no receiver configured"}

        target_command = "on" if should_call else "off"
        last = self._last_command.get(ieee)
        if last and last[0] == target_command:
            return {"sent": False, "reason": "unchanged", "command": target_command}

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would send receiver '{circuit['name']}' ({ieee}) → {target_command}")
            self._last_command[ieee] = (target_command, None, time.time())
            return {"sent": True, "command": target_command, "dry_run": True}

        try:
            await self._send_command(ieee, target_command, None,
                                     endpoint_id=circuit.get("receiver_endpoint"))
            self._last_command[ieee] = (target_command, None, time.time())
            logger.info(f"Receiver '{circuit['name']}' ({ieee}) → {target_command}")
            return {"sent": True, "command": target_command}
        except TypeError:
            # Fallback for command_sender that doesn't accept endpoint_id kwarg
            try:
                await self._send_command(ieee, target_command, None)
                self._last_command[ieee] = (target_command, None, time.time())
                return {"sent": True, "command": target_command}
            except Exception as e:
                logger.error(f"Receiver command failed: {e}")
                return {"sent": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Receiver command failed: {e}")
            return {"sent": False, "error": str(e)}

    # ── TRV control ────────────────────────────────────────────────
    async def _apply_trvs(self, room: dict, decision: RoomDecision,
                          circuit_calling: bool) -> List[Dict]:
        """
        Decide and apply TRV setpoints per room status:
          - cold:     setpoint = target  (open via own thermostat)
          - ontarget: setpoint = target  (idle)
          - hot:      setpoint = current - FORCE_CLOSE_OFFSET (force-close)
                      ONLY IF circuit_calling — otherwise harmless to leave open
        """
        actions = []
        target = decision.target_temp
        if target is None:
            return actions

        for trv in decision.trvs:
            ieee = trv["ieee"]
            current_temp = trv.get("current_temp")
            current_sp = trv.get("current_setpoint")

            # Decide intended setpoint
            if decision.status == "hot" and circuit_calling and current_temp is not None:
                intended = round(current_temp - FORCE_CLOSE_OFFSET, 1)
                action = "force_close"
            else:
                intended = round(target, 1)
                action = "track_target"

            # Skip if not online
            if not trv.get("online"):
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "offline",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Skip if already close enough
            if current_sp is not None and abs(current_sp - intended) < MIN_SETPOINT_DELTA:
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "already_set",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Cooldown check
            last = self._last_command.get(ieee)
            now = time.time()
            if last and last[0] == "temperature" and last[1] == intended \
                    and (now - last[2]) < COMMAND_COOLDOWN_SEC:
                actions.append({
                    "ieee": ieee, "action": "skip", "reason": "cooldown",
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            # Send command
            if self.dry_run:
                logger.info(
                    f"[DRY-RUN] Would set TRV {trv['name']} ({ieee}) → "
                    f"{intended}°C ({action}, room {decision.status})"
                )
                self._last_command[ieee] = ("temperature", intended, now)
                actions.append({
                    "ieee": ieee, "action": action, "sent": True, "dry_run": True,
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
                continue

            try:
                await self._send_command(ieee, "temperature", intended)
                self._last_command[ieee] = ("temperature", intended, now)
                logger.info(
                    f"TRV {trv['name']} ({ieee}) → {intended}°C "
                    f"({action}, room {decision.status})"
                )
                actions.append({
                    "ieee": ieee, "action": action, "sent": True,
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })
            except Exception as e:
                logger.error(f"TRV command failed for {ieee}: {e}")
                actions.append({
                    "ieee": ieee, "action": action, "sent": False, "error": str(e),
                    "target_setpoint": intended, "current_setpoint": current_sp,
                })

        return actions