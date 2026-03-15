"""
Automation Engine - State Machine with Recursive Action Sequences
=================================================================
Evaluates device state changes and fires action sequences on transitions.

Step types (recursive):
  command      - Send ZigBee command to a device
  delay        - Wait N seconds
  wait_for     - Pause until device state matches (with timeout)
  condition    - Inline gate: stop sequence if false
  if_then_else - Branch: check inline conditions, run then_steps or else_steps
  parallel     - Run multiple step branches simultaneously

Condition features:
  - AND / OR logic for prerequisites
  - NOT (negate) flag on any prerequisite
  - Inline conditions in if_then_else support AND/OR/NOT
  - Duration checks ("for" N seconds on inline conditions)

Persistence: ./data/automations.json
Hook:        core.py -> _debounced_device_update
Execution:   device.send_command() (direct zigpy)
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.automation")

MAX_RULES_PER_DEVICE = 10
MAX_CONDITIONS_PER_RULE = 5
MAX_PREREQUISITES_PER_RULE = 8
MAX_STEPS_PER_SEQUENCE = 15
MAX_NESTING_DEPTH = 4
DATA_FILE = "./data/automations.json"
DEFAULT_COOLDOWN = 5
WAIT_FOR_POLL_INTERVAL = 2

OPERATORS = {
    "eq":  lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt":  lambda a, b: float(a) > float(b),
    "lt":  lambda a, b: float(a) < float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "lte": lambda a, b: float(a) <= float(b),
    "in":  lambda a, b: True,  # handled specially in _evaluate_condition
    "nin": lambda a, b: True,  # handled specially in _evaluate_condition
}

VALID_COMMANDS = {
    "on", "off", "toggle", "brightness", "color_temp",
    "open", "close", "stop", "position", "temperature"
}

FLAT_STEP_TYPES = {"command", "delay", "wait_for", "condition"}
BRANCHING_STEP_TYPES = {"if_then_else", "parallel"}
ALL_STEP_TYPES = FLAT_STEP_TYPES | BRANCHING_STEP_TYPES


class AutomationEngine:

    def __init__(self, device_registry_getter: Callable[[], Dict],
                 friendly_names_getter: Callable[[], Dict],
                 event_emitter: Optional[Callable] = None,
                 group_manager_getter: Optional[Callable] = None):
        self._get_devices = device_registry_getter
        self._get_names = friendly_names_getter
        self._event_emitter = event_emitter
        self._get_group_manager = group_manager_getter

        self.rules: List[Dict[str, Any]] = []
        self._source_index: Dict[str, List[str]] = {}
        self._cooldowns: Dict[str, float] = {}
        self._sustain_tracker: Dict[str, float] = {}
        self._rule_states: Dict[str, Optional[str]] = {}
        self._running_sequences: Dict[str, asyncio.Task] = {}
        self._time_scheduler_task: Optional[asyncio.Task] = None

        self._trace_log: List[Dict[str, Any]] = []
        self._max_trace_entries = 200

        self._stats = {
            "evaluations": 0, "matches": 0, "transitions": 0,
            "executions": 0, "execution_successes": 0,
            "execution_failures": 0, "errors": 0,
        }

        self._load_rules()
        logger.info(f"Automation engine initialised with {len(self.rules)} rule(s)")


        # =========================================================================
        # TIME SCHEDULER
        # =========================================================================

        async def start(self):
            """Start background time-boundary scheduler and set initial rule states."""
            self._time_scheduler_task = asyncio.create_task(self._time_boundary_loop())
            logger.info("Automation time scheduler started")

        async def stop(self):
            """Stop background tasks."""
            if self._time_scheduler_task:
                self._time_scheduler_task.cancel()
                try:
                    await self._time_scheduler_task
                except asyncio.CancelledError:
                    pass

        async def _time_boundary_loop(self):
            """
            Runs every 30s. At each time-window boundary (time_from / time_to)
            re-evaluates all rules that contain time_window conditions so they
            fire at the correct clock time rather than waiting for the next
            incidental device update.
            """
            import datetime
            last_minute_checked = None

            # Evaluate on startup so initial state is set correctly
            await asyncio.sleep(2)  # Brief delay to let devices load
            await self._evaluate_timed_rules()

            while True:
                try:
                    await asyncio.sleep(30)
                    now_dt = datetime.datetime.now()
                    now_hhmm = now_dt.strftime("%H:%M")

                    if now_hhmm == last_minute_checked:
                        continue
                    last_minute_checked = now_hhmm

                    # Collect all boundary times across all enabled rules
                    boundaries: set = set()
                    for rule in self.rules:
                        if not rule.get("enabled", True):
                            continue
                        for c in rule.get("conditions", []):
                            if c.get("type") == "time_window":
                                boundaries.add(c.get("time_from"))
                                boundaries.add(c.get("time_to"))
                        for p in rule.get("prerequisites", []):
                            if p.get("type") == "time_window":
                                boundaries.add(p.get("time_from"))
                                boundaries.add(p.get("time_to"))

                    if now_hhmm in boundaries:
                        logger.info(f"[AUTO] Time boundary hit {now_hhmm} — evaluating timed rules")
                        await self._evaluate_timed_rules()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[AUTO] Time boundary loop error: {e}")

        async def _evaluate_timed_rules(self):
            """
            Evaluate all enabled rules that have at least one time_window condition.
            Uses empty changed_data since time_window conditions don't need attribute data.
            Runs the same state-machine transition logic as evaluate().
            """
            now = time.time()
            devices = self._get_devices()
            names = self._get_names()

            for rule in self.rules:
                if not rule.get("enabled", True):
                    continue

                has_tw_cond = any(
                    c.get("type") == "time_window" for c in rule.get("conditions", [])
                )
                has_tw_prereq = any(
                    p.get("type") == "time_window" for p in rule.get("prerequisites", [])
                )
                if not (has_tw_cond or has_tw_prereq):
                    continue

                rule_id = rule["id"]
                rule_name = rule.get("name") or rule_id
                source_ieee = rule.get("source_ieee", "")
                source_device = devices.get(source_ieee)
                full_state = source_device.state if source_device else {}

                # Evaluate with empty changed_data — time_window conditions don't need it
                all_matched, cond_results, has_sustain = self._eval_conditions_block(
                    rule.get("conditions", []), rule_id, {}, full_state, now)

                if has_sustain:
                    continue

                prereq_results = []
                prereqs_met = True
                if all_matched:
                    prereqs = rule.get("prerequisites", [])
                    prereqs_met, prereq_results = self._eval_prerequisites(prereqs, devices, names)

                conditions_met = all_matched and prereqs_met
                new_state = "matched" if conditions_met else "unmatched"
                prev_state = self._rule_states.get(rule_id)

                if not all_matched:
                    self._trace(rule_id, "evaluate", "NO_MATCH",
                                f"Conditions not met: {rule_name}",
                                level="DEBUG", conditions=cond_results)
                elif not prereqs_met:
                    self._trace(rule_id, "prerequisite", "PREREQ_FAIL",
                                f"Prerequisites not met: {rule_name}",
                                conditions=cond_results, prerequisites=prereq_results)

                self._rule_states[rule_id] = new_state

                if prev_state == new_state:
                    continue
                if prev_state is None and new_state == "unmatched":
                    continue

                # Cooldown check
                cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
                elapsed = now - self._cooldowns.get(rule_id, 0)
                if elapsed < cooldown:
                    self._trace(rule_id, "cooldown", "BLOCKED",
                                f"Cooldown {elapsed:.1f}s < {cooldown}s")
                    continue

                self._cooldowns[rule_id] = now
                self._stats["transitions"] += 1

                path = "THEN" if new_state == "matched" else "ELSE"
                seq = rule.get("then_sequence" if path == "THEN" else "else_sequence", [])
                if not seq:
                    self._trace(rule_id, "transition", "NO_SEQUENCE",
                                f"Transition → {new_state}, no {path} sequence: {rule_name}")
                    continue

                self._trace(rule_id, "transition", f"{path}_FIRING",
                            f"⚡ {prev_state or 'init'}→{new_state}: {path} ({len(seq)} steps) — {rule_name}",
                            conditions=cond_results, prerequisites=prereq_results)

                self._cancel_sequence(rule_id)
                task = asyncio.create_task(self._run_sequence(rule_id, rule_name, seq, path))
                self._running_sequences[rule_id] = task

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _load_rules(self):
        if not os.path.exists(DATA_FILE):
            self.rules = []
            self._rebuild_index()
            return
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            self.rules = data.get("rules", [])
            migrated = self._migrate_rules()
            if migrated:
                self._save_rules()
            self._rebuild_index()
            logger.info(f"Loaded {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self.rules = []
            self._rebuild_index()

    def _migrate_rules(self) -> int:
        count = 0
        for rule in self.rules:
            if "name" not in rule:
                rule["name"] = ""
            if "threshold" in rule and "conditions" not in rule:
                rule["conditions"] = [rule.pop("threshold")]
                count += 1
            if "action" in rule and "then_sequence" not in rule:
                action = rule.pop("action")
                target = rule.pop("target_ieee", "")
                steps = []
                delay = action.get("delay", 0) or 0
                if delay > 0:
                    steps.append({"type": "delay", "seconds": delay})
                steps.append({
                    "type": "command",
                    "target_ieee": target,
                    "command": action.get("command", "on"),
                    "value": action.get("value"),
                    "endpoint_id": action.get("endpoint_id"),
                })
                rule["then_sequence"] = steps
                count += 1
            for key in ("then_sequence", "else_sequence", "prerequisites"):
                if key not in rule:
                    rule[key] = []
        return count

    def _save_rules(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({"rules": self.rules}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save automations: {e}")

    def _rebuild_index(self):
        self._source_index.clear()
        for rule in self.rules:
            src = rule.get("source_ieee")
            if src:
                self._source_index.setdefault(src, []).append(rule["id"])

    # =========================================================================
    # TRACING
    # =========================================================================

    def _trace(self, rule_id, phase, result, message, level="INFO", **extra):
        entry = {
            "timestamp": time.time(), "rule_id": rule_id,
            "phase": phase, "result": result, "message": message,
            "level": level, **extra,
        }
        self._trace_log.append(entry)
        if len(self._trace_log) > self._max_trace_entries:
            self._trace_log = self._trace_log[-self._max_trace_entries:]

        log_msg = f"[AUTO {rule_id}] {message}"
        if level == "ERROR": logger.error(log_msg)
        elif level == "WARNING": logger.warning(log_msg)
        elif level == "INFO": logger.info(log_msg)
        else: logger.debug(log_msg)

        if self._event_emitter:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_emitter("automation_trace", entry))
            except RuntimeError:
                pass

    def get_trace_log(self) -> List[Dict[str, Any]]:
        return list(self._trace_log)

    # =========================================================================
    # VALIDATION (recursive)
    # =========================================================================

    def _validate_conditions(self, conds: List[Dict]) -> Optional[str]:
        import re
        if not isinstance(conds, list) or not conds:
            return "conditions must be a non-empty list"
        if len(conds) > MAX_CONDITIONS_PER_RULE:
            return f"Max {MAX_CONDITIONS_PER_RULE} conditions"
        for i, c in enumerate(conds):
            ctype = c.get("type", "attribute")
            if ctype == "time_window":
                for f in ("time_from", "time_to"):
                    if f not in c:
                        return f"Condition {i+1} (time_window) missing '{f}'"
                    if not re.match(r"^\d{2}:\d{2}$", str(c[f])):
                        return f"Condition {i+1} '{f}' must be HH:MM"
            else:
                for f in ("attribute", "operator", "value"):
                    if f not in c:
                        return f"Condition {i+1} missing '{f}'"
                if c["operator"] not in OPERATORS:
                    return f"Condition {i+1} invalid operator"
                s = c.get("sustain")
                if s:
                    try:
                        s = int(s)
                        c["sustain"] = s if s > 0 else None
                    except (ValueError, TypeError):
                        c["sustain"] = None
                if not c.get("sustain"):
                    c.pop("sustain", None)
        return None

    def _validate_prerequisites(self, prereqs: List[Dict]) -> Optional[str]:
        import re
        if len(prereqs) > MAX_PREREQUISITES_PER_RULE:
            return f"Max {MAX_PREREQUISITES_PER_RULE} prerequisites"
        for i, p in enumerate(prereqs):
            ptype = p.get("type", "device")
            if ptype == "time_window":
                for f in ("time_from", "time_to"):
                    if f not in p:
                        return f"Prerequisite {i+1} (time_window) missing '{f}'"
                    if not re.match(r"^\d{2}:\d{2}$", str(p[f])):
                        return f"Prerequisite {i+1} '{f}' must be HH:MM"
            else:
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in p:
                        return f"Prerequisite {i+1} missing '{f}'"
                if p["operator"] not in OPERATORS:
                    return f"Prerequisite {i+1} invalid operator"
        return None

    def _validate_sequence(self, steps: List[Dict], label: str, depth: int = 0) -> Optional[str]:
        if depth > MAX_NESTING_DEPTH:
            return f"{label}: max nesting depth {MAX_NESTING_DEPTH} exceeded"
        if len(steps) > MAX_STEPS_PER_SEQUENCE:
            return f"{label}: max {MAX_STEPS_PER_SEQUENCE} steps"

        for i, step in enumerate(steps):
            st = step.get("type")
            if st not in ALL_STEP_TYPES:
                return f"{label}[{i+1}]: invalid type '{st}'"

            if st == "command":
                if not step.get("target_ieee"):
                    return f"{label}[{i+1}]: command needs target_ieee"
                if step.get("command") not in VALID_COMMANDS:
                    return f"{label}[{i+1}]: invalid command"
            elif st == "delay":
                if not isinstance(step.get("seconds", 0), (int, float)) or step.get("seconds", 0) < 0:
                    return f"{label}[{i+1}]: delay needs positive seconds"
            elif st in ("wait_for", "condition"):
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in step:
                        return f"{label}[{i+1}]: {st} needs '{f}'"
            elif st == "if_then_else":
                inline = step.get("inline_conditions", [])
                if not inline:
                    return f"{label}[{i+1}]: if_then_else needs inline_conditions"
                for j, ic in enumerate(inline):
                    for f in ("ieee", "attribute", "operator", "value"):
                        if f not in ic:
                            return f"{label}[{i+1}] condition {j+1} missing '{f}'"
                err = self._validate_sequence(step.get("then_steps", []), f"{label}[{i+1}].then", depth + 1)
                if err: return err
                err = self._validate_sequence(step.get("else_steps", []), f"{label}[{i+1}].else", depth + 1)
                if err: return err
            elif st == "parallel":
                branches = step.get("branches", [])
                if len(branches) < 2:
                    return f"{label}[{i+1}]: parallel needs >= 2 branches"
                for bi, branch in enumerate(branches):
                    err = self._validate_sequence(branch, f"{label}[{i+1}].branch{bi+1}", depth + 1)
                    if err: return err
        return None

    # =========================================================================
    # RULE CRUD
    # =========================================================================

    def add_rule(self, data: Dict[str, Any]) -> Dict[str, Any]:
        conditions = data.get("conditions")
        if conditions:
            err = self._validate_conditions(conditions)
            if err: return {"success": False, "error": err}
        elif all(k in data for k in ("attribute", "operator", "value")):
            conditions = [{"attribute": data["attribute"],
                           "operator": data["operator"], "value": data["value"]}]
        else:
            return {"success": False, "error": "Provide conditions list"}

        prereqs = data.get("prerequisites", [])
        if prereqs:
            err = self._validate_prerequisites(prereqs)
            if err: return {"success": False, "error": err}

        then_seq = data.get("then_sequence", [])
        else_seq = data.get("else_sequence", [])
        if not then_seq and not else_seq:
            return {"success": False, "error": "At least one action step required"}
        err = self._validate_sequence(then_seq, "THEN")
        if err: return {"success": False, "error": err}
        err = self._validate_sequence(else_seq, "ELSE")
        if err: return {"success": False, "error": err}

        source = data.get("source_ieee")
        if not source:
            return {"success": False, "error": "source_ieee required"}
        if len(self._source_index.get(source, [])) >= MAX_RULES_PER_DEVICE:
            return {"success": False, "error": f"Max {MAX_RULES_PER_DEVICE} rules"}
        if source not in self._get_devices():
            return {"success": False, "error": f"Source not found: {source}"}

        rule = {
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "name": data.get("name", ""),
            "enabled": data.get("enabled", True),
            "source_ieee": source,
            "conditions": conditions,
            "prerequisites": prereqs,
            "then_sequence": then_seq,
            "else_sequence": else_seq,
            "cooldown": data.get("cooldown", DEFAULT_COOLDOWN),
            "created": time.time(),
        }
        self.rules.append(rule)
        self._rebuild_index()
        self._save_rules()
        logger.info(f"Rule added: {rule['id']} '{rule['name']}'")
        return {"success": True, "rule": rule}

    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Not found: {rule_id}"}

        if "name" in updates:
            rule["name"] = str(updates["name"])[:100]
        if "conditions" in updates:
            err = self._validate_conditions(updates["conditions"])
            if err: return {"success": False, "error": err}
            rule["conditions"] = updates["conditions"]
        if "prerequisites" in updates:
            p = updates["prerequisites"] or []
            if p:
                err = self._validate_prerequisites(p)
                if err: return {"success": False, "error": err}
            rule["prerequisites"] = p
        if "then_sequence" in updates:
            err = self._validate_sequence(updates["then_sequence"], "THEN")
            if err: return {"success": False, "error": err}
            rule["then_sequence"] = updates["then_sequence"]
        if "else_sequence" in updates:
            err = self._validate_sequence(updates["else_sequence"], "ELSE")
            if err: return {"success": False, "error": err}
            rule["else_sequence"] = updates["else_sequence"]
        if "enabled" in updates:
            rule["enabled"] = bool(updates["enabled"])
            if not rule["enabled"]:
                self._cancel_sequence(rule_id)
                self._rule_states.pop(rule_id, None)
        if "cooldown" in updates:
            rule["cooldown"] = max(0, int(updates["cooldown"]))

        rule["updated"] = time.time()
        self._rebuild_index()
        self._save_rules()
        return {"success": True, "rule": rule}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Not found: {rule_id}"}
        self._cancel_sequence(rule_id)
        self.rules.remove(rule)
        self._cooldowns.pop(rule_id, None)
        self._rule_states.pop(rule_id, None)
        for k in [k for k in self._sustain_tracker if k.startswith(rule_id)]:
            del self._sustain_tracker[k]
        self._rebuild_index()
        self._save_rules()
        return {"success": True}

    def get_rules(self, source_ieee: Optional[str] = None) -> List[Dict[str, Any]]:
        names = self._get_names()
        rules = self.rules if not source_ieee else [
            r for r in self.rules if r["source_ieee"] == source_ieee
        ]
        enriched = []
        for rule in rules:
            r = json.loads(json.dumps(rule))  # deep copy
            r["source_name"] = names.get(rule["source_ieee"], rule["source_ieee"])
            r["_state"] = self._rule_states.get(rule["id"], "unknown")
            r["_running"] = (rule["id"] in self._running_sequences and
                             not self._running_sequences[rule["id"]].done())
            self._enrich_names(r.get("prerequisites", []), names, "ieee", "device_name")
            self._enrich_steps(r.get("then_sequence", []), names)
            self._enrich_steps(r.get("else_sequence", []), names)
            enriched.append(r)
        return enriched

    def _enrich_names(self, items, names, ieee_key, name_key):
        for item in items:
            if item.get(ieee_key):
                item[name_key] = names.get(item[ieee_key], item[ieee_key])

    def _enrich_steps(self, steps, names):
        for step in steps:
            if step.get("target_ieee"):
                step["target_name"] = names.get(step["target_ieee"], step["target_ieee"])
            if step.get("ieee"):
                step["device_name"] = names.get(step["ieee"], step["ieee"])
            if step.get("inline_conditions"):
                for ic in step["inline_conditions"]:
                    if ic.get("ieee"):
                        ic["device_name"] = names.get(ic["ieee"], ic["ieee"])
            for sub in ("then_steps", "else_steps"):
                if step.get(sub):
                    self._enrich_steps(step[sub], names)
            if step.get("branches"):
                for branch in step["branches"]:
                    self._enrich_steps(branch, names)

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        return self._find_rule(rule_id)

    def _find_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        for r in self.rules:
            if r["id"] == rule_id:
                return r
        return None

    # =========================================================================
    # STATE MACHINE EVALUATION
    # =========================================================================

    async def evaluate(self, source_ieee: str, changed_data: Dict[str, Any]):
        rule_ids = self._source_index.get(source_ieee)
        if not rule_ids:
            return

        self._stats["evaluations"] += 1
        now = time.time()
        devices = self._get_devices()
        names = self._get_names()
        source_name = names.get(source_ieee, source_ieee)

        source_device = devices.get(source_ieee)
        if not source_device:
            return

        full_state = source_device.state or {}
        self._trace("-", "entry", "EVALUATING",
                    f"State change on {source_name}: {list(changed_data.keys())} — {len(rule_ids)} rule(s)",
                    level="DEBUG", source_ieee=source_ieee)

        for rule_id in rule_ids:
            rule = self._find_rule(rule_id)
            if not rule or not rule.get("enabled", True):
                continue

            conditions = rule.get("conditions", [])
            if not conditions:
                continue

            rule_name = rule.get("name") or rule_id

            # Relevance
            watched = {c["attribute"] for c in conditions if c.get("type", "attribute") != "time_window"}
            if watched and not watched.intersection(changed_data.keys()):
                continue

            # --- CONDITIONS ---
            all_matched, cond_results, has_sustain = self._eval_conditions_block(
                conditions, rule_id, changed_data, full_state, now)

            if has_sustain:
                self._trace(rule_id, "evaluate", "SUSTAIN_WAIT",
                            f"Sustain pending: {rule_name}",
                            conditions=cond_results)
                continue

            # --- PREREQUISITES ---
            prereq_results = []
            prereqs_met = True
            if all_matched:
                prereqs = rule.get("prerequisites", [])
                prereqs_met, prereq_results = self._eval_prerequisites(prereqs, devices, names)

            # --- DETERMINE STATE ---
            conditions_met = all_matched and prereqs_met
            new_state = "matched" if conditions_met else "unmatched"
            prev_state = self._rule_states.get(rule_id)

            if not all_matched:
                self._trace(rule_id, "evaluate", "NO_MATCH",
                            f"Conditions not met: {rule_name}",
                            level="DEBUG", conditions=cond_results)
            elif not prereqs_met:
                self._trace(rule_id, "prerequisite", "PREREQ_FAIL",
                            f"Prerequisites not met: {rule_name}",
                            conditions=cond_results, prerequisites=prereq_results)

            # --- TRANSITION ---
            self._rule_states[rule_id] = new_state

            if prev_state == new_state:
                if new_state == "matched":
                    self._trace(rule_id, "transition", "STILL_MATCHED",
                                f"No transition: {rule_name}", level="DEBUG")
                continue

            if prev_state is None and new_state == "unmatched":
                self._trace(rule_id, "transition", "INIT_UNMATCHED",
                            f"Initial: unmatched — {rule_name}", level="DEBUG")
                continue

            # Cooldown
            cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
            last = self._cooldowns.get(rule_id, 0)
            elapsed = now - last
            if elapsed < cooldown:
                self._trace(rule_id, "cooldown", "BLOCKED",
                            f"Cooldown {elapsed:.1f}s < {cooldown}s")
                continue

            self._cooldowns[rule_id] = now
            self._stats["transitions"] += 1
            for ci in range(len(conditions)):
                self._sustain_tracker.pop(f"{rule_id}_{ci}", None)

            # Fire sequence
            path = "THEN" if new_state == "matched" else "ELSE"
            seq = rule.get("then_sequence" if path == "THEN" else "else_sequence", [])
            if not seq:
                self._trace(rule_id, "transition", "NO_SEQUENCE",
                            f"Transition → {new_state}, no {path} sequence: {rule_name}")
                continue

            self._trace(rule_id, "transition", f"{path}_FIRING",
                        f"⚡ {prev_state or 'init'}→{new_state}: {path} ({len(seq)} steps) — {rule_name}",
                        conditions=cond_results, prerequisites=prereq_results)

            self._cancel_sequence(rule_id)
            task = asyncio.create_task(self._run_sequence(rule_id, rule_name, seq, path))
            self._running_sequences[rule_id] = task

            # ── EVENT ATTRIBUTE RESET ──
            _EVENT_ATTRS = {"action", "click", "button_action", "event", "scene", "command"}
            if any(c.get("attribute") in _EVENT_ATTRS for c in conditions):
                self._rule_states[rule_id] = "unmatched"

    # =========================================================================
    # CONDITION / PREREQUISITE EVALUATION
    # =========================================================================

    def _eval_conditions_block(self, conditions, rule_id, changed_data, full_state, now):
        """Evaluate source device conditions (AND). Returns (all_matched, results, has_sustain)."""
        results = []
        all_ok = True
        has_sustain = False

        for i, cond in enumerate(conditions):
            ctype = cond.get("type", "attribute")
            if ctype == "time_window":
                import datetime
                negate = cond.get("negate", False)
                now_dt = datetime.datetime.now()
                now_time = now_dt.time()
                weekday = now_dt.weekday()
                t_from = datetime.time(*map(int, cond["time_from"].split(":")))
                t_to   = datetime.time(*map(int, cond["time_to"].split(":")))
                days   = cond.get("days", list(range(7)))
                day_ok = (not days) or (weekday in days)
                if t_from <= t_to:
                    time_ok = t_from <= now_time <= t_to
                else:
                    time_ok = now_time >= t_from or now_time <= t_to
                matched = day_ok and time_ok
                if negate:
                    matched = not matched
                results.append({
                    "index": i + 1, "type": "time_window",
                    "time_from": cond["time_from"], "time_to": cond["time_to"],
                    "days": days, "negate": negate,
                    "now_time": now_dt.strftime("%H:%M"), "now_weekday": weekday,
                    "result": "PASS" if matched else "FAIL",
                })
                if not matched:
                    all_ok = False; break
                continue

            attr = cond["attribute"]
            op = cond["operator"]
            threshold = cond["value"]
            sustain = cond.get("sustain", 0) or 0

            if attr in changed_data:
                val = changed_data[attr]; src = "changed_data"
            elif attr in full_state:
                val = full_state[attr]; src = "full_state"
            else:
                results.append({"index": i+1, "attribute": attr, "result": "FAIL",
                                "reason": f"'{attr}' not in state"})
                all_ok = False; break

            try:
                matched = self._evaluate_condition(val, op, threshold)
            except Exception as e:
                results.append({"index": i+1, "attribute": attr, "result": "ERROR", "reason": str(e)})
                all_ok = False; break

            skey = f"{rule_id}_{i}"
            if matched and sustain > 0:
                if skey not in self._sustain_tracker:
                    self._sustain_tracker[skey] = now
                el = now - self._sustain_tracker[skey]
                if el < sustain:
                    results.append({"index": i+1, "attribute": attr, "operator": op,
                                    "threshold_raw": repr(threshold), "actual_raw": repr(val),
                                    "actual_type": type(val).__name__, "value_source": src,
                                    "result": "SUSTAIN_WAIT", "sustain_required": sustain,
                                    "sustain_elapsed": round(el, 1),
                                    "reason": f"Sustained {el:.1f}s / {sustain}s"})
                    has_sustain = True; all_ok = False; break

            if matched:
                self._sustain_tracker.pop(skey, None)
            else:
                self._sustain_tracker.pop(skey, None)

            results.append({"index": i+1, "attribute": attr, "operator": op,
                            "threshold_raw": repr(threshold),
                            "actual_raw": repr(val),
                            "actual_type": type(val).__name__,
                            "value_source": src,
                            "result": "PASS" if matched else "FAIL"})
            if not matched:
                all_ok = False; break

        return all_ok, results, has_sustain


    def _eval_prerequisites(self, prereqs, devices, names):
        """Evaluate prerequisites. time_window entries are OR'd; device entries are AND'd."""
        import datetime
        results = []
        all_met = True

        # ---- Partition ----
        tw_prereqs  = [(j, p) for j, p in enumerate(prereqs) if p.get("type") == "time_window"]
        dev_prereqs = [(j, p) for j, p in enumerate(prereqs) if p.get("type", "device") != "time_window"]

        # ---- time_window: OR logic ----
        if tw_prereqs:
            tw_any_passed = False
            for j, p in tw_prereqs:
                negate = p.get("negate", False)
                now_dt = datetime.datetime.now()
                now_time = now_dt.time()
                weekday = now_dt.weekday()
                t_from = datetime.time(*map(int, p["time_from"].split(":")))
                t_to   = datetime.time(*map(int, p["time_to"].split(":")))
                days   = p.get("days", list(range(7)))
                day_ok = (not days) or (weekday in days)
                if t_from <= t_to:
                    time_ok = t_from <= now_time <= t_to
                else:  # overnight wrap
                    time_ok = now_time >= t_from or now_time <= t_to
                matched = day_ok and time_ok
                if negate:
                    matched = not matched
                results.append({
                    "index": j + 1,
                    "type": "time_window",
                    "time_from": p["time_from"],
                    "time_to": p["time_to"],
                    "days": days,
                    "negate": negate,
                    "now_time": now_dt.strftime("%H:%M"),
                    "now_weekday": weekday,
                    "result": "PASS" if matched else "FAIL",
                })
                if matched:
                    tw_any_passed = True

            if not tw_any_passed:
                all_met = False
                return all_met, results

        for j, p in dev_prereqs:
            negate = p.get("negate", False)
            ieee = p["ieee"]
            attr = p["attribute"]
            op   = p["operator"]
            val  = p["value"]

            dname, state = self._resolve_state(ieee)
            if state is None:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "FAIL",
                                "reason": "Device/group not found"})
                all_met = False; break

            actual = state.get(attr)
            if actual is None:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "FAIL",
                                "reason": f"'{attr}' not in state",
                                "available_keys": list(state.keys())})
                all_met = False; break

            try:
                matched = self._evaluate_condition(actual, op, val)
                if negate:
                    matched = not matched
            except Exception as e:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "ERROR", "reason": str(e)})
                all_met = False; break

            results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                            "attribute": attr, "operator": op, "negate": negate,
                            "threshold_raw": repr(val),
                            "threshold_normalised": repr(self._normalise_value(val)),
                            "actual_raw": repr(actual),
                            "actual_normalised": repr(self._normalise_value(actual)),
                            "actual_type": type(actual).__name__,
                            "result": "PASS" if matched else "FAIL"})
            if not matched:
                all_met = False; break

        return all_met, results

    def _eval_inline_conditions(self, inline_conditions, logic="and"):
        """Evaluate inline conditions for if_then_else steps.
        Returns (met: bool, results: list).
        logic: 'and' or 'or'
        """
        devices = self._get_devices()
        names = self._get_names()
        results = []
        any_pass = False
        all_pass = True

        for ic in inline_conditions:
            ieee = ic["ieee"]
            attr = ic["attribute"]
            op = ic["operator"]
            threshold = ic["value"]
            negate = ic.get("negate", False)
            duration = ic.get("duration", 0) or 0  # "for" N seconds — check sustained

            dname, state = self._resolve_state(ieee)

            if state is None:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "FAIL", "reason": "Device/group not found"})
                all_pass = False
                continue

            actual = state.get(attr)
            if actual is None:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "FAIL", "reason": f"'{attr}' not in state"})
                all_pass = False
                continue

            try:
                matched = self._evaluate_condition(actual, op, threshold)
                if negate:
                    matched = not matched
            except Exception as e:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "ERROR", "reason": str(e)})
                all_pass = False
                continue

            # Duration check is handled by wait_for in practice
            # For inline conditions we just report current match
            results.append({"device_name": dname, "attribute": attr,
                            "operator": op, "negate": negate,
                            "threshold": repr(threshold), "actual": repr(actual),
                            "result": "PASS" if matched else "FAIL"})

            if matched:
                any_pass = True
            else:
                all_pass = False

        if logic == "or":
            return any_pass, results
        return all_pass, results

    # =========================================================================
    # SEQUENCE EXECUTOR (recursive)
    # =========================================================================

    def _cancel_sequence(self, rule_id: str):
        task = self._running_sequences.pop(rule_id, None)
        if task and not task.done():
            task.cancel()
            self._trace(rule_id, "sequence", "CANCELLED", "Previous sequence cancelled")

    async def _run_sequence(self, rule_id: str, rule_name: str,
                            steps: List[Dict], path: str, depth: int = 0):
        """Execute steps in order. Recursive for if_then_else/parallel."""
        prefix = "  " * depth
        try:
            for i, step in enumerate(steps):
                num = i + 1
                total = len(steps)
                st = step["type"]

                if st == "command":
                    await self._step_command(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                elif st == "delay":
                    secs = step.get("seconds", 0) or 0
                    if secs > 0:
                        self._trace(rule_id, "step", "DELAY",
                                    f"{prefix}[{path} {num}/{total}] ⏱ {secs}s")
                        await asyncio.sleep(secs)
                elif st == "wait_for":
                    met = await self._step_wait_for(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                    if not met:
                        self._trace(rule_id, "step", "WAIT_TIMEOUT",
                                    f"{prefix}[{path} {num}/{total}] ⏰ Timeout — stopping", level="WARNING")
                        break
                elif st == "condition":
                    met = self._step_gate(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                    if not met:
                        self._trace(rule_id, "step", "GATE_STOP",
                                    f"{prefix}[{path} {num}/{total}] Gate failed — stopping")
                        break
                elif st == "if_then_else":
                    await self._step_if_then_else(rule_id, rule_name, step,
                                                  f"{prefix}[{path} {num}/{total}]", depth)
                elif st == "parallel":
                    await self._step_parallel(rule_id, rule_name, step,
                                              f"{prefix}[{path} {num}/{total}]", depth)

            if depth == 0:
                self._trace(rule_id, "sequence", "COMPLETE",
                            f"✅ {path} sequence complete — {rule_name}")

        except asyncio.CancelledError:
            if depth == 0:
                self._trace(rule_id, "sequence", "CANCELLED",
                            f"{path} cancelled — {rule_name}")
        except Exception as e:
            self._stats["errors"] += 1
            self._trace(rule_id, "sequence", "EXCEPTION",
                        f"💥 {path} failed: {e}", level="ERROR",
                        traceback=traceback.format_exc())
        finally:
            if depth == 0:
                self._running_sequences.pop(rule_id, None)

    async def _step_command(self, rule_id, step, tag):
        target_ieee = step["target_ieee"]
        command = step["command"]
        value = step.get("value")
        endpoint_id = step.get("endpoint_id")
        devices = self._get_devices()
        names = self._get_names()

        # ── GROUP TARGET ROUTING ──
        if target_ieee.startswith("group:"):
            await self._step_group_command(rule_id, step, tag)
            return

        tname = names.get(target_ieee, target_ieee)
        target = devices.get(target_ieee)
        if not target or not hasattr(target, 'send_command'):
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} {tname} not found or no send_command", level="ERROR")
            return

        self._trace(rule_id, "step", "SENDING",
                    f"{tag} → {tname} {command}={value} EP={endpoint_id}")
        try:
            result = await target.send_command(command, value, endpoint_id=endpoint_id)
            success = True
            if isinstance(result, dict):
                success = result.get("success", True)
            elif result is not None:
                success = bool(result)

            self._stats["executions"] += 1
            if success:
                self._stats["execution_successes"] += 1
                self._trace(rule_id, "step", "SUCCESS",
                            f"{tag} ✅ {tname} {command}={value}")
            else:
                self._stats["execution_failures"] += 1
                self._trace(rule_id, "step", "CMD_FAIL",
                            f"{tag} ❌ {tname} {command} failed", level="ERROR")

            if self._event_emitter:
                await self._event_emitter("automation_triggered", {
                    "rule_id": rule_id, "target_ieee": target_ieee,
                    "command": command, "value": value, "success": success,
                    "timestamp": time.time()})
        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "EXCEPTION",
                        f"{tag} 💥 {tname} {command}: {e}", level="ERROR",
                        traceback=traceback.format_exc())


    async def _step_group_command(self, rule_id, step, tag):
        """Execute a command step targeting a group."""
        target_id_str = step["target_ieee"]
        command = step["command"]
        value = step.get("value")

        try:
            group_id = int(target_id_str.split(":", 1)[1])
        except (ValueError, IndexError):
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} Invalid group target: {target_id_str}", level="ERROR")
            return

        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} Group {group_id} not found", level="ERROR")
            return

        group_name = gm.groups[group_id]["name"]

        # Build command dict for control_group()
        cmd = {}
        if command in ("on", "off", "toggle"):
            cmd["state"] = command.upper()
        elif command == "brightness":
            cmd["brightness"] = int(value) if value is not None else 254
        elif command == "color_temp":
            cmd["color_temp"] = int(value) if value is not None else 370
        elif command in ("open", "close", "stop"):
            cmd["cover_state"] = command.upper()
        elif command == "position":
            cmd["position"] = int(value) if value is not None else 50
        elif command in ("lock", "unlock"):
            cmd["state"] = command.upper()
        else:
            cmd[command] = value

        self._trace(rule_id, "step", "SENDING",
                    f"{tag} → Group '{group_name}' {command}={value}")
        try:
            result = await gm.control_group(group_id, cmd)
            success = result.get("success", False)
            self._stats["executions"] += 1
            if success:
                self._stats["execution_successes"] += 1
                self._trace(rule_id, "step", "SUCCESS",
                            f"{tag} ✅ Group '{group_name}' {command}={value}")
            else:
                self._stats["execution_failures"] += 1
                self._trace(rule_id, "step", "CMD_FAIL",
                            f"{tag} ❌ Group '{group_name}' {command} failed: "
                            f"{result.get('error', '')}", level="ERROR")
        except Exception as e:
            self._stats["execution_failures"] += 1
            self._stats["errors"] += 1
            self._trace(rule_id, "step", "EXCEPTION",
                        f"{tag} 💥 Group '{group_name}' failed: {e}", level="ERROR")


    async def _step_wait_for(self, rule_id, step, tag) -> bool:
        ieee = step["ieee"]
        attr = step["attribute"]
        op = step["operator"]
        threshold = step["value"]
        timeout = step.get("timeout", 300) or 300

        dname, _ = self._resolve_state(ieee)
        self._trace(rule_id, "step", "WAITING",
                    f"{tag} ⏳ {dname} {attr} {op} {threshold} (timeout {timeout}s)")

        start = time.time()
        while time.time() - start < timeout:
            _, state = self._resolve_state(ieee)
            if state:
                val = state.get(attr)
                if val is not None:
                    try:
                        negate = step.get("negate", False)
                        matched = self._evaluate_condition(val, op, threshold)
                        if negate: matched = not matched
                        if matched:
                            el = time.time() - start
                            self._trace(rule_id, "step", "WAIT_MET",
                                        f"{tag} ✅ {dname} {attr}={repr(val)} met after {el:.1f}s")
                            return True
                    except Exception:
                        pass
            await asyncio.sleep(WAIT_FOR_POLL_INTERVAL)
        return False

    def _step_gate(self, rule_id, step, tag) -> bool:
        ieee = step["ieee"]
        attr = step["attribute"]
        op = step["operator"]
        threshold = step["value"]
        negate = step.get("negate", False)

        dname, state = self._resolve_state(ieee)
        if not state:
            return False
        val = state.get(attr)
        if val is None:
            return False
        try:
            result = self._evaluate_condition(val, op, threshold)
            if negate: result = not result
        except Exception:
            return False

        self._trace(rule_id, "step",
                    "GATE_PASS" if result else "GATE_FAIL",
                    f"{tag} {'🔒' if not result else '✅'} {dname} {attr} {op} {threshold}"
                    f"{' NOT' if negate else ''} → {repr(val)} → {'PASS' if result else 'FAIL'}",
                    level="DEBUG")
        return result

    async def _step_if_then_else(self, rule_id, rule_name, step, tag, depth):
        inline = step.get("inline_conditions", [])
        logic = step.get("condition_logic", "and")

        met, ic_results = self._eval_inline_conditions(inline, logic)

        branch_label = "if.then" if met else "if.else"
        self._trace(rule_id, "step", f"IF_{'TRUE' if met else 'FALSE'}",
                    f"{tag} IF ({logic.upper()}) → {'TRUE' if met else 'FALSE'}: "
                    f"running {branch_label}",
                    inline_conditions=ic_results)

        if met:
            sub_steps = step.get("then_steps", [])
        else:
            sub_steps = step.get("else_steps", [])

        if sub_steps:
            await self._run_sequence(rule_id, rule_name, sub_steps,
                                     f"{branch_label}", depth + 1)

    async def _step_parallel(self, rule_id, rule_name, step, tag, depth):
        branches = step.get("branches", [])
        self._trace(rule_id, "step", "PARALLEL",
                    f"{tag} ⚡ Running {len(branches)} branches in parallel")

        tasks = []
        for bi, branch in enumerate(branches):
            t = asyncio.create_task(
                self._run_sequence(rule_id, rule_name, branch,
                                   f"parallel.{bi+1}", depth + 1)
            )
            tasks.append(t)

        await asyncio.gather(*tasks, return_exceptions=True)
        self._trace(rule_id, "step", "PARALLEL_DONE",
                    f"{tag} All parallel branches complete")

    # =========================================================================
    # CONDITION HELPERS
    # =========================================================================

    def _evaluate_condition(self, actual_value, operator, threshold_value) -> bool:
        op_func = OPERATORS.get(operator)
        if not op_func:
            return False

        # Handle "in" / "nin" operators — threshold is a list
        if operator in ("in", "nin"):
            actual = self._normalise_value(actual_value)
            if isinstance(threshold_value, list):
                values = [self._normalise_value(v) for v in threshold_value]
            elif isinstance(threshold_value, str) and "," in threshold_value:
                values = [self._normalise_value(v.strip()) for v in threshold_value.split(",")]
            else:
                values = [self._normalise_value(threshold_value)]
            # Case-insensitive string matching
            matched = False
            for v in values:
                if isinstance(actual, str) and isinstance(v, str):
                    if actual.lower() == v.lower():
                        matched = True; break
                elif actual == v:
                    matched = True; break
            return matched if operator == "in" else not matched

        actual = self._normalise_value(actual_value)
        threshold = self._normalise_value(threshold_value)

        if isinstance(actual, str) and isinstance(threshold, str) and operator in ("eq", "neq"):
            if operator == "eq": return actual.lower() == threshold.lower()
            return actual.lower() != threshold.lower()

        if isinstance(actual, bool) and isinstance(threshold, str):
            threshold = threshold.lower() in ("on", "true")
        elif isinstance(threshold, bool) and isinstance(actual, str):
            actual = actual.lower() in ("on", "true")

        try:
            return op_func(actual, threshold)
        except (TypeError, ValueError):
            return op_func(str(actual).lower(), str(threshold).lower())

    @staticmethod
    def _normalise_value(value):
        if isinstance(value, str):
            stripped = value.strip().strip("'\"")
            lower = stripped.lower()
            if lower == "true": return True
            if lower == "false": return False
            try:
                if "." in stripped: return float(stripped)
                return int(stripped)
            except ValueError:
                return stripped
        return value

    # =========================================================================
    # GROUP STATE HELPERS
    # =========================================================================

    def _get_group_state(self, group_id: int) -> dict:
        """Aggregate state from group members.
        ON/OFF: any ON → ON. Numerics: average. Others: first value."""
        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            return {}
        devices = self._get_devices()
        members = [devices.get(ieee) for ieee in gm.groups[group_id].get("members", [])
                   if devices.get(ieee)]
        if not members:
            return {}

        all_states = [m.state or {} for m in members]
        all_keys = set()
        for s in all_states:
            all_keys.update(s.keys())

        skip = {"last_seen", "available", "manufacturer", "model",
                "power_source", "lqi", "linkquality"}
        merged = {}
        for key in all_keys:
            if key in skip or key.endswith("_raw") or key.startswith("attr_"):
                continue
            values = [s[key] for s in all_states if key in s and s[key] is not None]
            if not values:
                continue
            first = values[0]
            if isinstance(first, str) and first.upper() in ("ON", "OFF"):
                merged[key] = "ON" if any(
                    v.upper() == "ON" for v in values if isinstance(v, str)
                ) else "OFF"
            elif isinstance(first, bool):
                merged[key] = any(values)
            elif isinstance(first, (int, float)):
                merged[key] = round(sum(values) / len(values), 1)
            else:
                merged[key] = first
        return merged

    def _resolve_state(self, ieee_or_group: str):
        """Resolve (friendly_name, state_dict) for device OR group:<id>.
        Returns (name, None) if not found."""
        if ieee_or_group.startswith("group:"):
            try:
                gid = int(ieee_or_group.split(":", 1)[1])
            except (ValueError, IndexError):
                return ieee_or_group, None
            gm = self._get_group_manager() if self._get_group_manager else None
            if not gm or gid not in gm.groups:
                return ieee_or_group, None
            return f"\U0001F517 {gm.groups[gid]['name']}", self._get_group_state(gid)

        devices = self._get_devices()
        names = self._get_names()
        dev = devices.get(ieee_or_group)
        if not dev:
            return names.get(ieee_or_group, ieee_or_group), None
        return names.get(ieee_or_group, ieee_or_group), dev.state or {}

    @staticmethod
    def _get_group_commands(group_type: str, capabilities: list) -> list:
        """Generate command list for a group based on type/capabilities."""
        cmds = []
        if group_type in ("light", "switch"):
            cmds.extend([
                {"command": "on",     "label": "On",     "endpoint_id": None},
                {"command": "off",    "label": "Off",    "endpoint_id": None},
                {"command": "toggle", "label": "Toggle", "endpoint_id": None},
            ])
        if "brightness" in capabilities:
            cmds.append({"command": "brightness", "label": "Brightness",
                         "type": "slider", "min": 0, "max": 254, "endpoint_id": None})
        if "color_temp" in capabilities:
            cmds.append({"command": "color_temp", "label": "Color Temp",
                         "type": "slider", "min": 153, "max": 500, "endpoint_id": None})
        if group_type == "cover":
            cmds.extend([
                {"command": "open",  "label": "Open",  "endpoint_id": None},
                {"command": "close", "label": "Close", "endpoint_id": None},
                {"command": "stop",  "label": "Stop",  "endpoint_id": None},
                {"command": "position", "label": "Position",
                 "type": "slider", "min": 0, "max": 100, "endpoint_id": None},
            ])
        if group_type == "lock":
            cmds.extend([
                {"command": "lock",   "label": "Lock",   "endpoint_id": None},
                {"command": "unlock", "label": "Unlock", "endpoint_id": None},
            ])
        return cmds

    def _is_group_homogeneous(self, gm, group: dict) -> bool:
        """True only if all members resolve to exactly one device type."""
        members = group.get("members", [])
        if len(members) < 2:
            return False
        types = set()
        for ieee in members:
            device = self._get_devices().get(ieee)
            if not device:
                continue
            dtype = gm.get_device_type(device)
            if dtype:
                types.add(dtype)
        return len(types) == 1

    def get_source_attributes(self, ieee: str) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        if ieee not in devices: return []
        state = devices[ieee].state
        skip = {"last_seen","available","manufacturer","model","power_source","lqi","linkquality"}
        attrs = []
        for k, v in state.items():
            if k in skip or k.endswith("_raw") or k.startswith("attr_"): continue
            a = {"attribute":k,"current_value":v,"type":self._type(v)}
            if isinstance(v, bool):
                a["operators"]=["eq","neq"]; a["value_options"]=["true","false"]
            elif isinstance(v, str) and v.upper() in ("ON","OFF"):
                a["operators"]=["eq","neq","in","nin"]; a["value_options"]=["ON","OFF"]
            elif isinstance(v,(int,float)):
                a["operators"]=["eq","neq","gt","lt","gte","lte"]
            else:
                a["operators"]=["eq","neq","in","nin"]
            attrs.append(a)
        return sorted(attrs, key=lambda x:x["attribute"])

    def get_device_state(self, ieee: str) -> Dict[str, Any]:
        # ── GROUP TARGET ──
        if ieee.startswith("group:"):
            try:
                gid = int(ieee.split(":", 1)[1])
            except (ValueError, IndexError):
                return {}
            gm = self._get_group_manager() if self._get_group_manager else None
            if not gm or gid not in gm.groups:
                return {}
            group = gm.groups[gid]
            gstate = self._get_group_state(gid)
            attrs = []
            for k, v in gstate.items():
                a = {"attribute": k, "current_value": v, "type": self._type(v),
                     "operators": ["eq", "neq", "in", "nin"] if isinstance(v, str) else
                     ["eq", "neq"] if isinstance(v, bool) else
                     ["eq", "neq", "gt", "lt", "gte", "lte"]}
                if isinstance(v, bool):
                    a["value_options"] = ["true", "false"]
                elif isinstance(v, str) and v.upper() in ("ON", "OFF"):
                    a["value_options"] = ["ON", "OFF"]
                attrs.append(a)
            return {"ieee": ieee,
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "state": gstate, "attributes": attrs}

        # ── NORMAL DEVICE ──
        devices = self._get_devices()
        names = self._get_names()
        if ieee not in devices: return {}
        state = devices[ieee].state or {}
        attrs = []
        for k, v in state.items():
            if k.endswith("_raw") or k.startswith("attr_"): continue
            a = {"attribute": k, "current_value": v, "type": self._type(v),
                 "operators": ["eq", "neq", "in", "nin"] if isinstance(v, str) else
                 ["eq", "neq"] if isinstance(v, bool) else
                 ["eq", "neq", "gt", "lt", "gte", "lte"]}
            if isinstance(v, bool): a["value_options"] = ["true", "false"]
            elif isinstance(v, str) and v.upper() in ("ON", "OFF"): a["value_options"] = ["ON", "OFF"]
            attrs.append(a)
        return {"ieee": ieee, "friendly_name": names.get(ieee, ieee),
                "state": state, "attributes": attrs}

    def get_target_actions(self, ieee):
        d = self._get_devices().get(ieee)
        return d.get_control_commands() if d and hasattr(d,"get_control_commands") else []

    def get_actuator_devices(self):
        devices = self._get_devices(); names = self._get_names()
        out = []
        for ieee, dev in devices.items():
            caps = getattr(dev, "capabilities", None)
            if not caps: continue
            hc = getattr(caps, "has_capability", lambda x: False)
            if not any(hc(c) for c in ["on_off", "light", "switch", "cover",
                                       "window_covering", "thermostat", "fan_control"]):
                continue
            out.append({"ieee": ieee, "friendly_name": names.get(ieee, ieee),
                        "model": getattr(dev, "model", "Unknown"),
                        "commands": dev.get_control_commands() if hasattr(dev, "get_control_commands") else []})

        # Append eligible homogeneous groups
        gm = self._get_group_manager() if self._get_group_manager else None
        if gm:
            for group_id, group in gm.groups.items():
                if not self._is_group_homogeneous(gm, group):
                    continue
                gtype = group.get("type", "switch")
                caps_list = group.get("capabilities", [])
                out.append({
                    "ieee": f"group:{group_id}",
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "model": f"{gtype.capitalize()} Group ({len(group['members'])} devices)",
                    "commands": self._get_group_commands(gtype, caps_list),
                    "_is_group": True,
                })

        return sorted(out, key=lambda d: d.get("friendly_name", ""))


    @staticmethod
    def _get_group_commands(group_type: str, capabilities: list) -> list:
        """Generate command list for a group based on type and capabilities."""
        cmds = []
        if group_type in ("light", "switch"):
            cmds.extend([
                {"command": "on",     "label": "On",     "endpoint_id": None},
                {"command": "off",    "label": "Off",    "endpoint_id": None},
                {"command": "toggle", "label": "Toggle", "endpoint_id": None},
            ])
        if "brightness" in capabilities:
            cmds.append({"command": "brightness", "label": "Brightness",
                         "type": "slider", "min": 0, "max": 254, "endpoint_id": None})
        if "color_temp" in capabilities:
            cmds.append({"command": "color_temp", "label": "Color Temp",
                         "type": "slider", "min": 153, "max": 500, "endpoint_id": None})
        if group_type == "cover":
            cmds.extend([
                {"command": "open",     "label": "Open",     "endpoint_id": None},
                {"command": "close",    "label": "Close",    "endpoint_id": None},
                {"command": "stop",     "label": "Stop",     "endpoint_id": None},
                {"command": "position", "label": "Position",
                 "type": "slider", "min": 0, "max": 100, "endpoint_id": None},
            ])
        if group_type == "lock":
            cmds.extend([
                {"command": "lock",   "label": "Lock",   "endpoint_id": None},
                {"command": "unlock", "label": "Unlock", "endpoint_id": None},
            ])
        return cmds

    def _is_group_homogeneous(self, gm, group: dict) -> bool:
        """Check all members resolve to the same device type."""
        members = group.get("members", [])
        if len(members) < 2:
            return False
        types = set()
        for ieee in members:
            device = self._get_devices().get(ieee)
            if not device:
                continue
            dtype = gm.get_device_type(device)
            if dtype:
                types.add(dtype)
        return len(types) == 1

    def get_group_target_actions(self, group_id: int) -> list:
        """Get available commands for a group target."""
        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            return []
        group = gm.groups[group_id]
        return self._get_group_commands(group.get("type", "switch"),
                                        group.get("capabilities", []))


    def get_all_devices_summary(self):
        devices = self._get_devices(); names = self._get_names()
        out = sorted([
            {"ieee": ieee, "friendly_name": names.get(ieee, ieee),
             "model": getattr(d, "model", "Unknown"),
             "state_keys": [k for k in (d.state or {}).keys()
                            if not k.endswith("_raw") and not k.startswith("attr_")]}
            for ieee, d in devices.items()
        ], key=lambda x: x.get("friendly_name", ""))

        # Append homogeneous groups
        gm = self._get_group_manager() if self._get_group_manager else None
        if gm:
            for group_id, group in gm.groups.items():
                if not self._is_group_homogeneous(gm, group):
                    continue
                gstate = self._get_group_state(group_id)
                out.append({
                    "ieee": f"group:{group_id}",
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "model": f"{group.get('type', 'switch').capitalize()} Group",
                    "state_keys": list(gstate.keys()),
                    "_is_group": True,
                })

        return out

    @staticmethod
    def _type(v):
        if isinstance(v,bool): return "boolean"
        if isinstance(v,int): return "integer"
        if isinstance(v,float): return "float"
        return "string"

    def get_stats(self):
        return {**self._stats, "total_rules":len(self.rules),
                "enabled_rules":sum(1 for r in self.rules if r.get("enabled",True)),
                "trace_entries":len(self._trace_log),
                "active_sustains":len(self._sustain_tracker),
                "running_sequences:wq":sum(1 for t in self._running_sequences.values() if not t.done())}