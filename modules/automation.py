"""
Automation Engine - Threshold-based Device Triggers
====================================================
Standalone automation that evaluates device state changes against
user-defined thresholds and fires direct ZigBee commands to target
devices, bypassing MQTT for low-latency execution.

Supports compound conditions (multiple AND thresholds per rule).

Persistence: ./data/automations.json
Hook point:  core.py -> _debounced_device_update (after changed_data computed)
Execution:   device.send_command() (direct zigpy cluster commands)
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("modules.automation")

# ============================================================================
# CONSTANTS
# ============================================================================

MAX_RULES_PER_DEVICE = 10
MAX_CONDITIONS_PER_RULE = 5
DATA_FILE = "./data/automations.json"
DEFAULT_COOLDOWN = 5  # seconds between re-fires of same rule

# Valid comparison operators
OPERATORS = {
    "eq":  lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt":  lambda a, b: float(a) > float(b),
    "lt":  lambda a, b: float(a) < float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "lte": lambda a, b: float(a) <= float(b),
}

# Commands that are valid for automation targets
VALID_COMMANDS = {"on", "off", "toggle", "brightness", "color_temp", "open", "close", "stop", "position"}


# ============================================================================
# AUTOMATION ENGINE
# ============================================================================

class AutomationEngine:
    """
    Evaluates device state changes against threshold rules and
    fires direct ZigBee commands to target devices.

    Lifecycle:
        1. Initialised in core.py alongside other services
        2. Rules loaded from ./data/automations.json
        3. evaluate() called from _debounced_device_update on every state change
        4. Matching rules execute device.send_command() directly

    Trace events emitted via WebSocket for frontend visibility:
        - automation_trace: Full evaluation trace for every rule checked
        - automation_triggered: Fired when a command executes (success or fail)
    """

    def __init__(self, device_registry_getter: Callable[[], Dict],
                 friendly_names_getter: Callable[[], Dict],
                 event_emitter: Optional[Callable] = None):
        """
        Args:
            device_registry_getter: Callable returning dict of {ieee: ZigManDevice}
            friendly_names_getter:  Callable returning dict of {ieee: friendly_name}
            event_emitter:          Optional async callback for WebSocket events
        """
        self._get_devices = device_registry_getter
        self._get_names = friendly_names_getter
        self._event_emitter = event_emitter

        # Rule storage: list of rule dicts
        self.rules: List[Dict[str, Any]] = []

        # Index: source_ieee -> [rule_ids] for fast lookup
        self._source_index: Dict[str, List[str]] = {}

        # Cooldown tracking: rule_id -> last_fired_timestamp
        self._cooldowns: Dict[str, float] = {}

        # Recent trace log (ring buffer for frontend)
        self._trace_log: List[Dict[str, Any]] = []
        self._max_trace_entries = 100

        # Statistics
        self._stats = {
            "evaluations": 0,
            "matches": 0,
            "executions": 0,
            "execution_successes": 0,
            "execution_failures": 0,
            "errors": 0,
        }

        # Load persisted rules
        self._load_rules()
        logger.info(f"Automation engine initialised with {len(self.rules)} rule(s)")

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _load_rules(self):
        """Load rules from JSON file."""
        if not os.path.exists(DATA_FILE):
            self.rules = []
            self._rebuild_index()
            return

        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            self.rules = data.get("rules", [])

            # Migrate legacy single-threshold rules to conditions format
            migrated = False
            for rule in self.rules:
                if "threshold" in rule and "conditions" not in rule:
                    rule["conditions"] = [rule.pop("threshold")]
                    migrated = True
            if migrated:
                self._save_rules()
                logger.info("Migrated legacy threshold rules to conditions format")

            self._rebuild_index()
            logger.info(f"Loaded {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self.rules = []
            self._rebuild_index()

    def _save_rules(self):
        """Persist rules to JSON file."""
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({"rules": self.rules}, f, indent=2)
            logger.debug(f"Saved {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to save automations: {e}")

    def _rebuild_index(self):
        """Rebuild source_ieee -> rule_id lookup index."""
        self._source_index.clear()
        for rule in self.rules:
            src = rule.get("source_ieee")
            if src:
                self._source_index.setdefault(src, []).append(rule["id"])

    # =========================================================================
    # TRACING
    # =========================================================================

    def _add_trace(self, trace: Dict[str, Any]):
        """Add a trace entry to the ring buffer and emit via WebSocket."""
        trace["timestamp"] = time.time()
        self._trace_log.append(trace)

        # Trim ring buffer
        if len(self._trace_log) > self._max_trace_entries:
            self._trace_log = self._trace_log[-self._max_trace_entries:]

        # Log to file at appropriate level
        level = trace.get("level", "DEBUG")
        msg = trace.get("message", "")
        rule_id = trace.get("rule_id", "?")
        log_msg = f"[AUTOMATION {rule_id}] {msg}"

        if level == "ERROR":
            logger.error(log_msg)
        elif level == "WARNING":
            logger.warning(log_msg)
        elif level == "INFO":
            logger.info(log_msg)
        else:
            logger.debug(log_msg)

        # Emit to WebSocket (non-blocking)
        if self._event_emitter:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_emitter("automation_trace", trace))
            except RuntimeError:
                pass  # No event loop running

    def get_trace_log(self) -> List[Dict[str, Any]]:
        """Get recent trace entries for frontend display."""
        return list(self._trace_log)

    # =========================================================================
    # RULE CRUD
    # =========================================================================

    def add_rule(self, rule_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add a new automation rule with one or more AND conditions.

        Accepts EITHER:
          - conditions: [{attribute, operator, value}, ...] (compound)
          - attribute/operator/value (single - converted to conditions list)

        Plus:
            source_ieee, target_ieee, command, command_value,
            endpoint_id, cooldown, enabled

        Returns:
            {"success": True, "rule": {...}} or {"success": False, "error": "..."}
        """
        # --- Build conditions list ---
        conditions = rule_data.get("conditions")
        if conditions:
            if not isinstance(conditions, list) or len(conditions) == 0:
                return {"success": False, "error": "conditions must be a non-empty list"}
            if len(conditions) > MAX_CONDITIONS_PER_RULE:
                return {"success": False, "error": f"Maximum {MAX_CONDITIONS_PER_RULE} conditions per rule"}
        elif all(k in rule_data for k in ("attribute", "operator", "value")):
            # Single condition shorthand
            conditions = [{
                "attribute": rule_data["attribute"],
                "operator": rule_data["operator"],
                "value": rule_data["value"],
            }]
        else:
            return {"success": False, "error": "Provide 'conditions' list or attribute/operator/value fields"}

        # Validate each condition
        for i, cond in enumerate(conditions):
            for field in ("attribute", "operator", "value"):
                if field not in cond:
                    return {"success": False, "error": f"Condition {i+1} missing '{field}'"}
            if cond["operator"] not in OPERATORS:
                return {"success": False, "error": f"Condition {i+1} invalid operator: {cond['operator']}"}

        # Validate required top-level fields
        for field in ("source_ieee", "target_ieee", "command"):
            if field not in rule_data:
                return {"success": False, "error": f"Missing required field: {field}"}

        # Validate command
        if rule_data["command"] not in VALID_COMMANDS:
            return {"success": False, "error": f"Invalid command: {rule_data['command']}. Valid: {', '.join(VALID_COMMANDS)}"}

        # Check max rules per source device
        source_ieee = rule_data["source_ieee"]
        existing_count = len(self._source_index.get(source_ieee, []))
        if existing_count >= MAX_RULES_PER_DEVICE:
            return {"success": False, "error": f"Maximum {MAX_RULES_PER_DEVICE} rules per device reached"}

        # Validate devices exist
        devices = self._get_devices()
        if source_ieee not in devices:
            return {"success": False, "error": f"Source device not found: {source_ieee}"}
        if rule_data["target_ieee"] not in devices:
            return {"success": False, "error": f"Target device not found: {rule_data['target_ieee']}"}

        # Build the rule
        rule = {
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "enabled": rule_data.get("enabled", True),
            "source_ieee": source_ieee,
            "conditions": conditions,
            "target_ieee": rule_data["target_ieee"],
            "action": {
                "command": rule_data["command"],
                "value": rule_data.get("command_value"),
                "endpoint_id": rule_data.get("endpoint_id"),
            },
            "cooldown": rule_data.get("cooldown", DEFAULT_COOLDOWN),
            "created": time.time(),
        }

        self.rules.append(rule)
        self._rebuild_index()
        self._save_rules()

        cond_summary = " AND ".join(
            f"{c['attribute']} {c['operator']} {c['value']}" for c in conditions
        )
        logger.info(f"Automation rule added: {rule['id']} "
                    f"({source_ieee} [{cond_summary}] "
                    f"-> {rule_data['target_ieee']} {rule_data['command']})")

        return {"success": True, "rule": rule}

    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing rule.

        Updatable fields: enabled, conditions, target_ieee,
        command, command_value, endpoint_id, cooldown
        """
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Rule not found: {rule_id}"}

        # Update conditions (replaces the entire list)
        if "conditions" in updates:
            conditions = updates["conditions"]
            if not isinstance(conditions, list) or len(conditions) == 0:
                return {"success": False, "error": "conditions must be a non-empty list"}
            if len(conditions) > MAX_CONDITIONS_PER_RULE:
                return {"success": False, "error": f"Maximum {MAX_CONDITIONS_PER_RULE} conditions per rule"}
            for i, cond in enumerate(conditions):
                for field in ("attribute", "operator", "value"):
                    if field not in cond:
                        return {"success": False, "error": f"Condition {i+1} missing '{field}'"}
                if cond["operator"] not in OPERATORS:
                    return {"success": False, "error": f"Condition {i+1} invalid operator: {cond['operator']}"}
            rule["conditions"] = conditions

        # Update action fields
        if "command" in updates:
            if updates["command"] not in VALID_COMMANDS:
                return {"success": False, "error": f"Invalid command: {updates['command']}"}
            rule["action"]["command"] = updates["command"]
        if "command_value" in updates:
            rule["action"]["value"] = updates["command_value"]
        if "endpoint_id" in updates:
            rule["action"]["endpoint_id"] = updates["endpoint_id"]
        if "target_ieee" in updates:
            rule["target_ieee"] = updates["target_ieee"]

        # Update top-level fields
        if "enabled" in updates:
            rule["enabled"] = bool(updates["enabled"])
        if "cooldown" in updates:
            rule["cooldown"] = max(0, int(updates["cooldown"]))

        rule["updated"] = time.time()

        self._rebuild_index()
        self._save_rules()

        logger.info(f"Automation rule updated: {rule_id}")
        return {"success": True, "rule": rule}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        """Delete an automation rule."""
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Rule not found: {rule_id}"}

        self.rules.remove(rule)
        self._cooldowns.pop(rule_id, None)
        self._rebuild_index()
        self._save_rules()

        logger.info(f"Automation rule deleted: {rule_id}")
        return {"success": True}

    def get_rules(self, source_ieee: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all rules, optionally filtered by source device.
        Enriches rules with friendly names for frontend display.
        """
        names = self._get_names()
        rules = self.rules if not source_ieee else [
            r for r in self.rules if r["source_ieee"] == source_ieee
        ]

        enriched = []
        for rule in rules:
            r = rule.copy()
            r["source_name"] = names.get(rule["source_ieee"], rule["source_ieee"])
            r["target_name"] = names.get(rule["target_ieee"], rule["target_ieee"])
            enriched.append(r)

        return enriched

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Get a single rule by ID."""
        return self._find_rule(rule_id)

    def _find_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Find a rule by ID."""
        for rule in self.rules:
            if rule["id"] == rule_id:
                return rule
        return None

    # =========================================================================
    # EVALUATION ENGINE
    # =========================================================================

    async def evaluate(self, source_ieee: str, changed_data: Dict[str, Any]):
        """
        Evaluate all rules for a source device against changed state data.
        Called from core.py _debounced_device_update.

        For compound rules (multiple conditions), ALL conditions must be true (AND).
        Evaluation triggers when ANY condition attribute appears in changed_data.
        Non-changed attributes are read from the device's current full state.

        Emits detailed trace for every rule evaluated — every decision is logged
        so you can see exactly why a rule did or did not fire.

        Args:
            source_ieee:  IEEE of the device whose state changed
            changed_data: Dict of {attribute: new_value} that changed
        """
        # Fast exit: no rules for this device
        rule_ids = self._source_index.get(source_ieee)
        if not rule_ids:
            return

        self._stats["evaluations"] += 1

        now = time.time()
        devices = self._get_devices()
        names = self._get_names()
        source_name = names.get(source_ieee, source_ieee)

        # Get the source device's full state for non-changed attribute lookups
        source_device = devices.get(source_ieee)
        if not source_device:
            self._add_trace({
                "rule_id": "-",
                "level": "ERROR",
                "phase": "lookup",
                "result": "SOURCE_MISSING",
                "message": f"Source device {source_ieee} ({source_name}) not in device registry",
                "source_ieee": source_ieee,
                "changed_data": self._safe_repr(changed_data),
            })
            return

        full_state = source_device.state or {}

        self._add_trace({
            "rule_id": "-",
            "level": "DEBUG",
            "phase": "entry",
            "result": "EVALUATING",
            "message": f"State change on {source_name}: {list(changed_data.keys())} — checking {len(rule_ids)} rule(s)",
            "source_ieee": source_ieee,
            "changed_keys": list(changed_data.keys()),
            "changed_data": self._safe_repr(changed_data),
            "full_state_keys": list(full_state.keys()),
        })

        for rule_id in rule_ids:
            rule = self._find_rule(rule_id)
            if not rule:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "WARNING",
                    "phase": "lookup",
                    "result": "RULE_MISSING",
                    "message": f"Rule {rule_id} in index but not found in rules list",
                    "source_ieee": source_ieee,
                })
                continue

            # --- TRACE: Rule disabled ---
            if not rule.get("enabled", True):
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "DEBUG",
                    "phase": "enabled",
                    "result": "DISABLED",
                    "message": f"Rule {rule_id} is disabled — skipping",
                    "source_ieee": source_ieee,
                })
                continue

            conditions = self._get_conditions(rule)
            if not conditions:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "WARNING",
                    "phase": "conditions",
                    "result": "NO_CONDITIONS",
                    "message": "Rule has no conditions defined",
                    "source_ieee": source_ieee,
                })
                continue

            # --- Check if any condition attribute is in changed_data ---
            watched_attrs = {c["attribute"] for c in conditions}
            changed_attrs = set(changed_data.keys())
            triggered_attrs = watched_attrs.intersection(changed_attrs)

            if not triggered_attrs:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "DEBUG",
                    "phase": "relevance",
                    "result": "NOT_RELEVANT",
                    "message": f"Watches {watched_attrs} but changed was {changed_attrs} — no overlap",
                    "source_ieee": source_ieee,
                    "watched": list(watched_attrs),
                    "changed": list(changed_attrs),
                })
                continue

            # --- EVALUATE ALL CONDITIONS (AND logic) ---
            all_matched = True
            condition_results = []

            for i, cond in enumerate(conditions):
                attr = cond["attribute"]
                operator = cond["operator"]
                threshold = cond["value"]

                # Resolve current value: prefer changed_data, fallback to full state
                if attr in changed_data:
                    current_value = changed_data[attr]
                    value_source = "changed_data"
                elif attr in full_state:
                    current_value = full_state[attr]
                    value_source = "full_state"
                else:
                    cond_result = {
                        "index": i + 1,
                        "attribute": attr,
                        "operator": operator,
                        "threshold": threshold,
                        "result": "FAIL",
                        "reason": f"Attribute '{attr}' not found in changed_data or device state",
                        "available_keys": list(set(list(changed_data.keys()) + list(full_state.keys()))),
                    }
                    condition_results.append(cond_result)
                    all_matched = False
                    break

                # Normalise both sides for trace display
                normalised_actual = self._normalise_value(current_value)
                normalised_threshold = self._normalise_value(threshold)

                try:
                    matched = self._evaluate_condition(current_value, operator, threshold)
                except Exception as e:
                    cond_result = {
                        "index": i + 1,
                        "attribute": attr,
                        "operator": operator,
                        "threshold": repr(threshold),
                        "threshold_type": type(threshold).__name__,
                        "actual": repr(current_value),
                        "actual_type": type(current_value).__name__,
                        "result": "ERROR",
                        "reason": f"Evaluation exception: {e}",
                    }
                    condition_results.append(cond_result)
                    all_matched = False
                    break

                cond_result = {
                    "index": i + 1,
                    "attribute": attr,
                    "operator": operator,
                    "threshold_raw": repr(threshold),
                    "threshold_normalised": repr(normalised_threshold),
                    "threshold_type": type(normalised_threshold).__name__,
                    "actual_raw": repr(current_value),
                    "actual_normalised": repr(normalised_actual),
                    "actual_type": type(normalised_actual).__name__,
                    "value_source": value_source,
                    "matched": matched,
                    "result": "PASS" if matched else "FAIL",
                }
                condition_results.append(cond_result)

                if not matched:
                    all_matched = False
                    break  # Short-circuit AND

            target_ieee = rule.get("target_ieee", "")
            target_name = names.get(target_ieee, target_ieee)

            # --- TRACE: Condition evaluation result ---
            if not all_matched:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "INFO",
                    "phase": "evaluate",
                    "result": "NO_MATCH",
                    "message": f"Conditions not met: {source_name} -> {target_name}",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "triggered_by": list(triggered_attrs),
                    "conditions": condition_results,
                })
                continue

            self._stats["matches"] += 1

            # --- COOLDOWN CHECK ---
            cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
            last_fired = self._cooldowns.get(rule_id, 0)
            elapsed = now - last_fired

            if elapsed < cooldown:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "INFO",
                    "phase": "cooldown",
                    "result": "BLOCKED",
                    "message": f"Cooldown: {elapsed:.1f}s elapsed < {cooldown}s required",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "conditions": condition_results,
                })
                continue

            # --- TARGET DEVICE CHECK ---
            if target_ieee not in devices:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "ERROR",
                    "phase": "target",
                    "result": "TARGET_MISSING",
                    "message": f"Target device {target_ieee} ({target_name}) not in device registry",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "conditions": condition_results,
                })
                continue

            target_device = devices[target_ieee]

            # --- Check target has send_command ---
            if not hasattr(target_device, 'send_command'):
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "ERROR",
                    "phase": "target",
                    "result": "NO_SEND_COMMAND",
                    "message": f"Target {target_name} has no send_command() method — type: {type(target_device).__name__}",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "conditions": condition_results,
                })
                continue

            # --- Check target device availability ---
            target_state = getattr(target_device, 'state', {}) or {}
            target_available = target_state.get('available', True)
            if target_available is False:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "WARNING",
                    "phase": "target",
                    "result": "TARGET_UNAVAILABLE",
                    "message": f"Target {target_name} ({target_ieee}) marked unavailable — attempting anyway",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "conditions": condition_results,
                })

            action = rule["action"]
            command = action["command"]

            # --- Capability sanity check ---
            cap_issue = self._check_target_capability(target_device, command)
            if cap_issue:
                self._add_trace({
                    "rule_id": rule_id,
                    "level": "WARNING",
                    "phase": "capability",
                    "result": "CAPABILITY_WARN",
                    "message": f"Capability concern: {cap_issue} — attempting anyway",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "command": command,
                    "conditions": condition_results,
                })
                # WARNING not ERROR — still attempt the command

            # --- All pre-checks passed ---
            trigger_summary = ", ".join(
                f"{c['attribute']}={c.get('actual_raw', '?')}" for c in condition_results
            )
            self._add_trace({
                "rule_id": rule_id,
                "level": "INFO",
                "phase": "execute",
                "result": "FIRING",
                "message": f"⚡ {source_name} [{trigger_summary}] -> {target_name} {command}={action.get('value')} EP={action.get('endpoint_id')}",
                "source_ieee": source_ieee,
                "target_ieee": target_ieee,
                "command": command,
                "command_value": action.get("value"),
                "endpoint_id": action.get("endpoint_id"),
                "conditions": condition_results,
            })

            # Update cooldown BEFORE execution (prevents double-fire)
            self._cooldowns[rule_id] = now

            # Execute with traced wrapper
            asyncio.create_task(
                self._execute_action_traced(rule, target_device, action, source_ieee, condition_results)
            )

    # =========================================================================
    # EXECUTION WITH TRACING
    # =========================================================================

    async def _execute_action_traced(self, rule: Dict, target_device, action: Dict,
                                     source_ieee: str, condition_results: List[Dict]):
        """
        Execute a rule action via direct ZigBee command with full tracing.
        Captures and reports the command result — success or failure.
        """
        rule_id = rule["id"]
        command = action["command"]
        value = action.get("value")
        endpoint_id = action.get("endpoint_id")
        target_ieee = str(target_device.ieee)
        names = self._get_names()
        target_name = names.get(target_ieee, target_ieee)

        try:
            self._add_trace({
                "rule_id": rule_id,
                "level": "DEBUG",
                "phase": "sending",
                "result": "CALLING",
                "message": f"Calling {target_name}.send_command('{command}', {repr(value)}, endpoint_id={endpoint_id})",
                "source_ieee": source_ieee,
                "target_ieee": target_ieee,
            })

            result = await target_device.send_command(
                command,
                value,
                endpoint_id=endpoint_id
            )

            # Inspect the result to determine success
            if isinstance(result, dict):
                success = result.get("success", True)
                error_detail = result.get("error", None)
                result_repr = repr(result)
            elif result is None:
                # None return is ambiguous — treat as success (many commands return None)
                success = True
                error_detail = None
                result_repr = "None (command returned no result — assumed OK)"
            else:
                success = bool(result)
                error_detail = None if success else f"send_command returned falsy: {repr(result)}"
                result_repr = repr(result)

            if success:
                self._stats["executions"] += 1
                self._stats["execution_successes"] += 1

                self._add_trace({
                    "rule_id": rule_id,
                    "level": "INFO",
                    "phase": "result",
                    "result": "SUCCESS",
                    "message": f"✅ {target_name} {command}={value} EP={endpoint_id} — result: {result_repr}",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "command": command,
                    "command_value": value,
                    "endpoint_id": endpoint_id,
                    "command_result": result_repr,
                })
            else:
                self._stats["executions"] += 1
                self._stats["execution_failures"] += 1

                self._add_trace({
                    "rule_id": rule_id,
                    "level": "ERROR",
                    "phase": "result",
                    "result": "COMMAND_FAILED",
                    "message": f"❌ {target_name} {command}={value} failed — {error_detail} — raw: {result_repr}",
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "command": command,
                    "command_value": value,
                    "endpoint_id": endpoint_id,
                    "command_result": result_repr,
                    "error": error_detail,
                })

            # Emit event for frontend live panel
            if self._event_emitter:
                await self._event_emitter("automation_triggered", {
                    "rule_id": rule_id,
                    "source_ieee": source_ieee,
                    "target_ieee": target_ieee,
                    "target_name": target_name,
                    "command": command,
                    "value": value,
                    "success": success,
                    "error": error_detail,
                    "result": result_repr,
                    "timestamp": time.time(),
                })

        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1

            tb = traceback.format_exc()
            self._add_trace({
                "rule_id": rule_id,
                "level": "ERROR",
                "phase": "exception",
                "result": "EXCEPTION",
                "message": f"💥 Exception: {target_name}.send_command('{command}', {repr(value)}): {e}",
                "source_ieee": source_ieee,
                "target_ieee": target_ieee,
                "command": command,
                "command_value": value,
                "endpoint_id": endpoint_id,
                "error": str(e),
                "traceback": tb,
            })

            logger.error(f"Automation {rule_id} execution exception: {e}", exc_info=True)

            if self._event_emitter:
                try:
                    await self._event_emitter("automation_triggered", {
                        "rule_id": rule_id,
                        "source_ieee": source_ieee,
                        "target_ieee": target_ieee,
                        "command": command,
                        "success": False,
                        "error": str(e),
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass

    # =========================================================================
    # CONDITION HELPERS
    # =========================================================================

    @staticmethod
    def _get_conditions(rule: Dict) -> List[Dict]:
        """
        Get conditions list from a rule, with backward compatibility.
        Legacy rules have 'threshold' (single dict) -> convert to conditions list.
        """
        if "conditions" in rule:
            return rule["conditions"]
        if "threshold" in rule:
            return [rule["threshold"]]
        return []

    def _evaluate_condition(self, actual_value: Any, operator: str, threshold_value: Any) -> bool:
        """
        Evaluate a threshold condition.
        Handles type coercion for boolean and numeric comparisons.
        """
        op_func = OPERATORS.get(operator)
        if not op_func:
            return False

        # Normalise booleans (state may come as string "True"/"False")
        actual = self._normalise_value(actual_value)
        threshold = self._normalise_value(threshold_value)

        try:
            return op_func(actual, threshold)
        except (TypeError, ValueError):
            # Type mismatch - try string comparison as fallback
            return op_func(str(actual), str(threshold))

    @staticmethod
    def _normalise_value(value: Any) -> Any:
        """Normalise a value for comparison."""
        if isinstance(value, str):
            lower = value.lower()
            if lower == "true":
                return True
            if lower == "false":
                return False
            try:
                if "." in value:
                    return float(value)
                return int(value)
            except ValueError:
                return value
        return value

    @staticmethod
    def _check_target_capability(target_device, command: str) -> Optional[str]:
        """
        Sanity-check that the target device likely supports the command.
        Returns a warning string if concern found, None if OK.
        This is a soft check — command is still attempted.
        """
        caps = getattr(target_device, "capabilities", None)
        if not caps:
            return f"Target device has no capabilities object"

        has_cap = getattr(caps, "has_capability", None)
        if not has_cap:
            return f"Target capabilities object has no has_capability method"

        if command in ("on", "off", "toggle"):
            if not (has_cap("on_off") or has_cap("light") or has_cap("switch")):
                return f"No on_off/light/switch capability for '{command}'"
        elif command == "brightness":
            if not (has_cap("level_control") or has_cap("light")):
                return f"No level_control/light capability for 'brightness'"
        elif command == "color_temp":
            if not has_cap("color_control") and not has_cap("light"):
                return f"No color_control/light capability for 'color_temp'"
        elif command in ("open", "close", "stop", "position"):
            if not (has_cap("cover") or has_cap("window_covering")):
                return f"No cover/window_covering capability for '{command}'"

        return None

    @staticmethod
    def _safe_repr(data: Any, max_len: int = 500) -> str:
        """Safe repr for trace data, truncated to avoid bloat."""
        try:
            s = repr(data)
            if len(s) > max_len:
                return s[:max_len] + "..."
            return s
        except Exception:
            return "<repr failed>"

    # =========================================================================
    # HELPER METHODS (for frontend)
    # =========================================================================

    def get_source_attributes(self, ieee: str) -> List[Dict[str, Any]]:
        """
        Get available threshold attributes for a device.
        Returns current state keys with their values and suggested operators.
        """
        devices = self._get_devices()
        if ieee not in devices:
            return []

        device = devices[ieee]
        state = device.state

        # Filter out internal/metadata keys
        skip_keys = {
            "last_seen", "available", "manufacturer", "model",
            "power_source", "lqi", "linkquality"
        }

        attributes = []
        for key, value in state.items():
            if key in skip_keys:
                continue
            if key.endswith("_raw") or key.startswith("attr_"):
                continue

            attr_info = {
                "attribute": key,
                "current_value": value,
                "type": self._classify_value_type(value),
            }

            # Suggest operators based on type
            if isinstance(value, bool):
                attr_info["operators"] = ["eq", "neq"]
            elif isinstance(value, (int, float)):
                attr_info["operators"] = ["eq", "neq", "gt", "lt", "gte", "lte"]
            else:
                attr_info["operators"] = ["eq", "neq"]

            attributes.append(attr_info)

        return sorted(attributes, key=lambda a: a["attribute"])

    def get_target_actions(self, ieee: str) -> List[Dict[str, Any]]:
        """
        Get available actions for a target device.
        Wraps device.get_control_commands() for the frontend.
        """
        devices = self._get_devices()
        if ieee not in devices:
            return []

        device = devices[ieee]
        if hasattr(device, "get_control_commands"):
            return device.get_control_commands()
        return []

    def get_actuator_devices(self) -> List[Dict[str, Any]]:
        """
        Get list of devices that can be automation targets (actuators).
        Filters to devices with on_off, light, switch, cover, or thermostat capabilities.
        """
        devices = self._get_devices()
        names = self._get_names()
        actuators = []

        for ieee, device in devices.items():
            caps = getattr(device, "capabilities", None)
            if not caps:
                continue

            has_cap = getattr(caps, "has_capability", lambda x: False)
            is_actuator = any(has_cap(c) for c in [
                "on_off", "light", "switch", "cover",
                "window_covering", "thermostat", "fan_control"
            ])

            if not is_actuator:
                continue

            # Also verify it's not sensor-only (has input clusters for control)
            if hasattr(caps, "_configurable_endpoints"):
                roles = [ep.get("role") for ep in caps._configurable_endpoints.values()]
                if roles and all(r in ("sensor", "controller") for r in roles):
                    continue

            actuators.append({
                "ieee": ieee,
                "friendly_name": names.get(ieee, ieee),
                "model": getattr(device, "model", "Unknown"),
                "manufacturer": getattr(device, "manufacturer", "Unknown"),
                "commands": device.get_control_commands() if hasattr(device, "get_control_commands") else [],
            })

        return sorted(actuators, key=lambda d: d.get("friendly_name", ""))

    @staticmethod
    def _classify_value_type(value: Any) -> str:
        """Classify a value type for frontend display."""
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "float"
        return "string"

    def get_stats(self) -> Dict[str, Any]:
        """Get automation statistics."""
        return {
            **self._stats,
            "total_rules": len(self.rules),
            "enabled_rules": sum(1 for r in self.rules if r.get("enabled", True)),
            "trace_entries": len(self._trace_log),
        }