# Zigbee Device Debugging & Onboarding Guide

This guide explains how to debug and onboard new Zigbee devices (especially ones not in your current portfolio) using:

- The **ZigbeeDebugger** from `zigbee_debug.py`
- The **cluster handler framework** in `handlers/base.py` and `handlers/__init__.py`
- The existing general / sensors / hvac / tuya handlers as concrete examples

It's written for people who are comfortable editing Python and restarting the backend, but it should also serve as a step-by-step manual for onboarding a completely unknown device.

---

## 1. Architecture Overview

### 1.1 Core pieces

#### 1. ZigbeeDebugger (`zigbee_debug.py`)

A global debugger instance that:

- Captures raw Zigbee packets via `capture_packet(...)`
- Tracks attribute updates, cluster commands, and errors
- Exposes APIs for filtering, summaries, and stats
- Writes a rotating log file at `logs/zigbee_debug.log`

#### 2. Cluster Handler framework (`handlers/base.py` + `handlers/__init__.py`)

- `ClusterHandler` is the base class for all cluster-specific logic.
- `@register_handler(cluster_id)` registers a handler in the global `HANDLER_REGISTRY`.
- `handlers/__init__.py` imports all handler modules (security, sensors, general, hvac, tuya) so their decorators run and the registry is populated.
- `device.py` (not shown here) looks up handlers with `get_handler_for_cluster(cluster_id)` when attaching clusters to devices.

#### 3. Existing handler families

You already have rich coverage:

- **General** (Basic, On/Off, Level Control, Color, Groups, Scenes, Electrical Measurement, Metering)
- **Sensors** (Occupancy, Temperature, Humidity, Illuminance, Pressure, CO₂, PM2.5, Power)
- **HVAC** (Thermostats, TRVs, window covering, fan control)
- **Tuya** manufacturer-specific cluster with DP (Data Point) decoding (radar/mmWave, air quality, switches, valves)

When onboarding a new device, your goal is usually to reuse one of these handlers or add a new one that fits into the same pattern.

---

## 2. Zigbee Debugger Reference

This section is a drop-in replacement / update for `debugging.md`.

### 2.1 What the debugger does

The global debugger lives in `zigbee_debug.py`:

```python
# zigbee_debug.py
debugger = ZigbeeDebugger()

def get_debugger() -> ZigbeeDebugger:
    return debugger
```

Handlers and core code call `get_debugger()` to record events.

The debugger:

- Captures packets via `capture_packet(...)`
- Records handler activity via:
  - `record_attribute_update(...)`
  - `record_cluster_command(...)`
  - `record_error(...)`
- Exposes high-level queries:
  - `get_packets(...)`
  - `get_motion_events(...)`
  - `get_device_summary(ieee)`
  - `get_stats()`
  - `get_log_file_contents(lines)`
  - `clear()`

### 2.2 Enabling / disabling debugging

From any backend code (e.g. your API, startup routine, or REPL):

```python
from zigbee_debug import get_debugger

debugger = get_debugger()

# Enable debugging (logs to zigbee_debug.log by default)
debugger.enable(file_logging=True)

# Optionally disable later
debugger.disable()
```

- `file_logging=True` ensures all important events go into `logs/zigbee_debug.log`.
- You can safely call `enable()` multiple times; it just flips the flag and logs the change.

### 2.3 How packets get into the debugger

`ZigbeeDebugger.capture_packet(...)` is intended to be called from your zigpy / ZHA core whenever a raw ZCL payload is seen:

```python
debugger.capture_packet(
    sender_ieee=str(ieee),
    sender_nwk=nwk,
    profile=profile_id,
    cluster=cluster_id,
    src_ep=src_ep,
    dst_ep=dst_ep,
    message=zcl_payload_bytes,
    direction="RX",  # or "TX"
)
```

The debugger will:

1. Decode the frame control, manufacturer code, TSN and command ID.
2. Map the cluster ID to a human-readable name using `CLUSTER_NAMES`.
3. Try to interpret:
   - Global commands (e.g. Read, Report Attributes)
   - IAS Zone commands (e.g. Zone Status Change → motion, tamper, low battery)
