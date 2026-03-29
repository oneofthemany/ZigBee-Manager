# MultiPAN RCP — Concurrent Zigbee + Thread

## Overview

ZigBee Matter Manager automatically supports **MultiPAN RCP firmware** on Silicon Labs coordinators (Sonoff MG24, SkyConnect, etc.). When the Dongle Jedi detects RCP firmware, it transparently starts the CPC daemon stack and redirects bellows to communicate via a socket instead of direct serial — no manual configuration required.

### How It Works

```
ZigBee Matter Manager (single systemd service)
├── FastAPI / WebSocket UI              (port 8000)
├── MultiPanManager                     (managed subprocess orchestrator)
│   ├── cpcd                            (CPC daemon — serial multiplexer)
│   │   └── /dev/ttyACM0               (RCP firmware dongle)
│   ├── zigbeed                         (host-side Zigbee EmberZNet stack)
│   ├── socat                           (PTY → TCP bridge)
│   │   └── TCP :9999                   (EZSP/ASH over TCP)
│   └── otbr-agent                      (OpenThread Border Router)
│       └── Thread mesh + border routing
├── ZigbeeService                       (zigpy/bellows → socket://localhost:9999)
│   └── Zigbee mesh devices             (unchanged from NCP mode)
├── EmbeddedMatterServer                (managed subprocess, optional)
│   └── WiFi + Thread Matter devices
└── MatterBridge                        (ws://localhost:5580/ws)
    └── Unified device list
```

### Architecture Comparison

| Component        | NCP Mode (standard)          | MultiPAN RCP Mode                       |
|:-----------------|:-----------------------------|:----------------------------------------|
| Radio firmware   | EmberZNet NCP (EZSP)         | MultiPAN RCP (802.15.4)                 |
| Serial owner     | bellows (direct)             | cpcd (multiplexer)                      |
| Zigbee stack     | On-chip (EFR32)              | zigbeed (host daemon)                   |
| bellows connects | `/dev/ttyACM0`               | `socket://localhost:9999`               |
| Thread support   | None                         | otbr-agent via cpcd                     |
| Detection        | Jedi → "EZSP (Ember)"       | Jedi → "CPC Multi-PAN (RCP)"           |
| Config needed    | None                         | None (auto-detected)                    |

### Zero Overhead When Not Detected

If the dongle has standard NCP firmware, MultiPAN code never runs. The `MultiPanManager` is only instantiated when Dongle Jedi detects CPC Multi-PAN firmware. No imports, no subprocess spawning, no socket redirection.

---

## Automatic Detection

The Dongle Jedi already includes a `CPCMultiPANProbe` class that detects RCP firmware by sending CPC protocol frames. When detected, the startup flow changes:

```
Standard NCP:                          MultiPAN RCP:
                                       
Jedi → EZSP (Ember)                   Jedi → CPC Multi-PAN (RCP)
  ↓                                      ↓
probe_result.radio_type = "EZSP"       probe_result.adapter_family = "...CPC..."
  ↓                                      ↓
bellows → /dev/ttyACM0                 MultiPanManager.start()
  ↓                                      ├── cpcd → /dev/ttyACM0
Done                                     ├── zigbeed → cpcd
                                         ├── socat → TCP :9999
                                         └── otbr-agent → cpcd (Thread)
                                           ↓
                                         self.port = socket://localhost:9999
                                           ↓
                                         bellows → socket://localhost:9999
                                           ↓
                                         Done (everything else unchanged)
```

---

## Prerequisites

### Required Packages

```bash
# CPC daemon (serial multiplexer)
sudo apt-get install cpcd

# Zigbee host daemon
sudo apt-get install zigbeed

# PTY-to-TCP bridge (usually pre-installed)
sudo apt-get install socat

# OpenThread Border Router (optional, for Thread support)
sudo apt-get install otbr-agent
```

### RCP Firmware

The coordinator must be flashed with MultiPAN RCP firmware:

```bash
# Using universal-silabs-flasher
pip install universal-silabs-flasher --break-system-packages
universal-silabs-flasher --device /dev/ttyACM0 flash \
    --firmware rcp-uart-802154-your-board.gbl
```

