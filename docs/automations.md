# ZigBee Manager — Automation Engine

## Overview

The automation engine provides state-machine-based triggers with recursive action sequences, executing directly at the ZigBee gateway level with zero MQTT delay. Rules evaluate device attribute changes in real time and fire ordered sequences of commands, delays, waits, gates, branching logic, and parallel execution.

![Automation tab overview showing rule list with state badges and action buttons](./images/automation-tab-overview.png)

---

## Core Concepts

### State Machine

Rules track **matched/unmatched** state and only fire on transitions — not on every matching update.

| Previous State    | New State | Action                  |
|-------------------|-----------|-------------------------|
| unmatched         | matched   | Run **THEN** sequence   |
| matched           | unmatched | Run **ELSE** sequence   |
| matched           | matched   | Nothing (still matched) |
| unmatched         | unmatched | Nothing                 |
| init (first eval) | matched   | Run **THEN**            |
| init (first eval) | unmatched | Nothing                 |

![State machine diagram showing transitions between init, matched, and unmatched states](./images/state-machine-diagram.png)

### Rule Structure

Every automation rule consists of four parts:

1. **Trigger Conditions** — attribute checks on the source device (AND logic, up to 5)
2. **Prerequisites** — optional state checks on other devices before firing (supports NOT)
3. **THEN Sequence** — action steps when conditions become true
4. **ELSE Sequence** — action steps when conditions become false

---

## Creating a Rule

Click **Add Rule** on the Automation tab to open the rule builder.

![Add Rule form showing empty condition, prerequisite, and sequence builders](./images/add-rule-form.png)

### Step 1: Trigger Conditions

Conditions evaluate attributes on the source device. Multiple conditions are combined with AND logic. Each condition specifies an attribute, operator, and threshold value.

![Condition builder with IF/AND badges, attribute dropdown, operator, and value fields](./images/condition-builder.png)

**Supported Operators:**

| Symbol            | Meaning                   |
|-------------------|---------------------------|
| `=`               | equals                    |
| `≠`               | not equal                 |
| `>` `<` `>=` `<=` | numeric comparisons       |
| `∈`               | in list (comma-separated) |
| `∉`               | not in list               |

**Sustain** — optional hold timer (seconds). The condition must remain true for the specified duration before triggering.

### Step 2: Prerequisites (Optional)

Prerequisites check the current state of **other devices** before the rule fires. These support a **NOT** flag to negate the check.

![Prerequisite builder with CHECK badge, NOT checkbox, device picker, and attribute fields](./images/prerequisite-builder.png)

Example: Only fire if the hallway light is currently OFF.

### Step 3: THEN Sequence

Action steps that execute when conditions transition from unmatched → matched.

![THEN sequence builder with Command, Delay, Wait, Gate, If/Then/Else, and Parallel buttons](./images/then-sequence-builder.png)

### Step 4: ELSE Sequence

Action steps that execute when conditions transition from matched → unmatched.

![ELSE sequence builder with a delay step followed by a command step](./images/else-sequence-builder.png)

---

## Step Types

### Command

Sends a ZigBee command to a target device. Select the target, command, and optional value. Endpoint is auto-detected.

![Command step showing target device dropdown, command dropdown, and value input](./images/step-command.png)

### Delay

Pauses the sequence for a specified number of seconds.

![Delay step with seconds input field](./images/step-delay.png)

### Wait For

Pauses until a device attribute matches a condition, with a configurable timeout. If the timeout expires, the sequence stops.

![Wait For step with device picker, attribute, operator, value, and timeout fields](./images/step-wait-for.png)

### Gate

An inline condition check that stops the sequence if the condition is false. Supports NOT for negation.

![Gate step with NOT checkbox, device picker, attribute, operator, and value](./images/step-gate.png)

### If / Then / Else (Branching)

Evaluates one or more inline conditions and branches into nested THEN or ELSE paths. When a single condition is used, the AND/OR selector is hidden for a clean simple IF. Adding a second condition reveals the AND/OR logic toggle.