4. Mark packets as "important" (e.g. Zone Status Change, Report Attributes), and log them at WARNING level with the raw data appended.

### 2.4 How handlers integrate with the debugger (automatic)

`ClusterHandler` in `handlers/base.py` is already wired to the debugger:

**`attribute_updated(...)`:**
- Logs the callback (cluster name, attr id, value).
- Calls `debugger.record_attribute_update(...)`.
- Parses the value via `parse_value(...)`.
- Converts attr IDs to friendly names via `get_attr_name(...)`.
- Updates the device state with `{attr_name: parsed_value}`.

**`cluster_command(...)`:**
- Logs the command (TSN, command_id, args).
- Calls `debugger.record_cluster_command(...)`.

**`handle_cluster_request(...)`:**
- Handles cases where the device sends Report Attributes as a cluster request instead of standard attribute reports.
- Calls `attribute_updated(...)` for each embedded attribute.

Because of this, any new handler you write that inherits `ClusterHandler` automatically feeds rich debug info into the debugger.

### 2.5 Filtering & focusing on a single device

`ZigbeeDebugger` exposes two simple filters:

```python
debugger.filter_ieee = "00:11:22:33:44:55:66:77"
debugger.filter_cluster = 0x0406  # Occupancy
```

When set:

- `capture_packet(...)` will ignore packets that don't match the filters.
- You can then inspect:

```python
recent = debugger.get_packets(limit=100)
motion = debugger.get_motion_events(limit=50)
summary = debugger.get_device_summary("00:11:22:33:44:55:66:77")
stats = debugger.get_stats()
```

`get_device_summary(ieee)` gives you a per-device breakdown: total packets, attribute updates, cluster commands, errors, plus recent history.

### 2.6 Using callbacks / streaming to the frontend

For live views, you can register callbacks:

```python
def on_new_packet(packet_dict: dict):
    # e.g. push to WebSocket / SSE
    pass

debugger.add_callback(on_new_packet)
```

For each captured packet, all callbacks are invoked (or `asyncio.create_task` for coroutine callbacks).

This is ideal for powering a live "Zigbee Traffic / Motion Events" UI.

### 2.7 Reading the raw log file

To access the underlying debug log:

```python
text = debugger.get_log_file_contents(lines=200)
print(text)
```

Reads `logs/zigbee_debug.log` and returns the last N lines, or a readable error.

---

## 3. Onboarding a New Device – High-Level Flow

This is the end-to-end flow you should follow whenever you want to support a new Zigbee device (sensor, plug, TRV, Tuya thing, etc.).

### Step 0 – Requirements

You should have:

- Backend running (ZHA/zigpy based).
- Access to the filesystem and Python REPL or logs.
- Ability to restart the backend.
- This repo with `zigbee_debug.py` and the `handlers/` package.

### Step 1 – Enable debugging and clear history

Make sure debugging is enabled:

```python
from zigbee_debug import get_debugger
dbg = get_debugger()
dbg.enable(file_logging=True)
dbg.clear()  # optional, to start with a clean slate
```

Optionally set filters to the joining device once you know its IEEE.

### Step 2 – Pair the new device via ZHA

Pair the device as usual in ZHA (add device mode).

When it joins, ZHA / zigpy will:

1. Discover endpoints and clusters.
2. Instantiate ClusterHandlers for any known cluster IDs (those present in `HANDLER_REGISTRY`).

**Tip:** Your `device.py` (or equivalent) should log something like:

- Device IEEE, NWK
- Endpoints and cluster IDs

Keep an eye on `zigbee_debug.log` and normal application logs during join.

### Step 3 – Identify the important clusters

Use any combination of:

- The ZHA UI / device view
- Your `device.py` logs
- The debug summary:

```python
dbg.get_stats()
dbg.get_packets(limit=100)
dbg.get_device_summary("<device-ieee>")
```

Look for:

