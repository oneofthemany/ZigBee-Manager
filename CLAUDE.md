# CLAUDE.md

## Project Overview

ZigBee & Matter Manager is a self-hosted Python gateway that manages Zigbee and Matter mesh networks, bridging devices to Home Assistant via MQTT Discovery. It provides a real-time web UI for device management, OTA firmware updates, automation, presence detection, and mesh visualization.

## Tech Stack

- **Backend**: Python 3.8+ with FastAPI + Uvicorn (async/await throughout)
- **Zigbee**: zigpy 1.1.1, bellows 0.49.0 (EZSP), zigpy-znp 0.14.3 (ZNP), zha-quirks
- **Matter**: python-matter-server (optional, managed subprocess)
- **MQTT**: aiomqtt (async client)
- **Frontend**: Vanilla JS + Bootstrap 5 + D3.js (no bundler, served as static files)
- **Database**: SQLite (zigpy-managed `zigbee.db`), DuckDB (optional telemetry)
- **Deployment**: Podman/Docker container via `build.sh`, systemd service

## Repository Structure

```
main.py              # FastAPI entry point, route registration, startup/shutdown
device.py            # ZigManDevice wrapper around zigpy devices
mqtt.py              # MQTT service (connection, publish, subscribe)
boot_guard.py        # Pre-startup rollback system (runs via systemd ExecStartPre)
models.py            # Pydantic request models

core/                # ZigbeeService composed from mixins
  service.py         #   Main service (1600+ lines) - coordinator lifecycle
  config_builder.py  #   Radio config builders (EZSP/ZNP)
  mqtt_handler.py    #   Birth messages, republish
  topology.py        #   Mesh data, LQI scanning
  banning.py         #   Device banning
  database.py        #   Orphan cleanup
  tabs.py            #   Device tabs
  polling.py         #   Periodic device polling

handlers/            # Cluster handlers for Zigbee protocol
  base.py            #   Base ClusterHandler class
  generic.py         #   Fallback for unmapped clusters
  aqara.py           #   Aqara/Xiaomi (0xFCC0)
  tuya.py            #   Tuya (0xEF00)
  lighting.py        #   Lights/color control
  sensors.py         #   Temperature, humidity, occupancy
  security.py        #   IAS Zone, door locks
  hvac.py            #   Climate/thermostat
  blinds.py          #   Window coverings
  switches.py        #   On/off switches
  ...                #   More per-manufacturer/function handlers

modules/             # Feature and service modules
  automation.py      #   State-machine automation engine
  groups.py          #   Native Zigbee groups
  zones.py           #   RSSI-based presence detection
  ota.py             #   OTA firmware updates (multi-provider)
  matter_bridge.py   #   Matter WebSocket bridge
  mqtt_explorer.py   #   MQTT debugging tool
  mqtt_queue.py      #   Non-blocking publish queue
  resilience.py      #   NCP failure recovery, watchdog
  error_handler.py   #   Retry decorators, CommandWrapper
  safe_deploy.py     #   Safe code deployment with rollback
  json_helpers.py    #   JSON serialization utilities
  zigbee_debug.py    #   Packet-level debugging
  network_init.py    #   Network credential generation
  ai_assistant.py    #   AI assistant integration (Ollama)
  telemetry_*.py     #   DuckDB telemetry collection
  ...

routes/              # FastAPI route handlers
  config_routes.py   #   Config, spectrum, credentials
  device_routes.py   #   Device CRUD, commands, banning
  network_routes.py  #   Mesh, topology, packet stats
  system_routes.py   #   Debug, restart, HA status
  matter_routes.py   #   Matter commission/remove/status
  websocket_routes.py#   WebSocket connection manager
  ota_routes.py      #   OTA firmware endpoints
  group_routes.py    #   Group management
  editor_routes.py   #   In-app code editor

config/              # Runtime configuration
  config.yaml        #   Primary config (MQTT, Zigbee, Matter, logging, web)
  zones.yaml         #   Zone-based presence detection config

data/                # Runtime data (JSON persistence)
  device_settings.json
  device_state_cache.json
  device_overrides.json
  banned_devices.json

static/              # Web UI (SPA)
  index.html         #   Main HTML shell
  js/                #   Vanilla JS modules (no framework)
  css/               #   Stylesheets (dark mode, mobile, component-specific)
  sw.js              #   Service worker (PWA support)

docs/                # Markdown documentation
```

