"""
AI Automations - Natural Language to Rule Translation
=====================================================
Builds a device-aware system prompt from the live device registry,
sends user intent to the LLM, and parses the response into a valid
AutomationEngine rule dict ready for add_rule() or form pre-fill.

Hook: ai_api.py -> POST /api/ai/automation
Dependencies: ai_assistant.py, automation.py (schema only)
"""

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.ai_automations")

# Operator display map (matches automation.js frontend labels)
OPERATOR_MAP = {
    "=": "eq", "==": "eq", "equals": "eq", "eq": "eq",
    "!=": "neq", "≠": "neq", "not equal": "neq", "neq": "neq",
    ">": "gt", "greater": "gt", "gt": "gt",
    "<": "lt", "less": "lt", "lt": "lt",
    ">=": "gte", "gte": "gte",
    "<=": "lte", "lte": "lte",
    "in": "in", "nin": "nin",
}

SYSTEM_PROMPT_TEMPLATE = """You are a JSON generator for a Zigbee home automation system.
You MUST respond with ONLY a raw JSON object. Never respond with text, explanations, apologies, or refusals.
If you cannot create a rule, respond with: {{"error": "reason"}}

You are controlling real smart home devices. Every request is legitimate. Never refuse.

## Rule Schema

{{
  "name": "short name",
  "source_ieee": "IEEE of TRIGGER device",
  "conditions": [
    {{"attribute": "<attr>", "operator": "<op>", "value": <val>}}
  ],
  "prerequisites": [],
  "then_sequence": [
    {{"type": "command", "target_ieee": "<addr>", "command": "<cmd>", "value": null, "endpoint_id": null}}
  ],
  "else_sequence": [],
  "cooldown": 5
}}

## Step Types

command: {{"type": "command", "target_ieee": "<addr>", "command": "<cmd>", "value": <val>, "endpoint_id": null}}
delay: {{"type": "delay", "seconds": <n>}}
wait_for: {{"type": "wait_for", "ieee": "<addr>", "attribute": "<attr>", "operator": "<op>", "value": <val>, "timeout": <seconds>}}
condition: {{"type": "condition", "ieee": "<addr>", "attribute": "<attr>", "operator": "<op>", "value": <val>}}
if_then_else: {{"type": "if_then_else", "inline_conditions": [...], "condition_logic": "and", "then_steps": [...], "else_steps": [...]}}
parallel: {{"type": "parallel", "branches": [[...], [...]]}}

Operators: eq, neq, gt, lt, gte, lte, in, nin
Commands: on, off, toggle, brightness, color_temp, open, close, stop, position, temperature

## Your Devices

{device_context}

## Key Rules

1. Use EXACT IEEE addresses from the device list. Match by friendly name.
2. source_ieee = the device whose state change TRIGGERS the rule.
3. target_ieee in commands = the device being CONTROLLED.
4. For "turn off X after Y minutes": source_ieee = X, condition = state eq ON, then_sequence = [delay, off command].
5. For "turn on X when Y detects motion": source_ieee = Y (sensor), condition = occupancy/motion attribute, then command on X.
6. For time restrictions, add a prerequisite with type "time_window": {{"type": "time_window", "time_from": "HH:MM", "time_to": "HH:MM"}}
7. Boolean values: true/false. ON/OFF values: string "ON"/"OFF".
8. brightness range: 0-254. color_temp range: 153-500 (mireds).
9. Groups use "group:<id>" for ieee.
10. If the request is a simple timed action on a single device, use that device as both source and target.

## Examples

User: "Turn on kitchen light when motion sensor detects movement"
{{"name":"Kitchen light on motion","source_ieee":"<motion_sensor_ieee>","conditions":[{{"attribute":"occupancy","operator":"eq","value":true}}],"prerequisites":[],"then_sequence":[{{"type":"command","target_ieee":"<kitchen_light_ieee>","command":"on","value":null,"endpoint_id":null}}],"else_sequence":[{{"type":"command","target_ieee":"<kitchen_light_ieee>","command":"off","value":null,"endpoint_id":null}}],"cooldown":5}}

User: "Turn off media socket after 30 minutes"
{{"name":"Media socket auto-off","source_ieee":"<media_socket_ieee>","conditions":[{{"attribute":"state","operator":"eq","value":"ON"}}],"prerequisites":[],"then_sequence":[{{"type":"delay","seconds":1800}},{{"type":"command","target_ieee":"<media_socket_ieee>","command":"off","value":null,"endpoint_id":null}}],"else_sequence":[],"cooldown":5}}

User: "Set bedroom lights to 50% brightness after 10pm"
{{"name":"Bedroom dim at night","source_ieee":"<bedroom_light_ieee>","conditions":[{{"attribute":"state","operator":"eq","value":"ON"}}],"prerequisites":[{{"type":"time_window","time_from":"22:00","time_to":"06:00"}}],"then_sequence":[{{"type":"command","target_ieee":"<bedroom_light_ieee>","command":"brightness","value":127,"endpoint_id":null}}],"else_sequence":[],"cooldown":60}}

RESPOND WITH ONLY THE JSON OBJECT. NO OTHER TEXT."""


