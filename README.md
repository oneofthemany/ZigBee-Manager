<p align="center">
  <img src="docs/images/zigbee-manager-logo.png" alt="ZigBee Manager" width="120">
</p>

<h1 align="center">ZigBee & Matter Manager</h1>

<p align="center">
  <strong>A Python-powered ZigBee & Matter gateway with real-time web UI and Home Assistant integration</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-blue?logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/zigpy-bellows-orange" alt="zigpy">
  <img src="https://img.shields.io/badge/Home_Assistant-MQTT-41BDF5?logo=homeassistant&logoColor=white" alt="Home Assistant">
  <img src="https://img.shields.io/badge/Matter-WiFi%20%7C%20Thread-7B61FF" alt="Matter">
  <img src="https://img.shields.io/badge/OTA-Firmware%20Updates-FF6B35" alt="OTA">
  <img src="https://img.shields.io/badge/license-GPL--3.0-green" alt="License">
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> · 
  <a href="#-features">Features</a> · 
  <a href="#-web-interface">Web Interface</a> · 
  <a href="#-automation-engine">Automations</a> · 
  <a href="#-ota-firmware-updates">OTA Updates</a> · 
  <a href="#-device-onboarding">Device Onboarding</a> · 
  <a href="#-configuration">Configuration</a> · 
  <a href="#-documentation">Docs</a>
</p>

---

<!-- SCREENSHOT: Full device table view -->
<p align="center">
  <img src="docs/images/screenshot-devices.png" alt="Device table with LQI, status, and controls" width="90%">
  <br><em>Device management dashboard — real-time status, LQI, OTA badges, protocol indicators, and per-device controls</em>
</p>

## 📖 Overview

