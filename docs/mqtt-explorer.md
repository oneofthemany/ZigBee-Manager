# MQTT Explorer Wiki

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Getting Started](#getting-started)
- [User Interface Guide](#user-interface-guide)
- [Usage Examples](#usage-examples)
- [Advanced Features](#advanced-features)
- [Troubleshooting](#troubleshooting)
- [Best Practices](#best-practices)
- [FAQ](#faq)

---

## Overview

The **MQTT Explorer** is a powerful debugging and monitoring tool integrated into the Zigbee Manager Gateway. It provides real-time visibility into all MQTT traffic flowing through your broker, making it an essential tool for:

- 🔍 **Debugging device communication** - See exactly what messages devices are sending and receiving
- 📊 **Monitoring Home Assistant integration** - Verify MQTT discovery and command topics
- 🧪 **Testing MQTT messages** - Publish test messages to any topic
- 📈 **Analyzing traffic patterns** - View statistics on message rates and topic usage
- 🐛 **Troubleshooting connectivity** - Identify missing messages or incorrect payloads

### Architecture

The MQTT Explorer uses a **separate MQTT client** that subscribes to all topics (`#` wildcard) without interfering with your main gateway operations. Messages are captured in a **circular buffer** (1000 messages by default) and streamed to the frontend via **WebSocket** for real-time updates.

```
┌─────────────────┐
│   MQTT Broker   │
│  (Mosquitto)    │
└────────┬────────┘
         │
    ┌────┴─────────────┐
    │                  │
┌───▼──────────┐  ┌───▼──────────┐
│ Main Gateway │  │   Explorer   │
│ MQTT Client  │  │ MQTT Client  │
│              │  │ (Monitor)    │
└──────────────┘  └───┬──────────┘
                      │
                  ┌───▼─────────┐
                  │  Circular   │
                  │   Buffer    │
                  │ (1000 msgs) │
                  └───┬─────────┘
                      │
                  ┌───▼─────────┐
                  │  WebSocket  │
                  │  Broadcast  │
                  └───┬─────────┘
                      │
                  ┌───▼─────────┐
                  │  Frontend   │
                  │  Real-time  │
                  └─────────────┘
```

---

## Features

### Real-Time Monitoring

- ✅ **Live message capture** - All MQTT traffic displayed instantly
- ✅ **Zero polling** - WebSocket-based updates for minimal latency
- ✅ **Non-intrusive** - Separate client doesn't affect gateway performance
- ✅ **Memory bounded** - Circular buffer prevents memory leaks

### Smart Filtering

- ✅ **Topic patterns** - MQTT wildcard support (`+` and `#`)
- ✅ **Payload search** - Case-insensitive text search
- ✅ **Live filtering** - Apply filters without stopping monitoring
- ✅ **Regex support** - Advanced pattern matching (planned)

### Message Inspection

- ✅ **Click to view** - Full message details in modal
- ✅ **JSON formatting** - Pretty-printed JSON payloads
- ✅ **Binary detection** - Identifies non-text data
- ✅ **Property display** - QoS, retain flag, size, timestamp

### Message Publishing

- ✅ **Test messages** - Send to any topic
- ✅ **QoS control** - Select 0, 1, or 2
- ✅ **Retain flag** - Mark messages as retained
- ✅ **JSON validation** - Automatic format checking

### Statistics & Analytics

- ✅ **Message counters** - Total messages captured
- ✅ **Rate monitoring** - Messages per second
- ✅ **Topic tracking** - Unique topics discovered
- ✅ **Buffer usage** - Memory consumption display

---

## Getting Started

### Prerequisites

✅ MQTT broker running (e.g., Mosquitto)  
✅ Zigbee Gateway installed and operational  
✅ MQTT Explorer integrated (see [Integration Guide](MQTT_EXPLORER_INTEGRATION.md))

### Accessing MQTT Explorer

1. Open your Zigbee Manager web interface: `http://YOUR_IP:8000`
2. Click the **"MQTT Explorer"** tab in the navigation bar
3. The interface will load with monitoring stopped (default state)

![Screenshot: MQTT Explorer Tab Location](screenshots/mqtt-explorer-tab.png)

> **Screenshot Description:** The navigation bar shows tabs for Devices, Topology, Settings, Groups, Debug Log, and the newly added **MQTT Explorer** tab with a broadcast tower icon (📡). The tab is positioned after Debug Log.

---

## User Interface Guide

### Main Interface Layout

The MQTT Explorer interface is divided into two main columns:

![Screenshot: MQTT Explorer Main Interface](screenshots/mqtt-explorer-main.png)

> **Screenshot Description:**
> - **Left column (70%)** - Message list with live updates
> - **Right column (30%)** - Publish form and help information
> - **Header** - Control buttons and status badge
> - **Statistics bar** - Four metric cards showing real-time data

### 1. Control Bar

Located at the top of the left column:

![Screenshot: Control Bar](screenshots/mqtt-control-bar.png)

> **Screenshot Description:** The control bar displays:
> - **Title**: "MQTT Messages" with a status badge
> - **Status Badge**: Shows "Stopped" (gray) or "Monitoring" (green)
> - **Button Group**:
    >   - ▶️ **Start** button (green) - Begins monitoring
>   - ⏹️ **Stop** button (red) - Stops monitoring
>   - 🔄 **Refresh** button (blue) - Manually reloads messages
>   - 🗑️ **Clear** button (yellow) - Clears the buffer

### 2. Statistics Dashboard

Four metric cards displaying real-time statistics:

![Screenshot: Statistics Cards](screenshots/mqtt-statistics.png)

> **Screenshot Description:** Four cards in a row showing:
>
> **Total Messages**
> - Large number display (e.g., "1,247")
> - Gray "Total Messages" label
>
> **Msg/sec**
> - Decimal display (e.g., "23.45")
> - Gray "Msg/sec" label
>
> **Topics**
> - Integer display (e.g., "87")
> - Gray "Topics" label
>
> **Buffer**
> - Usage display (e.g., "523 / 1000")
> - Gray "Buffer" label
> - Shows current vs maximum capacity

### 3. Filter Controls

Powerful filtering options to narrow down messages:

![Screenshot: Filter Controls](screenshots/mqtt-filters.png)

> **Screenshot Description:** Three input fields in a row:
>
> **Topic Filter** (left, 40% width)
> - Text input with placeholder: "Filter by topic (supports + and # wildcards)"
> - Example: `zigbee/+/state`
> - Monospace font for readability
>
> **Search Filter** (middle, 40% width)
> - Text input with placeholder: "Search in topic or payload..."
> - Case-insensitive search
> - Searches both topic strings and payload content
>
> **Auto-Scroll Checkbox** (right, 20% width)
> - Checkbox labeled "Auto-scroll"
> - Checked by default
> - When enabled, keeps newest messages visible

### 4. Message Table

The main display showing captured MQTT messages:

![Screenshot: Message Table](screenshots/mqtt-message-table.png)

> **Screenshot Description:** A table with 5 columns:
>
> **Time** (120px)
> - Format: `HH:MM:SS.mmm` (e.g., "14:23:45.123")
> - Monospace font
> - Millisecond precision
>
> **Topic** (250px)
> - Blue badge displaying full topic
> - Truncates with ellipsis if too long
> - Hover shows full topic
> - Example: `zigbee/Living_Room_Light/state`
>
> **Payload** (flexible)
> - Monospace font
> - Truncated to 100 characters with "..."
> - JSON objects show inline
> - Example: `{"state":"ON","brightness":254}`
>
> **QoS/Retain** (100px, centered)
> - QoS badge: Gray (0), Blue (1), Yellow (2)
> - Small yellow "R" badge if retained
> - Example: `QoS 1` with `R`
>
> **Size** (80px, right-aligned)
> - Byte count with unit
> - Examples: `45B`, `1.23KB`
> - Small gray text

#### Row States

Messages appear with visual feedback:

![Screenshot: Message Row States](screenshots/mqtt-row-states.png)

> **Screenshot Description:** Three message rows showing:
>
> **Normal Row**
> - White background
> - Standard text color
> - Example: Device state update
>
> **Hover State**
> - Light blue background (rgba(0, 123, 255, 0.05))
> - Cursor changes to pointer
> - Indicates clickable
>
> **Recent Message** (if implemented)
> - Slight highlight/fade animation
> - Helps spot new messages
> - Fades to normal after 2 seconds

### 5. Message Details Modal

Click any message row to view full details:

![Screenshot: Message Details Modal](screenshots/mqtt-message-modal.png)

> **Screenshot Description:** A Bootstrap modal dialog showing:
>
> **Header**
> - Title: "📧 Message Details"
> - Topic displayed in monospace font
> - Close button (X) in top-right
>
> **Timestamp Section**
> - Label: "Timestamp"
> - ISO format: `2025-12-17T14:23:45.123Z`
> - Monospace font
>
> **Topic Section**
> - Label: "Topic"
> - Full topic path without truncation
> - Example: `homeassistant/light/zigbee_Living_Room_Light/config`
> - Monospace font
>
> **Properties Section**
> - QoS badge (colored by level)
> - Retained/Not Retained badge
> - Size badge (e.g., "342 bytes")
> - Badges displayed inline
>
> **Payload Section**
> - Label: "Payload"
> - Pretty-printed JSON in code block
> - Syntax highlighting
> - Scrollable if long (max 400px height)
> - Example:
> ```json
> {
>   "name": "Living Room Light",
>   "state_topic": "zigbee/Living_Room_Light/state",
>   "command_topic": "zigbee/Living_Room_Light/set",
>   "brightness": true,
>   "schema": "json"
> }
> ```
>
> **Footer**
> - "Close" button (gray)

### 6. Publish Form

Located in the right column for sending test messages:

![Screenshot: Publish Form](screenshots/mqtt-publish-form.png)

> **Screenshot Description:** A card with header "📤 Publish Message" containing:
>
> **Topic Input**
> - Label: "Topic"
> - Placeholder: `e.g., zigbee/test`
> - Monospace font
> - Example: `zigbee/Living_Room_Light/set`
>
> **Payload Textarea**
> - Label: "Payload"
> - 4 rows tall, resizable
> - Monospace font
> - Placeholder: `{"test": "message"}`
> - JSON input with syntax awareness
>
> **QoS Dropdown** (left, 50% width)
> - Label: "QoS"
> - Options:
    >   - `0 - At most once` (default)
>   - `1 - At least once`
>   - `2 - Exactly once`
>
> **Retain Checkbox** (right, 50% width)
> - Label: "Retain"
> - Unchecked by default
> - Aligned with QoS dropdown
>
> **Publish Button**
> - Full width
> - Blue background
> - Text: "📤 Publish"
> - Disabled if topic empty

#### Publishing Workflow

![Screenshot: Publish Workflow](screenshots/mqtt-publish-workflow.png)

> **Screenshot Description:** Three states shown:
>
> **State 1: Empty Form**
> - All fields empty
> - Publish button enabled but shows validation
>
> **State 2: Filled Form**
> - Topic: `zigbee/test`
> - Payload: `{"state": "ON"}`
> - QoS: 1
> - Retain: Checked
> - Publish button highlighted
>
> **State 3: Success Toast**
> - Green toast notification: "✓ Message published"
> - Appears top-right corner
> - Auto-dismisses after 3 seconds

### 7. Help Card

Contextual help in the right column:

![Screenshot: Help Card](screenshots/mqtt-help-card.png)

> **Screenshot Description:** A card with header "ℹ️ Help" containing:
>
> **Topic Wildcards Section**
> - Bold header: "Topic Wildcards"
> - Bulleted list:
    >   - `+` - Single level wildcard
          >     - Gray text: "e.g., `zigbee/+/state`"
>   - `#` - Multi-level wildcard
      >     - Gray text: "e.g., `zigbee/#`"
>
> **Features Section**
> - Bold header: "Features"
> - Bulleted list:
    >   - Real-time message monitoring
>   - Topic filtering with wildcards
>   - Payload search
>   - Message inspection
>   - Test message publishing

---

## Usage Examples

### Example 1: Monitoring All Device States

**Goal:** Watch all device state updates in real-time

**Steps:**

1. Click **Start** button to begin monitoring
2. In the **Topic Filter**, enter: `zigbee/+/state`
3. Watch as device state messages appear

![Screenshot: Monitoring Device States](screenshots/example-device-states.png)

> **Screenshot Description:** Message table showing:
> ```
> Time          Topic                              Payload
> 14:23:45.123  zigbee/Living_Room_Light/state    {"state":"ON","brightness":254}
> 14:23:46.234  zigbee/Bedroom_Sensor/state       {"occupancy":true,"temperature":22.5}
> 14:23:47.345  zigbee/Kitchen_Switch/state       {"state":"OFF"}
> ```
>
> - All messages match the `zigbee/+/state` pattern
> - Different devices shown
> - Real-time timestamps
> - JSON payloads visible

**What You'll See:**
- Device state changes as they happen
- Temperature sensor updates
- Motion sensor triggers
- Light on/off commands
- Brightness adjustments

### Example 2: Debugging Home Assistant Discovery

**Goal:** Verify MQTT discovery messages are being sent correctly

**Steps:**

1. Start monitoring
2. Topic Filter: `homeassistant/#`
3. Remove and re-add a device to trigger discovery
4. Watch for discovery messages

![Screenshot: HA Discovery Messages](screenshots/example-ha-discovery.png)

> **Screenshot Description:** Message table showing:
> ```
> Time          Topic                                           Payload
> 14:25:01.123  homeassistant/light/.../config                 {"name":"Living Room"...}
> 14:25:01.234  homeassistant/sensor/.../temperature/config    {"device_class":"temperature"...}
> 14:25:01.345  homeassistant/binary_sensor/.../motion/config  {"device_class":"motion"...}
> ```
>
> - Multiple discovery topics
> - All under `homeassistant/` prefix
> - Large JSON payloads (truncated)
> - Sequential timestamps showing batch publish

**What to Check:**
- ✅ Discovery topics follow HA convention
- ✅ Payloads contain correct device info
- ✅ `unique_id` is properly formatted
- ✅ `device` block has correct identifiers
- ✅ State and command topics are correct

### Example 3: Testing Device Commands

**Goal:** Send a test command to a light and verify it's received

**Steps:**

1. Start monitoring
2. Topic Filter: `zigbee/Living_Room_Light/#`
3. In Publish Form:
    - Topic: `zigbee/Living_Room_Light/set`
    - Payload: `{"state": "ON", "brightness": 128}`
    - QoS: 1
4. Click **Publish**
5. Watch for:
    - Your command message
    - Device response on state topic

![Screenshot: Command Testing](screenshots/example-command-test.png)

> **Screenshot Description:** Split view showing:
>
> **Top: Message Table**
> ```
> 14:30:01.123  zigbee/Living_Room_Light/set    {"state":"ON","brightness":128}  QoS 1
> 14:30:01.234  zigbee/Living_Room_Light/state  {"state":"ON","brightness":128}  QoS 0
> ```
>
> **Bottom: Publish Form**
> - Topic field shows: `zigbee/Living_Room_Light/set`
> - Payload shows: `{"state": "ON", "brightness": 128}`
> - QoS: 1 selected
> - Success toast: "✓ Message published"

**Expected Results:**
1. First message: Your command on `/set` topic (QoS 1)
2. Second message: Device state update on `/state` topic (QoS 0)
3. ~100-200ms delay between messages

### Example 4: Searching for Specific Events

**Goal:** Find all motion detection events

**Steps:**

1. Start monitoring
2. Leave Topic Filter empty (capture all)
3. In Search Filter, type: `motion`
4. Or type: `occupancy`
5. Review filtered results

![Screenshot: Motion Event Search](screenshots/example-motion-search.png)

> **Screenshot Description:** Message table with search active:
> ```
> Search: "motion" [x]
> 
> Time          Topic                           Payload
> 14:35:01.123  zigbee/Hallway_Sensor/state    {"motion":true,"occupancy":true}
> 14:35:15.234  zigbee/Hallway_Sensor/state    {"motion":false,"occupancy":false}
> 14:35:42.345  zigbee/Bedroom_Sensor/state    {"motion":true,"occupancy":true}
> ```
>
> - Search term "motion" highlighted in UI
> - Only messages containing "motion" shown
> - Both topic and payload searched
> - Results from different sensors

**Common Searches:**
- `"state"` - All state updates
- `"temperature"` - Temperature readings
- `"battery"` - Battery status
- `"online"` - Availability changes
- `"command"` - Command messages
- `Error` - Error messages

### Example 5: Monitoring Home Assistant Birth Messages

**Goal:** Detect when Home Assistant restarts

**Steps:**

1. Start monitoring
2. Topic Filter: `homeassistant/status`
3. Restart Home Assistant
4. Watch for `online` message

![Screenshot: HA Birth Message](screenshots/example-ha-birth.png)

> **Screenshot Description:** Single message highlighted:
> ```
> Time          Topic                    Payload    QoS/Retain
> 14:40:05.123  homeassistant/status    online     QoS 1  R
> ```
>
> - Retained flag visible (R badge)
> - Simple "online" payload
> - QoS 1 for reliability
> - This triggers device republishing in your gateway

**Why This Matters:**
When HA restarts, it publishes this birth message. Your gateway listens for it and automatically republishes all device discovery messages. Use the MQTT Explorer to:
- Verify the birth message is published
- Confirm your gateway receives it
- Watch discovery messages being republished

---

## Advanced Features

### 1. Topic Pattern Matching

MQTT Explorer supports standard MQTT wildcards:

#### Single-Level Wildcard (+)

Matches exactly one topic level:

**Pattern:** `zigbee/+/state`

**Matches:**
- ✅ `zigbee/Living_Room_Light/state`
- ✅ `zigbee/Bedroom_Sensor/state`
- ❌ `zigbee/state` (missing level)
- ❌ `zigbee/Living_Room_Light/motion/state` (too many levels)

**Use Cases:**
- All device states: `zigbee/+/state`
- All device commands: `zigbee/+/set`
- Specific cluster: `homeassistant/light/+/config`

#### Multi-Level Wildcard (#)

Matches zero or more topic levels:

**Pattern:** `zigbee/#`

**Matches:**
- ✅ `zigbee/state`
- ✅ `zigbee/Living_Room_Light/state`
- ✅ `zigbee/Living_Room_Light/motion/state`
- ✅ `zigbee/group/Living_Room/set`
- ❌ `homeassistant/sensor/zigbee` (different root)

**Use Cases:**
- All Zigbee topics: `zigbee/#`
- All HA discovery: `homeassistant/#`
- All device subtopics: `zigbee/Living_Room_Light/#`

#### Combining Wildcards

**Pattern:** `homeassistant/+/zigbee_+/config`

**Matches:**
- ✅ `homeassistant/light/zigbee_abc123/config`
- ✅ `homeassistant/sensor/zigbee_def456/config`
- ❌ `homeassistant/light/hue_abc123/config` (not zigbee_)

![Screenshot: Wildcard Examples](screenshots/advanced-wildcards.png)

> **Screenshot Description:** Help panel showing:
>
> **Wildcard Examples**
> ```
> Pattern                      Description
> ─────────────────────────────────────────────
> zigbee/+/state               All device states
> zigbee/Living_Room_+/#       All Living Room devices
> homeassistant/#              All HA topics
> +/+/+/set                    All commands (any depth)
> zigbee/+/availability        Device online/offline
> ```

### 2. Message Export (Planned)

Future feature to export captured messages:

**Export Formats:**
- JSON - Full message details
- CSV - Table-ready format
- Text - Simple log format

**Export Options:**
- Current filtered view
- All messages in buffer
- Time range selection

### 3. Statistics Analysis

The statistics dashboard provides insights:

![Screenshot: Statistics Analysis](screenshots/advanced-statistics.png)

> **Screenshot Description:** Expanded statistics view showing:
>
> **Message Rate Graph** (if implemented)
> - Line chart showing msgs/sec over time
> - 60-second window
> - Spikes indicate burst activity
>
> **Top Topics** (if implemented)
> - Bar chart of most active topics
> - Example:
    >   ```
>   zigbee/Bedroom_Sensor/state     142 msgs
>   zigbee/Living_Room_Light/state   89 msgs
>   homeassistant/status             12 msgs
>   ```
>
> **Traffic Patterns**
> - Peak time identification
> - Average message size
> - Most common QoS levels

**Interpreting Statistics:**

**Total Messages**
- Indicates system activity level
- Resets when you click Clear
- Accumulates from monitoring start

**Messages/Second**
- Normal: 0-10 msgs/sec (periodic sensors)
- Busy: 10-50 msgs/sec (active usage)
- Very Busy: 50+ msgs/sec (multiple motion sensors)

**Unique Topics**
- Should match: # of devices × ~3-5 topics per device
- HA discovery adds ~2-3 topics per entity
- Example: 20 devices = ~100-150 topics

**Buffer Usage**
- Warning at 800/1000 (80%)
- Messages roll off oldest-first
- Clear buffer to reset

### 4. Real-Time Performance

**Latency Characteristics:**

| Event Type | Expected Latency | Notes |
|------------|-----------------|--------|
| Message capture | <5ms | MQTT client to buffer |
| WebSocket broadcast | <10ms | Buffer to frontend |
| Total end-to-end | <20ms | Broker to display |
| Message processing | <1ms | Filtering and search |

**Performance Tips:**
- ✅ Use specific topic filters (reduces processing)
- ✅ Limit search to needed timeframe
- ✅ Stop monitoring when not debugging
- ✅ Clear buffer periodically
- ❌ Avoid wildcards when debugging one device

### 5. Integration with Other Debug Tools

Combine MQTT Explorer with other gateway features:

![Screenshot: Multi-Tool Debugging](screenshots/advanced-multi-tool.png)

> **Screenshot Description:** Browser window with three tabs open:
>
> **Tab 1: MQTT Explorer**
> - Shows MQTT message for motion sensor
> - Topic: `zigbee/Hallway_Sensor/state`
> - Payload: `{"occupancy": true}`
>
> **Tab 2: Debug Log**
> - Shows handler processing log
> - `[Hallway_Sensor] Motion detected via occupancy cluster`
>
> **Tab 3: Debug Packets**
> - Shows raw Zigbee packet
> - Cluster: 0x0406 (Occupancy Sensing)
> - Command: Report Attributes

**Debugging Workflow:**

1. **Packet Capture** - See Zigbee radio message
    - Raw ZCL frame
    - Cluster and command IDs
    - Attribute values

2. **Debug Log** - See handler processing
    - Which handler triggered
    - State updates applied
    - Any errors encountered

3. **MQTT Explorer** - See published result
    - Final MQTT message
    - Correct topic used
    - Proper JSON format

This **three-level view** helps identify where issues occur:
- Radio level (Packet Capture)
- Application level (Debug Log)
- Integration level (MQTT Explorer)

---

## Troubleshooting

### Issue: MQTT Explorer Won't Start

**Symptoms:**
- Click Start button
- Receive error message: "Failed to start monitoring"
- Status badge remains "Stopped"

**Possible Causes & Solutions:**

#### 1. MQTT Not Connected

**Check:**
```bash
sudo systemctl status zigbee-manager
```

Look for: `✓ Connected to MQTT Broker`

**Solution:**
```bash
# Check MQTT broker is running
sudo systemctl status mosquitto

# Check config.yaml has correct broker settings
sudo nano /opt/zigbee-manager/config.yaml

# Verify mqtt section:
mqtt:
  broker_host: localhost  # or IP address
  broker_port: 1883
  username: your_username
  password: your_password
```

#### 2. Permission Issues

**Check logs:**
```bash
sudo journalctl -u zigbee-manager -f
```

Look for: `Permission denied` or `Connection refused`

**Solution:**
```bash
# Check mosquitto ACL if using authentication
sudo nano /etc/mosquitto/conf.d/auth.conf

# Ensure your user can subscribe to #
```

#### 3. Port Already in Use

**Symptoms:** Multiple instances running

**Check:**
```bash
ps aux | grep zigbee
```

**Solution:**
```bash
# Kill any duplicate processes
sudo systemctl restart zigbee-manager
```

### Issue: No Messages Appearing

**Symptoms:**
- Monitoring status shows "Monitoring" (green)
- Message table is empty or not updating
- Statistics show 0 messages

**Debugging Steps:**

#### 1. Verify MQTT Traffic Exists

Use command-line tools to verify:

```bash
# Subscribe to all topics
mosquitto_sub -h localhost -t '#' -v

# Should see messages flowing
# If not, issue is with broker/devices, not Explorer
```

#### 2. Check Topic Filter

**Problem:** Filter too restrictive

**Example:**
- Filter: `zigbee/Living_Room_Light/state`
- No messages appear
- Device name is actually: `Living_Room_Light_1`

**Solution:**
- Use wildcard: `zigbee/Living_Room_+/state`
- Or clear filter to see all topics
- Click topic in message to copy exact name

#### 3. Check Browser Console

**Open Developer Tools:**
- Press F12
- Go to Console tab
- Look for JavaScript errors

**Common errors:**
```javascript
// WebSocket not connected
WebSocket connection failed

// Solution: Refresh page
```

#### 4. Refresh Messages Manually

**Steps:**
1. Click **Refresh** button
2. Wait 2-3 seconds
3. If messages appear, WebSocket may be disconnecting
4. Check network connection

### Issue: High Memory Usage

**Symptoms:**
- Browser becomes slow
- Message table lags when scrolling
- System memory increases

**Causes & Solutions:**

#### 1. Buffer Too Large

**Default:** 1000 messages × ~1KB each = ~1MB

**Solution - Reduce buffer size:**

Edit `main.py`:
```python
# Change from:
mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)

# To:
mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=500)
```

Restart service:
```bash
sudo systemctl restart zigbee-manager
```

#### 2. Too Many Messages

**High-traffic environment:** 100+ msgs/sec

**Solutions:**
- Use specific topic filters
- Stop monitoring when not actively debugging
- Clear buffer more frequently
- Reduce monitoring duration

#### 3. Memory Leak (Browser)

**Long monitoring sessions** (hours)

**Solution:**
- Refresh browser page periodically
- Use Firefox/Chrome's task manager to check
- Stop and restart monitoring

### Issue: Messages Arriving Out of Order

**Symptoms:**
- Timestamps not sequential
- State update before command
- Duplicate messages

**Explanation:**

This is **normal MQTT behavior** with QoS 0:

```
Expected:   Actual (possible):
1. Command  1. Command
2. State    2. State
            3. Command (retry)
            4. State (delayed)
```

**Not a Bug When:**
- Using QoS 0 (at most once)
- Network latency varies
- Multiple devices sending simultaneously

**Solutions:**
- Sort by timestamp (if needed)
- Focus on most recent message per topic
- Use QoS 1 for commands requiring ordering

### Issue: Publish Button Doesn't Work

**Symptoms:**
- Click Publish
- No error, no success message
- Message doesn't appear in table

**Debugging Steps:**

#### 1. Check Topic

**Invalid topics:**
- Empty topic
- Topic with spaces
- Special characters: `$`, `+`, `#` (reserved)

**Valid topics:**
```
✅ zigbee/test
✅ zigbee/Living_Room_Light/set
✅ custom/topic/123
❌ zigbee test (space)
❌ $SYS/broker (reserved)
❌ (empty)
```

#### 2. Check Payload

**JSON validation:**

```json
// Valid
{"state": "ON"}

// Invalid - missing quotes
{state: ON}

// Invalid - trailing comma
{"state": "ON",}
```

**Solution:** Use online JSON validator

#### 3. Check QoS & Broker Support

**QoS 2** may not be supported by all brokers

**Solution:** Try QoS 1 instead

---

## Best Practices

### 1. Monitoring Strategy

**Don't Leave Running 24/7**

The MQTT Explorer is a **debugging tool**, not a monitoring solution.

**Good:**
- ✅ Start when debugging issue
- ✅ Use for 5-30 minutes
- ✅ Stop when done
- ✅ Clear buffer between sessions

**Bad:**
- ❌ Leave running continuously
- ❌ Monitor for hours without clearing
- ❌ Keep multiple browser tabs open
- ❌ Use as primary monitoring tool

**Why:**
- Accumulates memory over time
- WebSocket connections multiply
- Fills buffer with old data
- Better tools exist for long-term monitoring (HA, Grafana)

### 2. Effective Filtering

**Start Broad, Then Narrow**

**Debugging Unknown Issue:**

```
Step 1: Start with no filter (#)
        → Observe traffic patterns
        → Identify relevant topics

Step 2: Add topic filter (e.g., zigbee/+/state)
        → Focus on device states
        → Reduce noise

Step 3: Add search filter (e.g., "error")
        → Find specific problems
        → Review matches
```

**Common Filter Patterns:**

| Goal | Topic Filter | Search Filter |
|------|-------------|---------------|
| Debug specific device | `zigbee/Device_Name/#` | (empty) |
| Find errors | `#` | `error` or `failed` |
| Monitor HA integration | `homeassistant/#` | (empty) |
| Track state changes | `zigbee/+/state` | specific IEEE |
| Check availability | `zigbee/+/availability` | (empty) |

### 3. Message Inspection Tips

**Click Messages Strategically**

**Don't click every message** - use table view to scan

**Click when:**
- ✅ Payload truncated (shows "...")
- ✅ Need to copy exact JSON
- ✅ Verifying structure of discovery message
- ✅ Checking retained flag
- ✅ Examining large payloads

**Use table view when:**
- ✅ Scanning for patterns
- ✅ Comparing multiple messages
- ✅ Monitoring message frequency
- ✅ Watching real-time updates

### 4. Publishing Test Messages

**Incremental Testing**

**Bad approach:**
```json
// Sending complex message immediately
{"state": "ON", "brightness": 200, "color_temp": 400, "transition": 5}
```

**Good approach:**
```json
// Test 1: Basic command
{"state": "ON"}

// Test 2: Add brightness (if Test 1 works)
{"state": "ON", "brightness": 200}

// Test 3: Add remaining (if Test 2 works)
{"state": "ON", "brightness": 200, "color_temp": 400}
```

**Why:** Easier to identify which parameter causes issues

**Verify Message Format**

**Before publishing:**
1. Check device's expected format
2. Compare with working messages
3. Validate JSON syntax
4. Use correct QoS

**Example - Checking Format:**
```
1. Filter to: zigbee/Working_Light/set
2. Send command via HA
3. See format in MQTT Explorer
4. Copy format for test device
```

### 5. Performance Optimization

**Reduce Load**

**For High-Traffic Systems:**

```python
# Smaller buffer
max_messages=500

# More aggressive filtering
topic_filter="zigbee/problem_device/#"

# Monitor in bursts
# 1. Start monitoring
# 2. Trigger issue
# 3. Stop monitoring immediately
```

**Browser Performance:**

- ✅ Use modern browser (Chrome, Firefox, Edge)
- ✅ Close other tabs
- ✅ Disable browser extensions temporarily
- ✅ Use Incognito/Private mode (clean slate)
- ❌ Keep 10+ tabs open
- ❌ Run on old/slow device

### 6. Integration with Home Assistant

**Debugging HA Integration Issues**

**Common scenarios:**

#### Discovery Not Working

```
1. Filter: homeassistant/#
2. Remove device in HA
3. In gateway: Remove & rejoin device
4. Watch MQTT Explorer for discovery messages
5. Should see: homeassistant/[component]/[node_id]/[entity]/config
6. Click message to verify structure
```

**Expected Discovery:**
- One message per entity
- Retained flag (R) present
- Valid JSON with required fields
- Proper `unique_id` format

#### Commands Not Received

```
1. Filter: zigbee/Device_Name/#
2. Send command from HA
3. Should see TWO messages:
   - Command: zigbee/Device_Name/set (QoS 1)
   - State: zigbee/Device_Name/state (QoS 0)
4. If only state appears, gateway isn't receiving commands
5. Check HA MQTT integration config
```

#### State Updates Not Showing

```
1. Trigger device (motion, button press, etc.)
2. Filter: zigbee/Device_Name/state
3. Should see state update within 1 second
4. If delayed >5 seconds, check:
   - Device battery
   - Zigbee mesh quality
   - Gateway processing logs
```

---

## FAQ

### General Questions

**Q: Does MQTT Explorer affect gateway performance?**

A: Minimal impact. The Explorer uses:
- Separate MQTT client (no contention)
- Async processing (non-blocking)
- ~1MB memory (circular buffer)
- <1% CPU when idle, ~5% when active

**Q: Can multiple users use MQTT Explorer simultaneously?**

A: Yes, each browser connection gets its own WebSocket. However:
- All share the same monitoring service
- Buffer is shared (1000 messages total)
- Only one user needs to start monitoring
- All users see the same messages

**Q: Will messages be lost if I'm not monitoring?**

A: **Monitoring only captures what flows while it's running.** Messages sent before starting are not captured. This is intentional - it's a real-time debugging tool, not a logger.

For persistent logging, use:
- MQTT broker logging
- Home Assistant history
- Dedicated MQTT logger (e.g., mqtt-logger)

**Q: Can I export messages?**

A: Currently messages can be retrieved via API:
```bash
curl 'http://localhost:8000/api/mqtt_explorer/messages?limit=1000' > messages.json
```

A proper export feature is planned.

### Technical Questions

**Q: What's the difference between QoS 0, 1, and 2?**

**QoS 0 - At most once:**
- No acknowledgment
- Fire and forget
- Fastest, unreliable
- Use for: Frequent sensor readings (motion, temperature)

**QoS 1 - At least once:**
- Acknowledged delivery
- May receive duplicates
- Moderate speed, reliable
- Use for: Commands, important state changes

**QoS 2 - Exactly once:**
- Guaranteed single delivery
- Slowest, most reliable
- High overhead
- Use for: Critical commands, billing data

**Q: What does the Retained flag mean?**

**Retained message:**
- Stored by broker
- Sent to new subscribers immediately
- Only one retained message per topic
- Use for: Current state, availability, discovery

**Example:**
```
Topic: zigbee/Living_Room_Light/state
Payload: {"state":"ON"}
Retained: Yes

New subscriber connects → Immediately receives last state
```

**Q: Why do I see duplicate messages?**

**Common causes:**

1. **QoS 1 retries** - Expected behavior
2. **Multiple publishers** - Two devices sending to same topic
3. **Retained + Real-time** - Retained message + new message
4. **Bridge/Repeater** - MQTT bridge forwarding messages

**Identifying duplicates:**
- Check timestamps (same = duplicate)
- Check message IDs (if present)
- Look for "duplicate" in Debug Log

**Q: How do topic wildcards work exactly?**

**Single-level (+):**
```
Pattern: sport/+/score
Matches:
  ✅ sport/tennis/score
  ✅ sport/soccer/score
  ❌ sport/score (nothing between /)
  ❌ sport/soccer/player1/score (too deep)
```

**Multi-level (#):**
```
Pattern: sport/#
Matches:
  ✅ sport
  ✅ sport/tennis
  ✅ sport/tennis/score
  ✅ sport/tennis/player1/score
  ❌ gaming/sport (wrong root)
```

**Combined:**
```
Pattern: zigbee/+/sensor/#
Matches:
  ✅ zigbee/bedroom/sensor/temperature
  ✅ zigbee/livingroom/sensor/motion/state
  ❌ zigbee/sensor (missing level)
```

### Troubleshooting Questions

**Q: Message table is empty but statistics show messages.**

**Solution:**
1. Check active filters - clear both topic and search
2. Click Refresh button
3. Check browser console for errors
4. Try stopping and restarting monitoring

**Q: Some messages show binary data - why?**

**Cause:** Message payload is not UTF-8 text

**Display:** `<binary data: 42 bytes>`

**Examples:**
- Image data
- Encrypted payloads
- Binary protocols
- Compressed data

**Solution:** Not an error, just non-text content

**Q: Timestamps don't match my timezone.**

**Cause:** Timestamps are in local browser time but display may vary

**Check:** Message details modal shows full ISO timestamp

**Solution:** If needed, timestamps can be customized in code (future feature)

**Q: Can I save my filter preferences?**

**Current:** Filters reset on page reload

**Planned:** Browser localStorage to save:
- Last used topic filter
- Last used search term
- Auto-scroll preference
- Buffer size preference

**Workaround:** Bookmark with query params (future feature):
```
http://localhost:8000/?mqtt_filter=zigbee/+/state
```

### Integration Questions

**Q: How does this compare to MQTT Explorer (desktop app)?**

| Feature | Desktop MQTT Explorer | This Tool |
|---------|----------------------|-----------|
| Installation | Separate app | Built into gateway |
| Real-time | Yes | Yes |
| Topic tree | Yes | No (list view) |
| History | Persistent | Buffer only |
| Publishing | Yes | Yes |
| Filtering | Tree-based | Pattern-based |
| Best for | Development, setup | Runtime debugging |

**Use desktop app when:**
- Setting up MQTT broker initially
- Need persistent message history
- Want hierarchical topic view
- Working with multiple brokers

**Use this tool when:**
- Debugging gateway issues
- Verifying device communication
- Testing MQTT integration
- Already in web interface

**Q: Can I integrate this with Grafana/InfluxDB?**

**Not directly**, but you can:

1. **Use API endpoints:**
```bash
# Get messages
curl http://localhost:8000/api/mqtt_explorer/messages

# Pipe to InfluxDB, etc.
```

2. **Use proper MQTT logger:**
    - [mqtt-logger](https://github.com/obergodmar/mqtt-logger)
    - [mqtt2db](https://github.com/dgomes/mqtt2db)
    - Home Assistant history

3. **Future feature:** Export to standard formats

---

## Appendix: Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl + F` | Focus search filter |
| `Ctrl + K` | Clear all filters |
| `Escape` | Close message modal |
| `Space` | Start/Stop monitoring |
| `Ctrl + L` | Clear message buffer |

## Appendix: API Reference

See complete API documentation in [MQTT_EXPLORER_INTEGRATION.md](MQTT_EXPLORER_INTEGRATION.md)

**Quick Reference:**

```bash
# Start monitoring
curl -X POST http://localhost:8000/api/mqtt_explorer/start

# Get messages
curl http://localhost:8000/api/mqtt_explorer/messages?limit=100

# Publish message
curl -X POST http://localhost:8000/api/mqtt_explorer/publish \
  -H "Content-Type: application/json" \
  -d '{"topic":"zigbee/test","payload":"hello"}'

# Get statistics
curl http://localhost:8000/api/mqtt_explorer/stats
```

---

## Additional Resources

- **Integration Guide:** [MQTT_EXPLORER_INTEGRATION.md](MQTT_EXPLORER_INTEGRATION.md)
- **Quick Start:** [QUICKSTART.md](QUICKSTART.md)
- **MQTT Protocol:** https://mqtt.org/
- **Home Assistant MQTT:** https://www.home-assistant.io/integrations/mqtt/
- **Zigbee Manager Docs:** [docs/](../docs/)

---

**Last Updated:** December 2025  
**Version:** 1.0.0  
**Author:** Zigbee Manager Project

---

*Have questions or suggestions for this wiki? Open an issue or submit a pull request!*