RCP firmware images are available from:
- [NabuCasa firmware repository](https://github.com/NabuCasa/silabs-firmware)
- Silicon Labs Simplicity Studio

---

## Configuration (Optional)

MultiPAN activates automatically — no configuration is needed for default setups. Add a `multipan` section to `config.yaml` only to override defaults:

```yaml
# MultiPAN RCP overrides (auto-detected — only add to customise)
multipan:
  cpcd:
    # serial_port auto-detected from zigbee.port
    baudrate: 115200
    flow_control: hardware
  zigbeed:
    ezsp_port: 9999       # TCP port for bellows connection
  otbr:
    enabled: true          # Set false to disable Thread
    thread_interface: wpan0
    backbone_interface: eth0
    nat64: false
```

---

## Migration from NCP

1. Stop the service: `sudo systemctl stop zigbee-matter-manager`
2. Flash RCP MultiPAN firmware onto the coordinator
3. Install prerequisites: `sudo apt-get install cpcd zigbeed socat otbr-agent`
4. Start the service — detection is automatic
5. **Zigbee network is preserved** — network key, PAN ID, and device pairings carry over because zigbeed maintains the same EmberZNet state

---

## Troubleshooting

### cpcd won't start: "Failed to open serial port"

Another process is holding the serial port. Check:
```bash
sudo lsof /dev/ttyACM0
# Kill any process holding it, or check if a previous cpcd is still running
sudo systemctl stop cpcd  # If installed as a system service
```

### zigbeed won't start: "CPC endpoint not available"

cpcd hasn't fully initialised. The MultiPanManager waits 1 second after cpcd starts, but on slow systems this may not be enough. Check cpcd logs for "Daemon startup was successful".

### bellows can't connect to socket

Verify the socat bridge is running and the TCP port is accepting connections:
```bash
# Check socat is running
ps aux | grep socat

# Test TCP connection
nc -zv 127.0.0.1 9999
```

### Thread not working but Zigbee is fine

otbr-agent failure is non-blocking — Zigbee continues working. Check:
```bash
# Check otbr-agent logs
grep "otbr-agent" /opt/zigbee_matter_manager/logs/zigbee.log

# Verify Thread interface
sudo ot-ctl state
```

### Log Inspection

All MultiPAN daemon logs are prefixed and routed through the application logger:
```bash
# All MultiPAN logs
grep -E "\[(cpcd|zigbeed|socat|otbr-agent)\]" logs/zigbee.log

# Just cpcd
grep "\[cpcd\]" logs/zigbee.log

# Just zigbeed
grep "\[zigbeed\]" logs/zigbee.log
```

---

## API

| Endpoint                | Method | Description                    |
|:------------------------|:-------|:-------------------------------|
| `/api/multipan/status`  | GET    | MultiPAN stack status + daemon PIDs |

Response example:
```json
{
  "enabled": true,
  "running": true,
  "ezsp_socket": "socket://127.0.0.1:9999",
  "prerequisites": {
    "cpcd": true,
    "zigbeed": true,
    "socat": true,
    "otbr_agent": true,
    "core_available": true,
    "all_available": true
  },
  "daemons": {
    "cpcd": {"name": "cpcd", "running": true, "pid": 1234, "restart_count": 0},
    "zigbeed": {"name": "zigbeed", "running": true, "pid": 1235, "restart_count": 0},
    "socat": {"name": "socat", "running": true, "pid": 1236, "restart_count": 0},
    "otbr-agent": {"name": "otbr-agent", "running": true, "pid": 1237, "restart_count": 0}
  }
}
```

---

## Future — Phase 2 (Python-Native RCP with Rust Hot Path)

Phase 1 uses Silicon Labs' cpcd/zigbeed daemons as managed subprocesses. Phase 2 would replace them with a Python + Rust hybrid:

- **Rust native module** (via PyO3): Owns serial port, handles HDLC framing, Spinel encode/decode, and the TX drain loop on a dedicated OS thread. Sub-millisecond latency on the radio hot path.
- **Python scheduler**: Application-aware TDM scheduling — can boost Zigbee priority during TRV calibrations, protect OTA transfers, batch same-channel frames.
- **Frame classifier**: Routes received 802.15.4 frames to Zigbee or Thread stacks based on PAN ID and NWK header inspection.

This removes the dependency on proprietary cpcd/zigbeed binaries and enables scheduling decisions informed by application-layer knowledge.