## Key Architectural Patterns

### Cluster Handler Registration

Handlers register via decorator and are looked up by cluster ID:

```python
@register_handler(cluster_id)
class MyHandler(ClusterHandler):
    ...
```

Registry: `HANDLER_REGISTRY[cluster_id] -> handler_class`

### ZigbeeService Mixin Composition

`core/service.py` inherits from focused mixins (`ConfigBuilderMixin`, `MQTTHandlerMixin`, `TopologyMixin`, `BanningMixin`, `DatabaseMixin`, `TabsMixin`).

### Route Registration

Each route module exposes `register_*_routes(app, ...)` called from `main.py`.

### State Management

- Per-device state dict: `device.state`
- Delta-only MQTT publishing (only changed attributes)
- Attribute source tracking to detect duplicates

### Async Patterns

- Non-blocking logging via `QueueListener` + `QueueHandler`
- `MQTTPublishQueue` prevents event loop stalls
- `FastPathProcessor` for latency-critical sensors
- `with_retries` decorator for exponential backoff

## Running the Application

```bash
# Direct (development)
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000

# Container deployment
./build.sh
```

Internal port is always 8000. `build.sh` handles container build, external port auto-detection, systemd service creation, and device access (dialout group).

## Configuration

Primary config: `config/config.yaml` (YAML). Covers MQTT, Zigbee radio settings (auto-detected: EZSP/ZNP/deconz), Matter, logging, web server, AI. Network credentials (PAN ID, network key, channel) are auto-generated on first boot.

## Testing & Safety

There are no unit tests. Safety is provided by:

- **Boot guard** (`boot_guard.py`): Runs before app start, detects failed deployments, auto-rolls back from `.editor_backups/`
- **Safe deploy** (`modules/safe_deploy.py`): Confirms changes within 120s or rolls back
- **Resilience module** (`modules/resilience.py`): NCP failure watchdog with automatic recovery

## Linting & Formatting

No linting or formatting tools are configured. No flake8, black, mypy, or eslint.

## CI/CD

No GitHub Actions or CI pipelines. Deployment is manual via `build.sh` (container) or systemd.

## Dependencies

All in `requirements.txt` (no dev dependencies declared):
fastapi, uvicorn, zigpy, bellows, aiomqtt, pyyaml, zha-quirks, pydantic, zigpy-znp, aiohttp, python-matter-server, python-multipart, duckdb

## Conventions for AI Assistants

- **Read before editing**: Always read files before modifying. The codebase has large files (service.py ~1600 lines, device.py ~1200 lines).
- **Follow the mixin pattern**: New core functionality should be added as a mixin in `core/`, not directly in `service.py`.
- **Follow the handler pattern**: New device support goes in `handlers/` using `@register_handler` and extending `ClusterHandler`.
- **Follow the route pattern**: New API endpoints go in `routes/` with a `register_*_routes(app, ...)` function called from `main.py`.
- **Feature modules go in `modules/`**: Keep modules focused on a single concern.
- **Async-first**: All I/O should be async. Use the existing patterns (`with_retries`, `MQTTPublishQueue`, etc.).
- **No test suite to run**: There are no automated tests. Validate changes by reading code carefully and checking for import/syntax errors.
- **YAML config**: Do not modify `config/config.yaml` directly - it's user data. Reference it for understanding config structure.
- **JSON data files**: Files in `data/` are runtime state. Do not commit them with real device data.
- **Static frontend**: JS/CSS changes take effect immediately (no build step). The frontend uses no framework - vanilla JS with Bootstrap 5.
- **Logging**: Use the existing non-blocking logging setup. Don't add `print()` statements.