![If/Then/Else step with single inline condition, nested THEN and ELSE sequences](./images/step-if-then-else-single.png)

![If/Then/Else step with multiple inline conditions and AND/OR toggle visible](./images/step-if-then-else-multi.png)

Each inline condition supports NOT negation, device selection, attribute, operator, and value — identical to prerequisites but evaluated inline during sequence execution.

### Parallel

Executes two or more branches concurrently. All branches run simultaneously and the step completes when all branches finish.

![Parallel step with Branch 1 and Branch 2 containers, each with their own step builders](./images/step-parallel.png)

Additional branches can be added with the **+ Branch** button.

---

## Rule Card Display

Each saved rule displays as a card showing conditions, prerequisites, sequence summaries, and state.

![Rule card showing IF/AND conditions, CHECK prerequisites, THEN/ELSE summaries, and action buttons](./images/rule-card.png)

**State Badges:**

| Badge               | Meaning                     |
|---------------------|-----------------------------|
| `matched` (green)   | Conditions currently true   |
| `unmatched` (grey)  | Conditions currently false  |
| `init` (dark)       | Not yet evaluated           |
| `⏳` (yellow)        | Sequence currently running  |

**Action Buttons:**

| Button | Action                               |
|--------|--------------------------------------|
| 🔍     | Open trace log filtered to this rule |
| ✏️     | Edit the rule                        |
| ⏻      | Enable / disable                     |
| 🗑️    | Delete the rule                      |
| ⬇️     | Download rule as JSON                |

---

## JSON Export

Each rule can be downloaded as a JSON file via the download button on the rule card. The exported file contains the complete rule definition including conditions, prerequisites, and both sequences — useful for backup, sharing, or importing into another instance.

![Download button on rule card and example JSON file](./images/json-download.png)

---

## Trace Log

The trace log shows real-time evaluation history for debugging automation behaviour. Open it via the **Trace** button.

![Trace log panel with timestamp, phase badges, result badges, and condition evaluation details](./images/trace-log.png)

**Result Colours:**

| Colour  | Results                                                                  |
|---------|--------------------------------------------------------------------------|
| Green   | SUCCESS, FIRING, COMPLETE, WAIT_MET, GATE_PASS, IF_TRUE, PARALLEL_DONE   |
| Red     | FAIL, ERROR, EXCEPTION, MISSING, CMD_FAIL                                |
| Yellow  | BLOCKED, SUSTAIN_WAIT, DELAY, WAITING                                    |
| Blue    | CANCELLED, WAIT_TIMEOUT, IF_FALSE                                        |

Filter by a specific rule using the dropdown, or select **System** to see engine-level events.

---

## Example: Door Contact Light

A practical example — turn on a light when a door opens in low light, turn it off 5 seconds after the door closes.

**Conditions:**
- IF `contact` = `open`
- AND `illuminance` < `11`

**THEN:**
- ⚡ Command → Hall Light → ON

**ELSE:**
- ⏱ Delay → 5 seconds
- ⚡ Command → Hall Light → OFF

---

## Example: Branching with If/Then/Else

A more advanced example using inline branching — when motion is detected, check time of day and set appropriate brightness.

**Conditions:**
- IF `occupancy` = `true`

**THEN:**
- If/Then/Else:
    - IF Kitchen Light `brightness` < `50`
        - THEN: ⚡ Kitchen Light → brightness = 255
        - ELSE: ⚡ Kitchen Light → brightness = 128

---

## Tips

- **Cooldown** prevents rapid re-firing. Set it based on how quickly your sensor re-triggers (motion sensors: 5-10s, contact sensors: 1-2s).
- **Prerequisites** let you create context-aware rules without duplicating conditions across multiple rules.
- **Gates** are useful mid-sequence to bail out if conditions have changed since the sequence started.
- **Wait For** is ideal for confirming a command took effect before proceeding.
- **Parallel** lets you command multiple devices simultaneously rather than sequentially.
- **JSON export** is your backup safety net — download rules before making major changes.