- **Standard clusters:**
  `0x0000` (Basic), `0x0006` (On/Off), `0x0402` (Temp), `0x0405` (Humidity), `0x0406` (Occupancy), `0x0201` (Thermostat), etc.

- **IAS / security:**
  `0x0500` (IAS Zone) for door contacts, some motion sensors.

- **Manufacturer-specific:**
  `0xEF00` for Tuya DP-based devices, `0xFC00` for other vendor-specific stuff.

### Step 4 – Check whether handlers already exist

For each cluster ID you see on the device:

From Python:

```python
from handlers import get_handler_for_cluster, print_registered_handlers

handler_cls = get_handler_for_cluster(0x0406)
print(handler_cls)
# Or:
print_registered_handlers()
```

- If a handler exists (e.g. `OccupancySensingHandler` for `0x0406`, `OnOffHandler` for `0x0006`), the device may already work.
- If no handler is registered for some cluster, you'll need to create a new handler (see section 5).

### Step 5 – Exercise the device and watch the debugger

Now you want to understand how the device talks:

1. Trigger motion / open door / press button / change setpoint.
2. Use:

```python
dbg.filter_ieee = "<device-ieee>"
packets = dbg.get_packets(limit=100)
```

Look for:

- Which cluster is being used when the device acts.
- Which command IDs or attributes are changing.

In `zigbee_debug.log`, you will see lines like:

- `Report Attributes` with a list of `{name=value}` if the attributes decode cleanly.
- `Zone Status Change Notification` and `MOTION` / `clear` for IAS Zone.

If you see packets but your UI shows nothing, that means:

- The packets are reaching the debugger, but
- No handler is interpreting them in a way that updates `device.update_state(...)`.

That's the moment to either:

- Adjust an existing handler, or
- Create a dedicated handler.

---

## 4. Understanding the Existing Handler Patterns

### 4.1 "Standard" attribute-driven handler (e.g. Temperature)

Example: `TemperatureMeasurementHandler` (cluster `0x0402`).

Key parts:

**Declares `CLUSTER_ID` and `REPORT_CONFIG`:**

```python
@register_handler(0x0402)
class TemperatureMeasurementHandler(ClusterHandler):
    CLUSTER_ID = 0x0402
    REPORT_CONFIG = [
        ("measured_value", 10, 300, 20),  # min, max, change
    ]
```

**Defines attribute IDs and interprets them:**

```python
ATTR_MEASURED_VALUE = 0x0000

def attribute_updated(self, attrid, value, timestamp=None):
    if attrid == self.ATTR_MEASURED_VALUE:
        if hasattr(value, 'value'):
            value = value.value
        if value is not None and value != 0x8000:
            temp_c = round(float(value) / 100, 2)
            self.device.update_state({"temperature": temp_c})
```

Optionally overrides `get_attr_name` / `parse_value` so the base class can do generic work.

### 4.2 Command-driven handler (e.g. Hue Motion via On/Off)

Philips Hue motion sensors use On/Off cluster command `0x42` (`on_with_timed_off`) instead of simple occupancy attributes. `OnOffHandler` handles this:

It overrides `cluster_command(...)`, and dispatches:

```python
if command_id == self.CMD_ON_WITH_TIMED_OFF:
    self._handle_on_with_timed_off(args)
```

`_handle_on_with_timed_off` then:

1. Extracts `on_time` in 1/10 seconds.
2. Sets:

```python
self.device.update_state({
    "occupancy": True,
    "motion": True,
    "presence": True,
    "motion_detected_at": time.time() * 1000,
    "motion_on_time": on_time_seconds,
    "state": "ON",
    "on": True,
})
```

3. Records a "virtual" attribute update in the debugger so motion shows up nicely in debug views.

This pattern is important for command-based devices (remotes, quirky sensors, etc.).

### 4.3 Tuya DP-based devices

Tuya devices report state via manufacturer-specific DP payloads on cluster `0xEF00`. `TuyaClusterHandler` does the heavy lifting:

Key ideas:

