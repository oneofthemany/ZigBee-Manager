# Matter Integration

## Overview

ZigBee Manager optionally supports **Matter** devices alongside Zigbee, presenting a unified device list across both protocols. Matter devices are controlled via [python-matter-server](https://github.com/home-assistant-libs/python-matter-server), which runs as a managed subprocess within the application — no Docker or external services required.

### How It Works

```
ZigBee Manager (single systemd service)
├── FastAPI / WebSocket UI          (port 8000)
├── ZigbeeService                   (zigpy/bellows → /dev/ttyACM0)
│   └── Zigbee mesh devices
├── EmbeddedMatterServer            (managed subprocess)
│   └── python-matter-server        (CHIP SDK, port 5580)
└── MatterBridge                    (ws://localhost:5580/ws)
    └── WiFi Matter devices
```

The **EmbeddedMatterServer** module spawns `python-matter-server` as a child process, monitors its health, streams its logs into the application logger, and auto-restarts on crash (up to 5 times with backoff). The **MatterBridge** connects to its WebSocket API, translates Matter nodes into the same device format as Zigbee devices, and feeds them through the same event pipeline (WebSocket updates, MQTT discovery, automation engine).

### What's Supported

| Feature               | Zigbee | Matter | Notes                                      |
|:----------------------|:-------|:-------|:-------------------------------------------|
| Device list           | ✅     | ✅     | Merged, protocol badge shown               |
| Pairing               | ✅     | ✅     | Separate flows, same UI dropdown           |
| On/Off/Brightness     | ✅     | ✅     | Routed by `matter_` IEEE prefix            |
| Color control         | ✅     | ✅     | Color temp (mireds/Kelvin)                 |
| Sensors               | ✅     | ✅     | Temperature, humidity, occupancy, contact  |
| Removal               | ✅     | ✅     | Routed by prefix                           |
| Rename                | ✅     | ✅     | Shared `names.json`                        |
| Automations           | ✅     | ✅     | Cross-protocol triggers work               |
| MQTT / HA Discovery   | ✅     | ✅     | Same patterns                              |
| WebSocket updates     | ✅     | ✅     | Same event pipeline                        |
| Mesh topology         | ✅     | ❌     | Thread mesh via OTBR (future)              |
| Groups                | ✅     | ❌     | Zigbee native only                         |
| Debug packets         | ✅     | ❌     | ZCL-level only                             |
| Bindings / Clusters   | ✅     | ❌     | Hidden for Matter devices                  |
| Touchlink             | ✅     | ❌     | Zigbee-only                                |

### Zero Overhead When Disabled

If `matter.enabled` is not set to `true` in `config.yaml`, no Matter code runs at all — no imports, no subprocess, no connections. All Zigbee functionality remains unchanged.

---

## Installation

### 1. Install the SDK

```bash
source /path/to/your/venv/bin/activate
pip install "python-matter-server[server]" --break-system-packages
```

Pre-built wheels are available for Linux **amd64** and **aarch64** (covers Rock 5B, Raspberry Pi 4/5). Python 3.11+ is required for the latest release.

### 2. Create the CHIP Data Directory

The CHIP SDK has a hardcoded `/data` path for its internal config files (separate from `--storage-path`):

```bash
sudo mkdir -p /data
sudo chown $(whoami):$(whoami) /data
```

Or symlink it to your application data:

```bash
sudo ln -s /opt/zigbee_manager/data/matter /data
```

### 3. Enable in config.yaml

```yaml
matter:
  enabled: true
  port: 5580
  storage_path: ./data/matter
```

### 4. Restart the Service

```bash
sudo systemctl restart zigbee-manager
```

You should see in the logs:

```
✅ Matter server started (PID xxxxx) on port 5580
[matter-server] CHIP Controller Stack initialized.
[matter-server] Matter Server successfully initialized.
✅ Reconnected to Matter server: ws://localhost:5580/ws
```

> **Automated install:** If deploying fresh, `deploy.sh` handles all of this — it prompts for Matter support during setup.

---

## Configuration

```yaml
matter:
  enabled: true                    # Start embedded server (default: false)
  port: 5580                       # WebSocket port (default: 5580)
  storage_path: ./data/matter      # Fabric/node persistence
  vendor_id: 0xFFF1                # Matter vendor ID (default: 0xFFF1)
  fabric_id: 1                     # Fabric ID (default: 1)
  bluetooth_adapter: 0             # BLE adapter index for commissioning (optional)
  log_level: info                  # Server log level: debug, info, warning, error
```

### External Server Mode

If you prefer running python-matter-server separately (Docker, standalone), disable the embedded server and point the bridge at the external URL:

```yaml
matter:
  enabled: false
  server_url: ws://192.168.1.100:5580/ws
```

---

## Commissioning Devices

### Via the Web UI

1. Click the **Pairing** dropdown in the navbar
2. Select **Commission Matter Device**
3. Enter the device's setup code (format: `MT:Y.ABCDEFG123456789` or numeric)
4. The device will appear in the device list with a Matter protocol badge

### Via the API

```bash
# Commission with setup code
curl -X POST http://localhost:8000/api/matter/commission \
  -H "Content-Type: application/json" \
  -d '{"code": "MT:Y.ABCDEFG123456789"}'

# Remove a Matter node
curl -X POST http://localhost:8000/api/matter/remove \
  -H "Content-Type: application/json" \
  -d '{"node_id": 1}'

# Check server + bridge status
curl http://localhost:8000/api/matter/status
```

### Commissioning Requirements

- **WiFi devices:** Must be on the same network as the server. IPv6 must be enabled.
- **BLE commissioning:** Requires a Bluetooth adapter on the host. Set `bluetooth_adapter: 0` in config.
- **Thread devices:** Require an OpenThread Border Router (OTBR). Not covered here — see the [Thread / MultiPAN](#thread--multipan-future) section.

---

## Device Routing

Matter and Zigbee devices coexist in the same device list. Routing is determined by the IEEE address prefix:

- `matter_42` → MatterBridge handles commands
- `00:11:22:...` → ZigbeeService handles commands

This applies to all operations: commands, removal, rename, and automation actions. The routing is transparent — the UI and automation engine don't need to know which protocol a device uses.

### Supported Matter Clusters

| Cluster              | ID     | Capabilities                        |
|:---------------------|:-------|:------------------------------------|
| OnOff                | 6      | On, Off, Toggle                     |
| LevelControl         | 8      | Brightness (0–254)                  |
| ColorControl         | 768    | Color temperature (mireds)          |
| TemperatureMeasurement | 1026 | Temperature sensor                  |
| RelativeHumidity     | 1029   | Humidity sensor                     |
| OccupancySensing     | 1030   | Occupancy / motion                  |
| IlluminanceMeasurement | 1024 | Light level sensor                  |
| BooleanState         | 69     | Contact / door sensor               |

---

## Architecture

### Module: `modules/matter_server.py` — EmbeddedMatterServer

Manages python-matter-server as a child process:

- Spawns via `asyncio.create_subprocess_exec`
- Streams stdout/stderr to the application logger (prefixed `[matter-server]`)
- Auto-restarts on crash with exponential backoff (5s → 10s → 15s → 20s → 30s, max 5 attempts)
- Graceful shutdown: SIGTERM → 10s timeout → SIGKILL
- Process group isolation via `os.setpgrp` (child dies with parent)

### Module: `modules/matter_bridge.py` — MatterBridge

WebSocket client to python-matter-server:

- Reconnects with exponential backoff (5s → 60s cap)
- Translates Matter node attributes to unified device state dicts
- Publishes MQTT discovery and state updates using the same patterns as Zigbee
- Fires WebSocket events through the shared `broadcast_event()` pipeline
- Device wrapper class `MatterDevice` implements `to_device_list_entry()` matching `ZigManDevice`

### Event Flow

```
Matter device state change
  → python-matter-server detects via subscription
  → WebSocket message to MatterBridge
  → MatterBridge updates internal state
  → broadcast_event() → WebSocket to UI
  → MQTT publish → Home Assistant
  → Automation engine evaluation
```

---

## Networking Requirements

Matter relies on IPv6 multicast for device discovery and communication:

- **IPv6 enabled** on the host network interface
- **Same VLAN** for Matter devices, the server, and commissioning devices
- **No IGMP snooping** or multicast optimisation on the switch/router (or configure exceptions)
- **No mDNS forwarders** — these corrupt Matter packets

Verify IPv6 is working:

```bash
# Check IPv6 is enabled
ip -6 addr show
# Should show a link-local address (fe80::...)

# Check mDNS resolution
avahi-resolve -n _matter._tcp.local
```

---

## Troubleshooting

### Server won't start: "CHIP handle has not been initialized!"

The CHIP SDK requires its own process — it cannot share the main thread with uvicorn. This is why the embedded server runs as a subprocess. If you see this error, the old in-process approach is still in use. Ensure `modules/matter_server.py` uses the subprocess-based `EmbeddedMatterServer`.

### Server crashes: "Failed to create temp file /data/chip_factory.ini"

The CHIP SDK writes config files to `/data`. Create the directory:

```bash
sudo mkdir -p /data
sudo chown $(whoami):$(whoami) /data
```

### Bridge can't connect: "Connect call failed ('127.0.0.1', 5580)"

The server hasn't finished initialising. Check:

1. Logs for `[matter-server]` lines — is the server starting at all?
2. The server takes ~3-5 seconds to initialise (fetches PAA certs, vendor info)
3. The bridge auto-reconnects — it should connect within 5–60 seconds

### Server exits with code -6

SIGABRT from the CHIP SDK. Common causes:

- `/data` directory missing or wrong permissions
- Another instance already running on port 5580
- Corrupted chip state files — try: `rm -rf /data/chip_*.ini && rm -rf ./data/matter/*`

### Commissioning fails

- **WiFi devices:** Ensure device is in pairing mode and on the same network
- **BLE:** Ensure `bluetooth_adapter: 0` is set and the adapter is available (`hciconfig`)
- **Setup code:** Must match exactly — QR code format `MT:...` or numeric PIN

### Log Inspection

Matter server logs are prefixed and routed through the application logger:

```bash
# All Matter-related logs
grep "matter" /opt/zigbee_manager/logs/zigbee.log

# Just the server subprocess output
grep "\[matter-server\]" /opt/zigbee_manager/logs/zigbee.log

# Bridge connection status
grep "matter_bridge" /opt/zigbee_manager/logs/zigbee.log
```

---

## API Reference

| Endpoint                      | Method | Description                          |
|:------------------------------|:-------|:-------------------------------------|
| `/api/matter/status`          | GET    | Server + bridge status               |
| `/api/matter/commission`      | POST   | Commission device `{"code": "..."}` |
| `/api/matter/remove`          | POST   | Remove node `{"node_id": N}`        |
| `/api/devices`                | GET    | Merged Zigbee + Matter device list   |
| `/api/device/{ieee}/command`  | POST   | Routed by `matter_` prefix           |
| `/api/device/{ieee}/remove`   | POST   | Routed by `matter_` prefix           |
| `/api/device/{ieee}/rename`   | POST   | Routed by `matter_` prefix           |

---

## Thread / MultiPAN (Future)

Matter-over-Thread devices require an **OpenThread Border Router** (OTBR) to translate between Thread mesh and IPv6. This is a separate layer:

```
/dev/ttyACM0 → MultiPAN (cpcd) → zigbeed → Zigbee devices
                                → ot-ctl  → Thread devices → OTBR → Matter
```

Full MultiPAN support (sharing a single radio for both Zigbee and Thread) requires:

1. Silicon Labs MultiPAN RCP firmware on the coordinator
2. `cpcd` (CPC daemon) for radio multiplexing
3. `zigbeed` replacing direct bellows access
4. OTBR container for Thread border routing

This is planned for future development. Currently, Matter-over-WiFi works without any additional hardware.