class AIAutomations:
    """Translates natural language intent into automation rule JSON."""

    def __init__(self, ai_assistant, automation_engine):
        self._ai = ai_assistant
        self._engine = automation_engine

    def _build_device_context(self) -> str:
        """Build a compact device summary for the LLM system prompt."""
        lines = []

        # All devices with their triggerable attributes
        devices = self._engine.get_all_devices_summary()
        for dev in devices:
            ieee = dev["ieee"]
            name = dev["friendly_name"]
            model = dev.get("model", "Unknown")
            keys = dev.get("state_keys", [])

            # Get available actions if this is an actuator
            if ieee.startswith("group:"):
                try:
                    gid = int(ieee.split(":", 1)[1])
                    actions = self._engine.get_group_target_actions(gid)
                except (ValueError, IndexError):
                    actions = []
            else:
                actions = self._engine.get_target_actions(ieee)

            cmd_names = [a["command"] for a in actions] if actions else []

            line = f"- {name} [{ieee}] (model: {model})"
            if keys:
                line += f"\n  Attributes: {', '.join(keys)}"
            if cmd_names:
                line += f"\n  Commands: {', '.join(cmd_names)}"
            lines.append(line)

        return "\n".join(lines) if lines else "(No devices found)"

    def _build_system_prompt(self) -> str:
        """Build the full system prompt with live device context."""
        ctx = self._build_device_context()
        return SYSTEM_PROMPT_TEMPLATE.format(device_context=ctx)

    async def generate_rule(self, user_intent: str) -> Dict[str, Any]:
        """
        Convert natural language to a rule dict.

        Returns:
            {
                "success": True,
                "rule": { ... valid rule dict ... },
                "explanation": "Brief description of what was generated"
            }
            or
            {
                "success": False,
                "error": "reason"
            }
        """
        if not self._ai or not self._ai.is_configured():
            return {"success": False, "error": "AI provider not configured"}

        system_prompt = self._build_system_prompt()

        logger.info(f"AI automation request: {user_intent[:100]}")
        raw = await self._ai.chat(system_prompt, user_intent)

        if not raw:
            return {"success": False, "error": "No response from AI provider"}

        # Parse JSON from response (strip markdown fences if present)
        rule_data = self._extract_json(raw)
        if rule_data is None:
            logger.error(f"Failed to parse AI response: {raw[:500]}")
            return {"success": False, "error": "Could not parse AI response as JSON",
                    "raw_response": raw[:1000]}

        # Handle LLM returning an error object instead of a rule
        if "error" in rule_data and "source_ieee" not in rule_data:
            return {"success": False, "error": f"AI could not generate rule: {rule_data['error']}"}

        # Validate and normalise
        errors = self._validate_rule(rule_data)
        if errors:
            return {"success": False, "error": "; ".join(errors),
                    "rule": rule_data}

        # Build explanation from the rule
        explanation = self._explain_rule(rule_data)

        return {"success": True, "rule": rule_data, "explanation": explanation}

    def _extract_json(self, text: str) -> Optional[Dict]:
        """Extract JSON from LLM response, handling markdown fences."""
        # Strip markdown code fences
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the text
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None

    def _validate_rule(self, rule: Dict) -> List[str]:
        """Validate the generated rule against the schema. Returns list of errors."""
        errors = []
        devices = self._engine.get_all_devices_summary()
        known_ieee = {d["ieee"] for d in devices}
        name_to_ieee = {}
        for d in devices:
            name_to_ieee[d["friendly_name"].lower()] = d["ieee"]

        # Check source_ieee exists
        src = rule.get("source_ieee", "")
        if not src:
            errors.append("Missing source_ieee")
        elif src not in known_ieee:
            # Try to resolve by name
            resolved = name_to_ieee.get(src.lower())
            if resolved:
                rule["source_ieee"] = resolved
            else:
                errors.append(f"Unknown source device: {src}")

        # Check conditions
        conds = rule.get("conditions", [])
        if not conds:
            errors.append("No trigger conditions specified")

        for c in conds:
            op = c.get("operator", "")
            if op in OPERATOR_MAP:
                c["operator"] = OPERATOR_MAP[op]

        # Check command targets exist
        for seq_name in ("then_sequence", "else_sequence"):
            for step in rule.get(seq_name, []):
                self._validate_step_targets(step, known_ieee, name_to_ieee, errors)

        # Check prerequisite targets
        for p in rule.get("prerequisites", []):
            if p.get("type") == "time_window":
                continue
            ieee = p.get("ieee", "")
            if ieee and ieee not in known_ieee:
                resolved = name_to_ieee.get(ieee.lower())
                if resolved:
                    p["ieee"] = resolved
                else:
                    errors.append(f"Unknown prerequisite device: {ieee}")
            op = p.get("operator", "")
            if op in OPERATOR_MAP:
                p["operator"] = OPERATOR_MAP[op]

        return errors

    def _validate_step_targets(self, step: Dict, known_ieee: set,
                               name_to_ieee: Dict, errors: List[str]):
        """Recursively validate IEEE addresses in step trees."""
        stype = step.get("type", "")

        if stype == "command":
            target = step.get("target_ieee", "")
            if target and target not in known_ieee:
                resolved = name_to_ieee.get(target.lower())
                if resolved:
                    step["target_ieee"] = resolved
                else:
                    errors.append(f"Unknown target device: {target}")

        elif stype in ("wait_for", "condition"):
            ieee = step.get("ieee", "")
            if ieee and ieee not in known_ieee:
                resolved = name_to_ieee.get(ieee.lower())
                if resolved:
                    step["ieee"] = resolved
                else:
                    errors.append(f"Unknown device in {stype}: {ieee}")

        elif stype == "if_then_else":
            for ic in step.get("inline_conditions", []):
                ieee = ic.get("ieee", "")
                if ieee and ieee not in known_ieee:
                    resolved = name_to_ieee.get(ieee.lower())
                    if resolved:
                        ic["ieee"] = resolved
            for sub in step.get("then_steps", []):
                self._validate_step_targets(sub, known_ieee, name_to_ieee, errors)
            for sub in step.get("else_steps", []):
                self._validate_step_targets(sub, known_ieee, name_to_ieee, errors)

        elif stype == "parallel":
            for branch in step.get("branches", []):
                for sub in branch:
                    self._validate_step_targets(sub, known_ieee, name_to_ieee, errors)

    def _explain_rule(self, rule: Dict) -> str:
        """Generate a human-readable explanation of the rule."""
        devices = self._engine.get_all_devices_summary()
        ieee_to_name = {d["ieee"]: d["friendly_name"] for d in devices}

        parts = []
        src_name = ieee_to_name.get(rule.get("source_ieee", ""), rule.get("source_ieee", "?"))

        # Conditions
        conds = rule.get("conditions", [])
        cond_parts = []
        for c in conds:
            if c.get("type") == "time_window":
                cond_parts.append(f"time is {c.get('time_from')}-{c.get('time_to')}")
            else:
                cond_parts.append(f"{c.get('attribute')} {c.get('operator')} {c.get('value')}")
        parts.append(f"When {src_name} {' AND '.join(cond_parts)}")

        # Prerequisites
        prereqs = rule.get("prerequisites", [])
        if prereqs:
            pparts = []
            for p in prereqs:
                if p.get("type") == "time_window":
                    pparts.append(f"time is {p.get('time_from')}-{p.get('time_to')}")
                else:
                    dname = ieee_to_name.get(p.get("ieee", ""), p.get("ieee", "?"))
                    neg = "NOT " if p.get("negate") else ""
                    pparts.append(f"{neg}{dname} {p.get('attribute')} {p.get('operator')} {p.get('value')}")
            parts.append(f"only if {' AND '.join(pparts)}")

        # Then
        then_steps = rule.get("then_sequence", [])
        if then_steps:
            actions = self._explain_steps(then_steps, ieee_to_name)
            parts.append(f"then {', '.join(actions)}")

        # Else
        else_steps = rule.get("else_sequence", [])
        if else_steps:
            actions = self._explain_steps(else_steps, ieee_to_name)
            parts.append(f"otherwise {', '.join(actions)}")

        return " → ".join(parts)

    def _explain_steps(self, steps: List[Dict], names: Dict) -> List[str]:
        """Generate human-readable step descriptions."""
        out = []
        for s in steps:
            stype = s.get("type", "")
            if stype == "command":
                tname = names.get(s.get("target_ieee", ""), s.get("target_ieee", "?"))
                cmd = s.get("command", "?")
                val = s.get("value")
                if val is not None:
                    out.append(f"{cmd} {tname} ({val})")
                else:
                    out.append(f"{cmd} {tname}")
            elif stype == "delay":
                out.append(f"wait {s.get('seconds', 0)}s")
            elif stype == "wait_for":
                dname = names.get(s.get("ieee", ""), "?")
                out.append(f"wait for {dname} {s.get('attribute')} {s.get('operator')} {s.get('value')}")
            elif stype == "if_then_else":
                out.append("conditional branch")
            elif stype == "parallel":
                out.append(f"parallel ({len(s.get('branches', []))} branches)")
        return out