**ZigBee Matter Manager** is a self-hosted gateway application that manages Zigbee and Matter mesh networks and bridges devices to **Home Assistant** via MQTT Discovery. It supports **Matter** devices over WiFi (and Thread via OTBR), presenting a unified device list across both protocols. It has a modular Python backend built on [zigpy](https://github.com/zigpy/zigpy)/[bellows](https://github.com/zigpy/bellows) and [python-matter-server](https://github.com/home-assistant-libs/python-matter-server), with a real-time single-page web interface.

The system is designed for production-grade home automation — running 40+ devices on a Rock 5B with 30+ day uptime, featuring automatic NCP failure recovery, exponential backoff retries, OTA firmware management, and a fast-path pipeline for latency-critical sensor events.

---

## ⚡ Quick Start

```bash
# Clone the repository
git clone https://github.com/oneofthemany/ZigBee-Matter-Manager.git
cd ZigBee-Matter-Manager

# Run the automated deployment (sets up venv, systemd service, user)
sudo bash deploy.sh

# Start the service
sudo systemctl start zigbee-matter-manager
```

Open **http://YOUR_IP:8000** in your browser.

On first boot, if `channel`, `pan_id`, `extended_pan_id`, or `network_key` are absent or placeholder values, the system will **auto-generate valid random credentials** and write them to `config.yaml` before starting the radio. No manual YAML editing required for initial setup.

### Prerequisites

- Linux (Ubuntu/Debian recommended)
- Python 3.8+
- An MQTT broker (e.g. Mosquitto)
- A supported Zigbee coordinator (EZSP or ZNP USB stick)
- **Optional for Matter:** `python-matter-server[server]` pip package, IPv6-enabled network

---

## 🚀 Features

### Network Management & Device Control

- **Real-time Web Interface** — Single-page app with WebSocket-driven live updates, Bootstrap 5 UI
- **Device Lifecycle** — Join, rename, remove, re-interview, ban/unban devices
- **Remote Control** — On/Off, brightness, color temp, color XY/HS, cover position, thermostat setpoints
- **Multi-Endpoint Routing** — Proper handling of devices with multiple endpoints (e.g., dual-gang switches)
- **Device Tabs** — Custom tab organization to group devices by room or function (e.g., "Heating", "Lighting")
- **Touchlink** — Scan, identify (blink), and factory reset Philips Hue bulbs directly from the web UI

<!-- SCREENSHOT: Device modal with control tab -->
<p align="center">
  <img src="docs/images/screenshot-device-modal.png" alt="Device modal showing controls, bindings, clusters, config, OTA, and automation tabs" width="70%">
  <br><em>Device modal — control panel, cluster browser, bindings, per-device configuration, OTA firmware, and automation rules</em>
</p>

### Zigbee Groups

- **Native Zigbee Groups** — Create groups at the coordinator level (not just software grouping)
- **Smart Compatibility** — Input/output cluster awareness ensures only actuators are groupable; sensor-only devices are excluded
- **Unified Control** — On/off, brightness, color temp, color, and cover controls for groups
- **Home Assistant Discovery** — Groups appear as native HA entities via MQTT

<!-- SCREENSHOT: Groups tab -->
<p align="center">
  <img src="docs/images/screenshot-groups.png" alt="Groups tab with create form and group control modal" width="70%">
  <br><em>Groups — compatible device detection, group creation, and unified control panel</em>
</p>

### Matter Integration (Optional)

Support for **Matter** devices alongside Zigbee — presented as a unified device list with protocol-aware routing.

- **Embedded Server** — Runs [python-matter-server](https://github.com/home-assistant-libs/python-matter-server) as a managed subprocess, no Docker required
- **WiFi Matter Devices** — Commission and control Matter-over-WiFi devices (Eve, Nanoleaf, etc.)
- **Unified Device List** — Matter and Zigbee devices appear in the same table with protocol badges
- **Cross-Protocol Automation** — Automation engine triggers and actions work across both protocols
- **MQTT Discovery** — Matter devices published to Home Assistant using the same discovery patterns
- **Zero Overhead** — Completely optional; disabled by default with no impact on Zigbee performance

<!-- SCREENSHOT: Matter commissioning -->
<p align="center">
  <img src="docs/images/screenshot-matter.png" alt="Matter device commissioning and unified device list" width="70%">
  <br><em>Matter integration — commission via setup code, unified device list with protocol badges</em>
</p>

For full documentation see [docs/matter.md](docs/matter.md).

### OTA Firmware Updates

Over-the-air firmware management for all Zigbee devices with OTA cluster support (0x0019).

- **Multi-Provider Support** — Automatic image matching via IKEA, LEDVANCE, Sonoff, Inovelli, and other zigpy OTA providers
- **Per-Device Check & Update** — Check availability, trigger updates, and monitor progress from the device modal OTA tab
- **Bulk Scan** — One-click "Check OTA" scans all devices for available firmware updates
- **Live Progress** — Real-time WebSocket-driven progress bar during firmware transfer
- **Local Firmware Upload** — Upload `.ota`, `.zigbee`, `.bin`, `.ota1`, `.sbl-ota` files for devices not covered by online providers
- **Background Checks** — Periodic automatic scans (every 6 hours) with notification when updates are found
- **Image Notify** — Send OTA Image Notify commands to prompt sleepy devices to check for updates
- **OTA Badge** — Devices with OTA cluster show an OTA badge in the device table for quick identification

<!-- SCREENSHOT: OTA firmware tab -->
<p align="center">
  <img src="docs/images/screenshot-ota.png" alt="OTA firmware update tab with check, notify, and progress bar" width="70%">
  <br><em>OTA firmware tab — check for updates, trigger install, and live progress tracking</em>
</p>

### Automation Engine

A full state-machine automation system that executes directly at the Zigbee gateway level with **zero MQTT round-trip delay**.

- **State Machine Triggers** — Fire only on transitions (matched → unmatched), not on every matching update
- **Multi-Condition Rules** — Up to 5 AND conditions with sustain timers per source device
- **Prerequisites** — Check other device states before firing, with NOT negation and OR logic across multiple time windows
- **Recursive Action Sequences** — Command, Delay, Wait For, Gate, If/Then/Else branching, Parallel execution
- **Group Targets** — Command steps can target Zigbee groups as well as individual devices
- **Day-of-Week Filtering** — Restrict rules to specific days
- **Event-Type Auto-Reset** — Attributes like `action` are automatically reset to allow re-triggering
- **Time Boundary Scheduler** — 30-second polling loop fires rules at exact time boundaries, not just on device state changes
- **Global Automations Tab** — Dedicated top-level nav tab showing all rules across all devices with inline filtering, editing, and trace log
- **Trace Log** — Real-time colour-coded evaluation history for debugging automation behaviour
- **JSON Export** — Download/import rules for backup or sharing

<!-- SCREENSHOT: Global automations tab -->
<p align="center">
  <img src="docs/images/screenshot-automations-global.png" alt="Global automations tab with all rules across devices" width="90%">
  <br><em>Global automations tab — all rules across all devices, filterable by device and state</em>
</p>

<!-- SCREENSHOT: Automation rule builder -->
<p align="center">
  <img src="docs/images/screenshot-automation.png" alt="Automation rule builder with conditions, prerequisites, and THEN/ELSE sequences" width="70%">
  <br><em>Automation rule builder — IF/AND conditions, CHECK prerequisites, THEN/ELSE action sequences</em>
</p>

For full documentation see [docs/automations.md](docs/automations.md).

### Device Onboarding (Unsupported Devices)

A three-layer system for onboarding devices that don't have dedicated cluster handlers — no code changes or restarts required.

- **GenericClusterHandler** — Automatically attached to any cluster without a dedicated handler; captures all attribute reports and commands as raw keys
- **Device Override Manager** — JSON-driven definitions (`data/device_overrides.json`) that map raw keys to friendly names with scaling, units, and Home Assistant device classes
- **Visual Mappings UI** — A "Mappings" tab in the device modal for mapping attributes without touching code
- **Model-Level Promotion** — Map once per device, then promote to a model definition so all devices of the same type inherit the mappings automatically
- **Manufacturer-Aware Profiles** — Known manufacturer clusters (Aqara 0xFCC0, Tuya 0xEF00, Philips 0xFC00, IKEA 0xFC7C, etc.) get intelligent prefixing and manufacturer code handling
- **REST API** — Full CRUD for overrides via `/api/device_overrides` endpoints for scripting or bulk operations

<!-- SCREENSHOT: Mappings tab -->
<p align="center">
  <img src="docs/images/screenshot-mapping.png" alt="Device mappings tab showing unmapped attributes and active mappings" width="70%">
  <br><em>Mappings tab — visual attribute mapping for unsupported devices, with model-level promotion</em>
</p>

For full documentation see [docs/onboarding_unsupported_devices.md](docs/onboarding_unsupported_devices.md).

### MQTT Explorer

An integrated MQTT debugging tool — monitor all broker traffic in real-time without leaving the gateway.

- **Live Traffic Monitor** — Subscribe to `#` wildcard with topic and payload filtering
- **Publish Tool** — Send test messages to any topic with configurable QoS
- **Wildcard Support** — `+` single-level and `#` multi-level pattern matching
- **Three-Level Debugging** — Correlate packet capture → debug log → MQTT output

<!-- SCREENSHOT: MQTT Explorer -->
<p align="center">
  <img src="docs/images/screenshot-mqtt-explorer.png" alt="MQTT Explorer showing live message stream with filters" width="70%">
  <br><em>MQTT Explorer — real-time message stream, topic filtering, and publish tool</em>
</p>

For full documentation see [docs/mqtt-explorer.md](docs/mqtt-explorer.md).

### Zone-Based Presence Detection

An experimental presence detection system using RSSI signal fluctuations from existing Zigbee devices — no dedicated presence sensors required.

- **RSSI Baseline Calibration** — Learns normal signal patterns per device link
- **Fluctuation Detection** — Triggers occupancy when multiple links show deviation
- **MQTT Publishing** — Zones appear as binary sensors in Home Assistant
- **Configurable Thresholds** — Deviation sensitivity, minimum triggered links, clear delay

<!-- SCREENSHOT: Zones tab -->
<p align="center">
  <img src="docs/images/screenshot-zones.png" alt="Zones tab with zone creation and status cards" width="70%">
  <br><em>Zone presence detection — RSSI-based occupancy without dedicated sensors</em>
</p>

### Stability & Resilience

- **NCP Failure Recovery** — Automatic watchdog with recovery logic for critical coordinator failures
- **EZSP Dynamic Tuning** — Coordinator stack settings auto-tuned based on network size (packet buffers, APS counts, source route tables)
- **Fast Path Processing** — Non-blocking pipeline for motion/presence sensors to minimise MQTT publication latency
- **MQTT Queue** — Background publish queue prevents event loop stalls during bursts
- **Exponential Backoff** — Automatic retry with configurable backoff for transient command failures
- **Multi-Radio Support** — Auto-detection of EZSP and ZNP coordinators
- **Orphaned Device Cleanup** — Detect and remove stale database entries for devices no longer on the network

### Diagnostics & Debugging

- **Live Debug Log** — Real-time filtered log streaming to the browser
- **Packet Capture** — Raw ZCL frame capture with human-readable decoding
- **Deep Packet Analysis** — IAS Zone (0x0500), Occupancy (0x0406), and Tuya (0xEF00) protocol decoders with Tuya DP decoder
- **Mesh Topology** — Interactive D3.js force-directed graph with LQI link quality overlay and online/offline device zones

<!-- SCREENSHOT: Mesh topology -->
<p align="center">
  <img src="docs/images/screenshot-mesh.png" alt="D3.js mesh topology with LQI-coloured links" width="70%">
  <br><em>Mesh topology — force-directed graph showing device relationships and link quality</em>
</p>

### Home Assistant Integration

- **MQTT Discovery** — All devices and groups auto-discovered with proper schemas (JSON, not legacy template)
- **Full Component Support** — light, switch, cover, climate, sensor, binary_sensor, number
- **Birth Message Handling** — Automatic republish on HA restart
- **Device Metadata** — Manufacturer, model, SW version passed through to HA device registry
- **Delta-Only Publishing** — Only changed attributes are published to avoid false HA automation triggers

### Supported Devices & Quirk Handling

Tested with 40+ devices across multiple manufacturers:

| Manufacturer       | Devices                            | Notes                                              |
|:-------------------|:-----------------------------------|:---------------------------------------------------|
| **IKEA**           | Tradfri bulbs (E14, E27, GU10)     | Brightness, color temp, OTA updates                |
| **Philips**        | Hue lights, motion sensors         | `on_with_timed_off` motion detection on EP1        |
| **Aqara / Xiaomi** | TRVs, sensors, switches            | Packed binary struct parsing (0xFF01, 0x00DF, 0x00F7), Aqara Opple cluster (0xFCC0) |
| **Hive**           | SLT6 thermostat, SLR1c receiver    | Proprietary EP9→EP5 handshake during pairing       |
| **Aurora**         | DoubleSocket50AU                   | Multi-endpoint socket with per-endpoint state      |
| **Tuya**           | Radar sensors, blinds, switches    | Cluster 0xEF00 DP parsing with device-type filtering |
| **Generic**        | Contact sensors, smart sockets     | GenericClusterHandler with override mapping         |

New devices can be onboarded via the [Device Onboarding](#device-onboarding-unsupported-devices) system without code changes, or by adding dedicated handlers in `handlers/`.

---

## 🌐 Web Interface

Access at **http://YOUR_IP:8000**. All tabs update in real-time via WebSocket.

| Tab               | Description                                                                                                                                                  |
|:------------------|:-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Devices**       | Main device table — LQI, status, last seen, OTA badges, protocol badges. Click any device for a full modal                                                  |
| **Topology**      | Interactive force-directed mesh graph with LQI link quality and online/offline zones                                                                         |
| **Groups**        | Create and control native Zigbee groups                                                                                                                      |
| **Automations**   | Global automation rules across all devices with inline filtering, editing, and trace log                                                                     |
| **Zones**         | RSSI-based presence detection zones                                                                                                                          |
| **MQTT Explorer** | Real-time MQTT traffic monitor and publish tool                                                                                                              |
| **Settings**      | Rich settings panel — see below                                                                                                                              |
| **Debug Log**     | Live filtered log stream and raw packet analyser with Tuya DP decoder                                                                                       |

### Settings Panel

The settings tab is a four-sub-tab panel:

| Sub-tab               | Description                                                                                                                                                                                      |
|:----------------------|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Configuration**     | Form-based editor for Zigbee radio, MQTT broker, web interface, and logging settings. Writes to `config.yaml` with no manual YAML editing required. Includes HTTPS/SSL toggle.                   |
| **Security**          | Manage PAN ID, Extended PAN ID, and Network Key with per-field regenerate buttons. Network key is hidden by default.                                                                             |
| **Spectrum Analysis** | Live 2.4 GHz energy scan across all ZigBee channels (11–26) with colour-coded interference chart. Auto channel select writes the best channel to config. Manual channel override also available. |
| **Advanced (YAML)**   | Raw `config.yaml` editor — preserved for advanced users who need direct access.                                                                                                                  |

<!-- SCREENSHOT: Settings panel -->
<p align="center">
  <img src="docs/images/screenshot-settings.png" alt="Settings panel with Configuration, Security, Spectrum Analysis, and Advanced tabs" width="70%">
  <br><em>Settings panel — structured form UI, security credential management, and spectrum analysis</em>
</p>

<!-- SCREENSHOT: Spectrum Analysis -->
<p align="center">
  <img src="docs/images/screenshot-spectrum.png" alt="Spectrum Analysis tabs" width="70%">
  <br><em>Settings panel — spectrum analysis</em>
</p>

### Device Modal

Each device opens a tabbed modal with:

| Tab            | What it does                                                                                                                            |
|:---------------|:----------------------------------------------------------------------------------------------------------------------------------------|
| **Overview**   | Identity, maintenance actions (re-interview, poll, remove, ban), sensor readings, device-specific configuration (Aqara modes, Tuya DPs) |
| **Control**    | Send commands — on/off, brightness sliders, color picker, thermostat setpoints, cover position                                          |
| **Bindings**   | View and manage ZCL bindings between devices                                                                                            |
| **Clusters**   | Raw cluster browser — read attributes, explore endpoints                                                                                |
| **OTA**        | Firmware update management — check for updates, trigger install, live progress bar, image notify                                        |
| **Mappings**   | Visual attribute mapping for devices using GenericClusterHandler (appears when unmapped attributes exist)                                |
| **Automation** | Per-device rule builder for the automation engine                                                                                       |

---

## 🏗️ Architecture

| Component               | Technology                                  | Role                                                               |
|:------------------------|:--------------------------------------------|:-------------------------------------------------------------------|
| **Core**                | Python (FastAPI, zigpy/bellows)              | Zigbee radio, device lifecycle, resilience, state management       |
| **Matter Server**       | python-matter-server (managed subprocess)    | CHIP SDK controller for Matter devices (optional)                  |
| **Matter Bridge**       | aiohttp WebSocket client                     | Translates Matter nodes into unified device format                 |
| **MQTT Service**        | aiomqtt                                      | Broker connection, reconnection, HA MQTT Discovery                 |
| **Cluster Handlers**    | handlers/ package                            | ZCL message decoding, normalised state, device-specific logic      |
| **Generic Handler**     | handlers/generic.py                          | Fallback for unsupported clusters with override manager integration |
| **Automation Engine**   | modules/automation.py                        | State-machine rules, recursive sequences, direct zigpy execution   |
| **OTA Manager**         | modules/ota.py                               | Firmware update orchestration, provider config, progress tracking   |
| **Group Manager**       | modules/groups.py                            | Native Zigbee groups with input/output cluster awareness           |
| **Device Overrides**    | modules/device_overrides.py                  | JSON-driven attribute mappings for unsupported devices              |
| **Network Init**        | modules/network_init.py                      | Auto-generation of credentials and channel selection on first boot |
| **Frontend**            | HTML, Bootstrap 5, D3.js                     | SPA connected via WebSocket for real-time updates                  |

For the full file structure see [docs/structure.md](docs/structure.md).

---

## ⚙️ Configuration

Configuration is managed through the **Settings tab** in the web UI, which provides a structured form interface backed by `config.yaml`. Direct file editing is also available via the Advanced sub-tab or on disk.

### Key Configuration Sections

```yaml
zigbee:
  port: /dev/ttyACM0          # Serial port for coordinator
  baudrate: 115200
  channel: 15                  # Auto-selected via spectrum analysis or manual
  pan_id: "0x1A2B"            # Auto-generated on first boot
  extended_pan_id: "..."       # Auto-generated on first boot
  network_key: [...]           # Auto-generated on first boot (16 bytes)

mqtt:
  host: 192.168.1.x
  port: 1883
  username: mqtt_user
  password: mqtt_pass
  base_topic: zigbee2mqtt      # Base topic for HA discovery
  discovery_prefix: homeassistant

matter:
  enabled: false               # Set true to enable Matter support
  port: 5580                   # python-matter-server WebSocket port

ota:
  enabled: true                # Enable OTA firmware update support
  providers:                   # List of OTA image providers
    - ikea
    - ledvance
    - sonoff
    - inovelli

web:
  host: 0.0.0.0
  port: 8000
  ssl: false
```

---

## 🔧 Troubleshooting

### Debugging Workflow

1. **Live Logs** — Real-time WebSocket log stream with category filtering
2. **Debug Packets Modal** — Raw ZCL frame capture with decoded summaries
3. **MQTT Explorer** — Monitor all MQTT traffic, publish test messages
4. **Trace Log** — Automation evaluation history with colour-coded results
5. **Mesh Topology** — Visual network graph with LQI overlay
6. **Spectrum Analysis** — Identify channel interference causing network instability

### Log Files

| File                    | Content                                             |
|:------------------------|:----------------------------------------------------|
| `logs/zigbee.log`       | Main application log                                |
| `logs/zigbee_debug.log` | Detailed packet/handler events (when debug enabled) |

### Service Commands

```bash
sudo systemctl status zigbee-matter-manager             # Check service status
sudo systemctl kill -s SIGKILL zigbee-matter-manager    # Kill the service
sudo systemctl start zigbee-matter-manager              # Start the service
sudo journalctl -u zigbee-matter-manager -f             # Follow system logs
sudo tail -f /opt/zigbee_matter_manager/logs/zigbee.log # Follow app logs
```

---

## 📚 Documentation

| Document                                                                    | Description                                                        |
|:----------------------------------------------------------------------------|:-------------------------------------------------------------------|
| [docs/matter.md](docs/matter.md)                                           | Matter integration — setup, supported features, architecture       |
| [docs/automations.md](docs/automations.md)                                 | Automation engine — rule syntax, conditions, sequences, examples   |
| [docs/mqtt-explorer.md](docs/mqtt-explorer.md)                             | MQTT Explorer — usage, filtering, architecture                     |
| [docs/onboarding.md](docs/onboarding.md)                                   | Developer guide — handler architecture, adding new device support  |
| [docs/onboarding_unsupported_devices.md](docs/onboarding_unsupported_devices.md) | User guide — visual attribute mapping for unsupported devices |
| [docs/aqara_cluster_guide.md](docs/aqara_cluster_guide.md)                 | Aqara 0xFCC0 cluster implementation reference                     |
| [docs/debugging.md](docs/debugging.md)                                     | Debugging features — packet capture, log analysis, troubleshooting |
| [docs/structure.md](docs/structure.md)                                     | Full project file structure                                        |

---

## 🤝 Contributing

Contributions are welcome. The codebase follows a modular handler architecture — adding support for a new device typically means adding or extending a cluster handler in `handlers/`. For devices that don't need complex logic, the [Device Onboarding](#device-onboarding-unsupported-devices) system can get things working without any code changes. See [docs/onboarding.md](docs/onboarding.md) for a step-by-step developer guide.

---

## 📄 License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.