"""
Heating Controller — Active control of receivers and TRVs.
==========================================================
Sits alongside HeatingAdvisor (which is read-only/analytical).
The Controller actually sends commands to make heating happen.

Model:
    Circuit (a receiver/zone valve calling for boiler heat)
      └── Room (a heated space with target temp)
            ├── Room sensor (optional — external thermostat/temp sensor)
            └── TRV(s) (regulate flow into that room's radiators)

Per-tick decision flow:
    1. Snapshot device states
    2. For each room:
         a. Pick the room temperature source:
              - temperature_sensor_ieee if present & online  → authoritative
              - otherwise, mean of TRV local_temperature
         b. Classify: COLD / ONTARGET / HOT (with hysteresis)
    3. Decide each circuit: CALLING (any room cold) / IDLE (all rooms ok)
    4. Decide each TRV's setpoint:
         - room COLD     → setpoint = target         (open via own thermostat)
         - room HOT      → setpoint = current - 1.0  (force close, prevent stealing)
         - room ONTARGET → setpoint = target         (idle)
    5. Apply receiver state changes (only if differ from last command)
    6. Apply TRV setpoint changes (only if differ from last command + larger than 0.5°C)
    7. (Background) Push external temp to Aqara TRVs if external_temp_mode=='push'

External sensor modes (per-room, config.external_temp_mode):
    - "off"      : TRV local temps decide everything (legacy behaviour)
    - "advisory" : controller uses external sensor for its own classification,
                   but TRVs continue using their own internal sensor. This is
                   the safe default when an external sensor is configured —
                   it immediately fixes "TRV reads hot pipe, not air".
    - "push"     : advisory + controller writes the external temperature into
                   each Aqara TRV's manufacturer cluster (0xFCC0, attr 0x0280)
                   and flips sensor_type to external. Requires Aqara TRV.

Per-TRV config (applied on start and via API):
    - window_detection : bool  → Aqara 0xFCC0 attr 0x0273
    - child_lock       : bool  → Aqara 0xFCC0 attr 0x0277
    - valve_detection  : bool  → Aqara 0xFCC0 attr 0x0274
  (motor_calibration is one-shot via API, not persisted as "always on")

Config (config.yaml under heating):
  heating:
    controller:
      enabled: true
      dry_run: false
    circuits:
      - id: downstairs
        name: "Downstairs"
        receiver_ieee: "00:15:8d:00:00:aa:bb:cc"
        receiver_command: thermostat      # 'thermostat' or 'switch'
        receiver_endpoint: 1
        rooms:
          - id: living
            name: "Living"
            target_temp: 20.5
            night_setback: 17.0
            min_temp: 16.0
            temperature_sensor_ieee: "00:1e:5e:09:02:a3:e4:c1"
            external_temp_mode: advisory          # off | advisory | push
            external_temp_push_interval_sec: 300
            # Legacy: trv_ieees: ["54:ef:44:..."]     (still supported)
            trvs:
              - ieee: "54:ef:44:10:00:67:3e:a6"
                window_detection: true
                child_lock: false
                valve_detection: true
            schedule:
              - days: [mon,tue,wed,thu,fri]
                start: "07:00"
                end:   "22:00"
                temp:  20.5
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

# Default external-temp push cadence when mode='push' and room doesn't override
DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC = 300

# Min delta before we re-push external temp to a TRV (°C). Saves battery airtime.
EXT_TEMP_PUSH_MIN_DELTA = 0.3

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── Helpers ────────────────────────────────────────────────────────
def _as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(v, default: Optional[bool] = None) -> Optional[bool]:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled", "lock"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled", "unlock"):
            return False
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


def _pick_temperature(state: Dict[str, Any]) -> Optional[float]:
    """
    Pull a temperature reading from a device's state, preferring the most
    'room-like' key. Works for thermostats, TRVs and bare temperature sensors.
    """
    for key in ("local_temperature", "current_temperature", "temperature"):
        v = state.get(key)
        f = _as_float(v)
        if f is not None and f != 0:   # filter out the init-zero we sometimes see
            return f
    return None


# ── Room state ─────────────────────────────────────────────────────
class RoomDecision:
    """Per-tick analysis of a single room."""

    __slots__ = (
        "room_id", "name", "target_temp", "current_temp", "temp_source",
        "status", "calling_for_heat", "trvs", "sensor_ieee", "sensor_online",
    )

    def __init__(self, room_id: str, name: str):
        self.room_id = room_id
        self.name = name
        self.target_temp: Optional[float] = None
        self.current_temp: Optional[float] = None
        self.temp_source: str = "none"   # "external" | "trv_mean" | "none"
        self.sensor_ieee: Optional[str] = None
        self.sensor_online: Optional[bool] = None
        self.status: str = "unknown"     # cold | ontarget | hot | unknown
        self.calling_for_heat: bool = False
        self.trvs: List[Dict] = []

    def to_dict(self) -> Dict:
        return {
            "room_id": self.room_id,
            "name": self.name,
            "target_temp": self.target_temp,
            "current_temp": self.current_temp,
            "temp_source": self.temp_source,
            "sensor_ieee": self.sensor_ieee,
            "sensor_online": self.sensor_online,
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

        # Last external-temp push tracking:  trv_ieee -> (last_pushed_c, ts)
        self._last_ext_push: Dict[str, Tuple[float, float]] = {}

        # Last decision snapshot (for dashboard/API)
        self._last_decision: Dict[str, Any] = {}
        self._last_decision_ts: float = 0

        # Applied-on-start flags so we don't spam configuration writes every tick
        self._trv_config_applied: set = set()

        self._task: Optional[asyncio.Task] = None
        self._ext_push_task: Optional[asyncio.Task] = None

        if self.enabled:
            mode = "DRY-RUN" if self.dry_run else "LIVE"
            n_rooms = sum(len(c["rooms"]) for c in self.circuits)
            n_trvs = sum(len(r["trvs"]) for c in self.circuits for r in c["rooms"])
            n_sensors = sum(
                1 for c in self.circuits for r in c["rooms"]
                if r.get("temperature_sensor_ieee")
            )
            n_push = sum(
                1 for c in self.circuits for r in c["rooms"]
                if r.get("external_temp_mode") == "push"
            )
            logger.info(
                f"Heating Controller [{mode}]: "
                f"{len(self.circuits)} circuits, {n_rooms} rooms, {n_trvs} TRVs, "
                f"{n_sensors} room sensors, {n_push} rooms pushing ext-temp"
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

            # Parse TRVs from either the new 'trvs' list (dicts) or legacy 'trv_ieees' list (strings).
            trvs = self._clean_trvs(r)

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

            sensor_ieee = r.get("temperature_sensor_ieee")
            sensor_ieee = str(sensor_ieee).strip() if sensor_ieee else None
            sensor_ieee = sensor_ieee or None

            mode = str(r.get("external_temp_mode", "advisory" if sensor_ieee else "off")).lower()
            if mode not in ("off", "advisory", "push"):
                mode = "advisory" if sensor_ieee else "off"
            # Push without a sensor is nonsensical — coerce to off.
            if not sensor_ieee and mode == "push":
                logger.warning(
                    f"Room {rid}: external_temp_mode='push' but no temperature_sensor_ieee — treating as 'off'"
                )
                mode = "off"

            push_interval = int(_as_float(
                r.get("external_temp_push_interval_sec"),
                DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC
            ) or DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)

            out.append({
                "id": rid,
                "name": str(r["name"]),
                "target_temp": _as_float(r.get("target_temp"), self._default_target),
                "night_setback": _as_float(r.get("night_setback"), self._default_setback),
                "min_temp": _as_float(r.get("min_temp"), self._default_min),
                "temperature_sensor_ieee": sensor_ieee,
                "external_temp_mode": mode,
                "external_temp_push_interval_sec": push_interval,
                "trvs": trvs,
                # Keep legacy key populated so older code paths still work.
                "trv_ieees": [t["ieee"] for t in trvs],
                "schedule": clean_sched,
            })
        return out

    def _clean_trvs(self, room: dict) -> List[Dict]:
        """
        Accept both shapes:
            trvs: [{ieee, window_detection?, child_lock?, valve_detection?}, ...]
            trv_ieees: ["aa:bb:...", ...]
        Later-listed IEEE in either collection wins on conflict (dict form preferred).
        """
        by_ieee: Dict[str, Dict[str, Any]] = {}

        # Legacy list first, so explicit dicts override.
        legacy = room.get("trv_ieees") or []
        if isinstance(legacy, list):
            for ieee in legacy:
                if not ieee:
                    continue
                ieee_s = str(ieee).strip()
                if not ieee_s:
                    continue
                by_ieee[ieee_s] = {
                    "ieee": ieee_s,
                    "window_detection": None,
                    "child_lock": None,
                    "valve_detection": None,
                }

        new = room.get("trvs") or []
        if isinstance(new, list):
            for t in new:
                if isinstance(t, str):
                    ieee_s = t.strip()
                    if ieee_s:
                        by_ieee.setdefault(ieee_s, {
                            "ieee": ieee_s,
                            "window_detection": None,
                            "child_lock": None,
                            "valve_detection": None,
                        })
                elif isinstance(t, dict):
                    ieee_s = str(t.get("ieee") or "").strip()
                    if not ieee_s:
                        continue
                    by_ieee[ieee_s] = {
                        "ieee": ieee_s,
                        "window_detection": _as_bool(t.get("window_detection"), None),
                        "child_lock": _as_bool(t.get("child_lock"), None),
                        "valve_detection": _as_bool(t.get("valve_detection"), None),
                    }

        return list(by_ieee.values())

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self):
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._control_loop())
        self._ext_push_task = asyncio.create_task(self._ext_push_loop())
        logger.info("Heating Controller started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ext_push_task:
            self._ext_push_task.cancel()
            self._ext_push_task = None
        logger.info("Heating Controller stopped")

    async def _control_loop(self):
        # Initial delay so other services finish startup
        await asyncio.sleep(15)
        # Apply persistent per-TRV config once devices are online
        try:
            await self._apply_all_trv_config()
        except Exception as e:
            logger.error(f"Initial TRV config apply failed: {e}", exc_info=True)

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Controller tick failed: {e}", exc_info=True)
            await asyncio.sleep(TICK_INTERVAL_SEC)

    async def _ext_push_loop(self):
        """Background task: push external temperature into Aqara TRVs."""
        await asyncio.sleep(30)  # settle
        while True:
            try:
                await self._push_external_temps_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"External temp push failed: {e}", exc_info=True)
            # Use shortest configured push interval as loop cadence, clamped
            intervals = [
                r.get("external_temp_push_interval_sec", DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)
                for c in self.circuits for r in c["rooms"]
                if r.get("external_temp_mode") == "push"
            ]
            sleep_for = min(intervals) if intervals else DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC
            sleep_for = max(60, min(sleep_for, 1800))
            await asyncio.sleep(sleep_for)

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

    def find_trv(self, ieee: str) -> Optional[Tuple[Dict, Dict, Dict]]:
        """Locate a TRV in the config. Returns (circuit, room, trv) or None."""
        for c in self.circuits:
            for r in c["rooms"]:
                for t in r["trvs"]:
                    if t["ieee"] == ieee:
                        return c, r, t
        return None

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

            # Surface the receiver's live running_state / system_mode so the
            # controller panel can show whether the boiler is actually firing.
            recv_state = {}
            rx_ieee = circuit.get("receiver_ieee")
            if rx_ieee and rx_ieee in devices:
                rx_dev = devices[rx_ieee]
                rs_dict = (
                              rx_dev.get("state") if isinstance(rx_dev, dict)
                              else getattr(rx_dev, "state", None)
                          ) or {}
                rs = rs_dict.get("running_state")
                rs_on = False
                if isinstance(rs, (int, float)):
                    rs_on = bool(int(rs) & 0x0001)
                elif isinstance(rs, str) and "heat" in rs.lower():
                    rs_on = True
                recv_state = {
                    "running_state": rs,
                    "running": rs_on,
                    "system_mode": rs_dict.get("system_mode"),
                    "setpoint": rs_dict.get("occupied_heating_setpoint"),
                }

            circuits_out.append({
                "id": circuit["id"],
                "name": circuit["name"],
                "calling_for_heat": should_call,
                "receiver_ieee": circuit["receiver_ieee"],
                "receiver_action": receiver_action,
                "receiver_state": recv_state,
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

        # Gather TRV state
        trv_temps = []
        trvs = []
        for t in room["trvs"]:
            ieee = t["ieee"]
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
            temp = _pick_temperature(state)
            setpoint = state.get("occupied_heating_setpoint") or state.get("target_temp")
            if temp is not None:
                trv_temps.append(temp)
            trvs.append({
                "ieee": ieee,
                "name": _device_friendly_name(dev, ieee),
                "current_temp": temp,
                "current_setpoint": _as_float(setpoint),
                "online": True,
            })

        decision.trvs = trvs

        # Pick room temperature: external sensor wins if present & reading.
        sensor_ieee = room.get("temperature_sensor_ieee")
        ext_mode = room.get("external_temp_mode", "off")
        decision.sensor_ieee = sensor_ieee

        ext_temp: Optional[float] = None
        if sensor_ieee and ext_mode != "off":
            sensor_dev = devices.get(sensor_ieee)
            if sensor_dev is not None:
                ext_temp = _pick_temperature(_device_state(sensor_dev))
                decision.sensor_online = ext_temp is not None
            else:
                decision.sensor_online = False

        if ext_temp is not None:
            decision.current_temp = round(ext_temp, 1)
            decision.temp_source = "external"
        elif trv_temps:
            decision.current_temp = round(sum(trv_temps) / len(trv_temps), 1)
            decision.temp_source = "trv_mean"
        else:
            decision.current_temp = None
            decision.temp_source = "none"

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
        Control the receiver based on circuit config.

        receiver_command modes (from config):
          - "thermostat" : sends system_mode heat/off via 0x0201 handler
          - "switch"     : sends on/off via OnOff cluster (relay-type receivers)

        Idempotent — only sends if command differs from last sent.
        """
        ieee = circuit.get("receiver_ieee")
        if not ieee:
            return {"sent": False, "reason": "no receiver configured"}

        mode = str(circuit.get("receiver_command", "thermostat")).lower()

        if mode == "thermostat":
            target_command = "system_mode"
            target_value = "heat" if should_call else "off"
            display = f"system_mode → {target_value}"
            # When calling for heat, also push a high setpoint to guarantee
            # the receiver's internal comparator fires the boiler. When
            # standing down, push a low one so the receiver doesn't fight us.
            # Config override: circuit.receiver_call_setpoint / _idle_setpoint
            call_sp = float(circuit.get("receiver_call_setpoint", 30.0))
            idle_sp = float(circuit.get("receiver_idle_setpoint", 7.0))
            target_setpoint = call_sp if should_call else idle_sp
        else:
            target_command = "on" if should_call else "off"
            target_value = None
            display = target_command

        last = self._last_command.get(ieee)
        if last and last[0] == target_command and last[1] == target_value:
            return {"sent": False, "reason": "unchanged", "command": display}

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would send receiver '{circuit['name']}' ({ieee}) → {display}")
            self._last_command[ieee] = (target_command, target_value, time.time())
            return {"sent": True, "command": display, "dry_run": True}

        try:
            # 1) Push setpoint first (only in thermostat mode)
            if mode == "thermostat":
                last_sp = self._last_command.get(f"{ieee}:setpoint")
                if not last_sp or last_sp[0] != target_setpoint:
                    try:
                        await self._send_command(
                            ieee, "temperature", target_setpoint,
                            endpoint_id=circuit.get("receiver_endpoint"),
                        )
                    except TypeError:
                        await self._send_command(ieee, "temperature", target_setpoint)
                    self._last_command[f"{ieee}:setpoint"] = (target_setpoint, time.time())
                    logger.info(
                        f"Receiver '{circuit['name']}' setpoint → {target_setpoint}°C"
                    )
            # 2) Then push mode / on-off
            await self._send_command(ieee, target_command, target_value,
                                     endpoint_id=circuit.get("receiver_endpoint"))
            self._last_command[ieee] = (target_command, target_value, time.time())
            logger.info(f"Receiver '{circuit['name']}' ({ieee}) → {display}")
            return {
                "sent": True,
                "command": display,
                "setpoint": target_setpoint if mode == "thermostat" else None,
            }
        except TypeError:
            try:
                await self._send_command(ieee, target_command, target_value)
                self._last_command[ieee] = (target_command, target_value, time.time())
                return {"sent": True, "command": display}
            except Exception as e:
                logger.error(f"Receiver command failed ({display}): {e}")
                return {"sent": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Receiver command failed ({display}): {e}")
            return {"sent": False, "error": str(e)}

    # ── TRV setpoint control ───────────────────────────────────────
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

        # When forcing closed we use the *room* temperature (external if present);
        # that's more defensible than the TRV's own hot-pipe reading.
        room_temp = decision.current_temp

        for trv in decision.trvs:
            ieee = trv["ieee"]
            current_temp = trv.get("current_temp")
            current_sp = trv.get("current_setpoint")

            # Decide intended setpoint
            if decision.status == "hot" and circuit_calling:
                reference = room_temp if room_temp is not None else current_temp
                if reference is None:
                    intended = round(target, 1)
                    action = "track_target"
                else:
                    intended = round(reference - FORCE_CLOSE_OFFSET, 1)
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
                    f"{intended}°C ({action}, room {decision.status}, "
                    f"src={decision.temp_source})"
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
                    f"({action}, room {decision.status}, src={decision.temp_source})"
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

    # ── Per-TRV persistent config application ──────────────────────
    async def _apply_all_trv_config(self):
        """Apply window_detection / child_lock / valve_detection for every configured TRV."""
        for c in self.circuits:
            for r in c["rooms"]:
                for t in r["trvs"]:
                    if t["ieee"] in self._trv_config_applied:
                        continue
                    await self.apply_trv_config(t["ieee"])

    async def apply_trv_config(self, ieee: str) -> Dict[str, Any]:
        """
        Apply persistent Aqara-cluster settings for a single TRV.
        Safe to call repeatedly; returns a report of what was sent.
        """
        loc = self.find_trv(ieee)
        if not loc:
            return {"success": False, "error": "TRV not in controller config"}
        _, room, trv = loc

        results: Dict[str, Any] = {"ieee": ieee, "sent": {}, "skipped": {}, "failed": {}}

        for attr, command in (
                ("window_detection", "window_detection"),
                ("child_lock",       "child_lock"),
                ("valve_detection",  "valve_detection"),
        ):
            val = trv.get(attr)
            if val is None:
                results["skipped"][attr] = "not configured"
                continue
            if self.dry_run:
                logger.info(f"[DRY-RUN] Would set TRV {ieee} {attr} = {val}")
                results["sent"][attr] = {"value": bool(val), "dry_run": True}
                continue
            try:
                await self._send_command(ieee, command, 1 if val else 0)
                results["sent"][attr] = {"value": bool(val)}
            except Exception as e:
                logger.error(f"TRV {ieee} {attr} write failed: {e}")
                results["failed"][attr] = str(e)

        # If the room is in push mode, tell the TRV to honour the external feed.
        if room.get("external_temp_mode") == "push":
            if self.dry_run:
                logger.info(f"[DRY-RUN] Would set TRV {ieee} sensor_type = external")
                results["sent"]["sensor_type"] = {"value": "external", "dry_run": True}
            else:
                try:
                    await self._send_command(ieee, "sensor_type", 1)  # 1 = external
                    results["sent"]["sensor_type"] = {"value": "external"}
                except Exception as e:
                    logger.error(f"TRV {ieee} sensor_type write failed: {e}")
                    results["failed"]["sensor_type"] = str(e)

        self._trv_config_applied.add(ieee)
        results["success"] = not results["failed"]
        return results

    async def trigger_calibration(self, ieee: str) -> Dict[str, Any]:
        """
        One-shot: kick off motor calibration on an Aqara TRV.
        Takes ~2 minutes on the device side — status lives in state['motor_calibration'].
        """
        if not self.find_trv(ieee):
            return {"success": False, "error": "TRV not in controller config"}
        if self.dry_run:
            logger.info(f"[DRY-RUN] Would start calibration on TRV {ieee}")
            return {"success": True, "dry_run": True, "ieee": ieee}
        try:
            await self._send_command(ieee, "motor_calibration", 1)
            logger.info(f"TRV {ieee}: motor calibration started")
            return {"success": True, "ieee": ieee}
        except Exception as e:
            logger.error(f"TRV {ieee} calibration failed: {e}")
            return {"success": False, "error": str(e)}

    def update_trv_settings(self, ieee: str, updates: Dict[str, Any]) -> bool:
        """
        In-memory update of a single TRV's config. Persistence is the caller's job
        (routes save to config.yaml). Returns True if TRV found and updated.
        """
        loc = self.find_trv(ieee)
        if not loc:
            return False
        _, _, trv = loc
        for k in ("window_detection", "child_lock", "valve_detection"):
            if k in updates:
                trv[k] = _as_bool(updates[k], trv.get(k))
        # Force re-apply on next sweep
        self._trv_config_applied.discard(ieee)
        return True

    # ── External temperature push ──────────────────────────────────
    async def _push_external_temps_once(self):
        """
        For every room in push mode, read the sensor's current temperature and
        forward it to each Aqara TRV in the room. Skips pushes that are
        redundant (same reading within EXT_TEMP_PUSH_MIN_DELTA of last push).
        """
        devices = self._snapshot_devices()
        now_ts = time.time()

        for c in self.circuits:
            for room in c["rooms"]:
                if room.get("external_temp_mode") != "push":
                    continue
                sensor_ieee = room.get("temperature_sensor_ieee")
                if not sensor_ieee:
                    continue
                sensor_dev = devices.get(sensor_ieee)
                if sensor_dev is None:
                    logger.debug(f"Room {room['id']}: sensor {sensor_ieee} not in device registry")
                    continue
                sensor_temp = _pick_temperature(_device_state(sensor_dev))
                if sensor_temp is None:
                    logger.debug(f"Room {room['id']}: sensor {sensor_ieee} has no temperature")
                    continue

                interval = room.get("external_temp_push_interval_sec",
                                    DEFAULT_EXT_TEMP_PUSH_INTERVAL_SEC)

                for trv in room["trvs"]:
                    ieee = trv["ieee"]
                    last = self._last_ext_push.get(ieee)
                    if last is not None:
                        last_temp, last_ts = last
                        fresh_enough = (now_ts - last_ts) < interval
                        tiny_change = abs(sensor_temp - last_temp) < EXT_TEMP_PUSH_MIN_DELTA
                        if fresh_enough and tiny_change:
                            continue

                    if self.dry_run:
                        logger.info(
                            f"[DRY-RUN] Would push external temp {sensor_temp:.2f}°C "
                            f"→ TRV {ieee} (room {room['id']})"
                        )
                        self._last_ext_push[ieee] = (sensor_temp, now_ts)
                        continue

                    try:
                        await self._send_command(ieee, "external_temp", sensor_temp)
                        self._last_ext_push[ieee] = (sensor_temp, now_ts)
                        logger.info(
                            f"TRV {ieee}: pushed external temp {sensor_temp:.2f}°C "
                            f"(room {room['id']})"
                        )
                    except Exception as e:
                        logger.warning(f"TRV {ieee}: external temp push failed: {e}")