- `_identify_dp_map()` picks the right DP mapping (radar, air quality, valve) based on model/manufacturer strings.
- `_parse_tuya_payload(...)` walks the raw bytes and calls `_process_dp(dp_id, dp_type, dp_data)`.
- `_process_dp` decodes the value, applies converters/scale, and:

```python
self.device.update_state({dp_def.name: value})
```

- Unknown DPs are still logged and stored as `dp_<id>` so you can see them.

For new Tuya devices, you almost always just need to extend the DP mapping tables (`TUYA_RADAR_DPS`, `TUYA_AIR_QUALITY_DPS`, `TUYA_SWITCH_DPS`, `TUYA_VALVE_DPS`).

---

## 5. Creating a New Cluster Handler (Onboarding a New Device Type)

When you discover a cluster ID that doesn't have a handler (or the default behaviour isn't sufficient), create a new handler.

### 5.1 Boilerplate template

Create a new file in `handlers/` (or extend an existing one if it's logically related). Example:

```python
# handlers/my_custom.py
import logging
from typing import Any, Dict, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.my_custom")


@register_handler(0x1234)
class MyCustomClusterHandler(ClusterHandler):
    """
    Handles Custom Cluster (0x1234).
    Describe what this does: e.g. button presses, energy stats, etc.
    """
    CLUSTER_ID = 0x1234
    REPORT_CONFIG = [
        # (attribute_name, min_interval, max_interval, reportable_change)
        # Optionally, you can keep this empty and configure manually in configure()
    ]

    # Attribute IDs (from spec or from zigbee_debug captures)
    ATTR_SOMETHING = 0x0000
    ATTR_OTHER = 0x0001

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        # Option 1: use base behaviour + parse_value/get_attr_name (simpler)
        # Option 2: fully handle here (like OccupancySensingHandler / OnOffHandler)
        try:
            if attrid == self.ATTR_SOMETHING:
                if hasattr(value, 'value'):
                    value = value.value
                # Parse to human units
                parsed = int(value)
                self.device.update_state({"something": parsed})
                logger.info(f"[{self.device.ieee}] Something = {parsed}")

            else:
                # Fallback to base implementation
                super().attribute_updated(attrid, value, timestamp=timestamp)

        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error in MyCustomClusterHandler: {e}")
            # base.ClusterHandler will already record debugger errors for exceptions
```

If you need custom command handling:

```python
    def cluster_command(self, tsn: int, command_id: int, args):
        super().cluster_command(tsn, command_id, args)  # keep debug logging

        if command_id == 0x01:
            # handle some command
            ...
        else:
            logger.debug(f"[{self.device.ieee}] Unhandled custom cmd 0x{command_id:02X}")
```

Because `ClusterHandler.cluster_command` already logs and records the command with the debugger, calling `super()` first ensures you don't lose those debug events.

### 5.2 Enabling polling and configure reporting

To poll attributes periodically:

```python
    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_SOMETHING: "something",
            self.ATTR_OTHER: "other",
        }
```

The base `configure()` and `poll()` logic in `ClusterHandler` uses:

- `REPORT_CONFIG` to bind and configure reporting.
- `get_pollable_attributes()` to read values directly.

You can override `configure()` if the default doesn't match the device behaviour.

### 5.3 Wiring in a new module

If you add a new file under `handlers/`, make sure it is imported in `handlers/__init__.py` so the decorators run:

```python
# handlers/__init__.py
from . import security
from . import sensors
from . import general
from . import hvac
from . import tuya
from . import my_custom   # <-- add this
```

Otherwise, `@register_handler` will never execute and your handler won't be visible to `get_handler_for_cluster(...)`.

**Restart the backend after adding/importing the new module.**

---

## 6. Onboarding Workflow – Practical Checklist

Here's the condensed checklist you can follow for each new device model:

1. **Enable debugging & clear old data:**
   - `dbg.enable(file_logging=True)`
   - `dbg.clear()`

2. **Pair the device in ZHA:**
   - Note its IEEE address.
   - Confirm endpoints and cluster IDs in logs.

3. **Set debugger filters to focus:**
   - `dbg.filter_ieee = "<device-ieee>"`

4. **Exercise the device:**
   - Trigger all relevant behaviours (motion, button presses, temperature changes, etc.).

5. **Look at `dbg.get_packets(...)` and `zigbee_debug.log`:**
   - Identify which clusters and commands/attributes change.

6. **Map to handlers:**
   - For each cluster ID, call `get_handler_for_cluster(cluster_id)`:
     - If handler exists → inspect its code (`general.py`, `sensors.py`, `hvac.py`, `tuya.py`).
     - If none exists → create new handler as in section 5.

7. **Adjust or extend handlers:**
   - For standard measurement clusters, add/adjust `attribute_updated` and `REPORT_CONFIG`.
   - For command-driven devices, override `cluster_command` and pattern after `OnOffHandler`.
   - For Tuya devices, extend the relevant DP table to give friendly names and units to new DP IDs.

8. **Restart backend and re-test:**
   - Confirm that:
     - Device state is updated correctly (`device.update_state(...)`).
     - Debugger stats show handler triggers and attribute reports.

9. **Verify in the front end:**
   - Ensure the fields you update (`temperature`, `humidity`, `occupancy`, etc.) match what your UI expects.

---

## 7. Examples of Common Onboarding Scenarios

### 7.1 New occupancy / motion sensor

**Typical clusters:**

- `0x0406` (Occupancy) – standard occupancy attribute `0x0000`.
- `0x0500` (IAS Zone) – Zone Status Change notifications.
- Some Philips Hue motion sensors also send motion via On/Off cluster command `0x42` (`on_with_timed_off`).

**Steps:**

1. Trigger motion and check debugger packets for clusters `0x0406`, `0x0500`, `0x0006`.

2. If packets are seen:
   - **Occupancy attributes:** ensure `OccupancySensingHandler` is mapped and updating `occupancy` / `motion` / `presence`.
   - **IAS Zone:** make sure your `IASZoneHandler` (in `security.py`) interprets zone status as motion / open / closed.
   - **On/Off 0x42:** `OnOffHandler` already treats this as motion; replicate pattern for new sensors if needed.

3. If no standard cluster is used, create a new handler for the actual cluster.

### 7.2 New Tuya radar / presence sensor

**Typical pattern:**

- Uses cluster `0xEF00` with DP payloads.
- `TuyaClusterHandler` will try to pick the right DP map based on model/manufacturer.

**Steps:**

1. Confirm cluster `0xEF00` is present.

2. Trigger presence and inspect `zigbee_debug.log` / debugger to see DP IDs and values.

3. If you see unknown `dp_<id>` fields in device state, add them to the mapping table:

```python
TUYA_RADAR_DPS[123] = TuyaDP(123, "new_metric", scale=1, unit="", type=TuyaClusterHandler.DP_TYPE_VALUE)
```

4. Restart backend, re-test, and ensure new keys show in the state (and thus in your frontend).

### 7.3 New thermostat / TRV

**Typical cluster:** `0x0201` (Thermostat), optionally `0x0202` (Fan Control).

1. Confirm cluster `0x0201` is present.

2. Trigger setpoint changes via device and via UI.

3. Use debugger to see which attributes change (e.g. `local_temperature`, `occupied_heating_setpoint`).

4. Adjust `ThermostatHandler` if required:
   - Additional attributes
   - Different units or scaling
   - Vendor-specific behaviour

---

## 8. Summary

- The **ZigbeeDebugger** gives you a full picture of what packets and handler events occur for any device.

- The **ClusterHandler framework** standardises how you (configure, parse, and update state for) each cluster.

- **Onboarding a new device** is primarily about:
  1. **Observing:** using the debugger to see which clusters / commands / attributes it uses.
  2. **Mapping:** creating or extending handlers so those messages become clean, named fields on the device state.
  3. **Verifying:** ensuring the UI responds as expected, and the debugger shows healthy stats and minimal errors.
