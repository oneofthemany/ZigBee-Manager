"""
ZigBee Manager - Main Application
FastAPI-based web server for ZigBee device management.
"""
import uvicorn
import subprocess
import json
import yaml
import os
import sys
import logging
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import queue
import asyncio
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, Any
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import random


# Import services
from core import ZigbeeService
from mqtt import MQTTService
from modules.zigbee_debug import get_debugger
from modules.json_helpers import prepare_for_json, safe_json_dumps
from modules.groups import GroupManager
from modules.mqtt_explorer import MQTTExplorer
from modules.zones_api import register_zone_routes
from modules.zones import ZoneConfig
from modules.zone_device_config import configure_zone_device_reporting, remove_aggressive_reporting
from modules.automation_api import register_automation_routes
from modules.network_init import (
    ensure_network_credentials, generate_pan_id,
    generate_extended_pan_id, generate_network_key,
    select_best_channel, ZIGBEE_CHANNELS
)
from modules.spectrum_monitor import SpectrumMonitor, get_history, get_channel_averages, save_scan



# ============================================================================
# LOGGING CONFIGURATION (NON-BLOCKING)
# ============================================================================

os.makedirs("logs", exist_ok=True)

# 1. Create a queue for logs
log_queue = queue.Queue(-1) # Unlimited size

# 2. Setup the actual handlers (File & Console)
file_handler = RotatingFileHandler('logs/zigbee.log', maxBytes=1024*1024, backupCount=3)
console_handler = logging.StreamHandler()

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# 3. Create the Listener (Runs in a separate thread)
# It reads from the queue and writes to the file/console
log_listener = QueueListener(log_queue, file_handler, console_handler)

# 4. Configure the root logger to write to the Queue (Instant)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Remove default handlers to avoid duplication
root_logger.handlers = []

# Add the non-blocking QueueHandler
queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

# Set debug level for specific modules
logging.getLogger('handlers').setLevel(logging.INFO)
logging.getLogger('handlers.base').setLevel(logging.INFO) # Set base handler to INFO
logging.getLogger('core').setLevel(logging.INFO)
logging.getLogger('device').setLevel(logging.INFO)

logger = logging.getLogger('main')


# ============================================================================
# CONFIGURATION
# ============================================================================

def load_config():
    """
Load
configuration
from config.yaml.

"""
    if not os.path.exists("./config/config.yaml"):
        return {}
    with open("./config/config.yaml", 'r') as f:
        return yaml.safe_load(f) or {}


CONFIG = load_config()


def get_conf(section, key, default=None):
    """
Get
configuration
value.
"""
    return CONFIG.get(section, {}).get(key, default)


# ============================================================================
# PYDANTIC MODELS FOR API
# ============================================================================

class StructuredConfigRequest(BaseModel):
    zigbee: dict
    mqtt: dict
    web: dict
    logging: dict

class DeviceRequest(BaseModel):
    ieee: str
    force: Optional[bool] = False
    ban: bool = False
    aggressive: Optional[bool] = None  # Only used by reconfigure


class RenameRequest(BaseModel):
    ieee: str
    name: str


class ConfigureRequest(BaseModel):
    ieee: str
    qos: Optional[int] = None
    polling_interval: Optional[int] = None
    reporting: Optional[dict] = None
    tuya_settings: Optional[dict] = None
    updates: Optional[dict] = None


class CommandRequest(BaseModel):
    ieee: str
    command: str
    value: Optional[Any] = None
    endpoint: Optional[int] = None


class AttributeReadRequest(BaseModel):
    ieee: str
    endpoint_id: int
    cluster_id: int
    attribute: str


class BindRequest(BaseModel):
    source_ieee: str
    target_ieee: str
    cluster_id: int

# For config file updates
class ConfigUpdateRequest(BaseModel):
    content: str


class PermitJoinRequest(BaseModel):
    duration: int = 240
    target_ieee: Optional[str] = None


class BanRequest(BaseModel):
    ieee: str
    reason: Optional[str] = None

class UnbanRequest(BaseModel):
    ieee: str


class TouchlinkRequest(BaseModel):
    ieee: Optional[str] = None
    channel: Optional[int] = None


# ============================================================================
# WEBSOCKET CONNECTION MANAGER
# ============================================================================

class ConnectionManager:
    """
Manages
WebSocket
connections
for real - time updates."""

    def __init__(self):
        self.active_connections = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients with safe JSON serialization."""
        if not self.active_connections:
            return

        try:
            # Sanitize message for JSON serialisation
            safe_message = prepare_for_json(message)
            json_msg = json.dumps(safe_message)
        except Exception as e:
            logger.error(f"Failed to serialise broadcast message: {e}")
            return

        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_text(json_msg)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for ws in disconnected:
            self.disconnect(ws)


manager = ConnectionManager()


async def broadcast_event(event_type: str, data: dict):
    """Helper to broadcast events via WebSocket."""
    await manager.broadcast({"type": event_type, "payload": data})


# ============================================================================
# SERVICES INITIALIZATION
# ============================================================================

mqtt_service = MQTTService(
    broker_host=get_conf('mqtt', 'broker_host', 'localhost'),
    port=get_conf('mqtt', 'broker_port', 1883),
    username=get_conf('mqtt', 'username'),
    password=get_conf('mqtt', 'password'),
    base_topic=get_conf('mqtt', 'base_topic', 'zigbee_ha'),
    qos=get_conf('mqtt', 'qos', 0),
    log_callback=None
)

zigbee_service = ZigbeeService(
    port=get_conf('zigbee', 'port', '/dev/ttyACM0'),
    mqtt_client=mqtt_service,
    config=CONFIG.get('zigbee', {}),
    event_callback=broadcast_event
)


# ============================================================================
# APPLICATION LIFECYCLE
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown handling."""

    # 1. Start the Threaded Log Listener
    log_listener.start()
    logger.info("Starting Zigbee Gateway (Threaded Logging Enabled)...")

    # --- WIRING UP DEBUGGER TO WEBSOCKET ---
    async def debug_callback(packet_data):
        # Stream debug packet to frontend
        await manager.broadcast({
            "type": "debug_packet",
            "payload": packet_data
        })

    # Register the callback with the singleton debugger
    debugger = get_debugger()
    if debugger:
        debugger.add_callback(debug_callback)
        logger.info("Registered debug callback for live streaming")

    # Broadcast startup message
    await manager.broadcast({
        "type": "log",
        "payload": {"level": "INFO", "message": "System Starting...", "timestamp": None}
    })

    # Start MQTT (non-blocking)
    try:
        await mqtt_service.start()
        logger.info("MQTT connected")
    except Exception as e:
        logger.warning(f"MQTT connection failed: {e}")


    # Initialize MQTT Explorer
    mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)
    logger.info("MQTT Explorer initialized")

    # Register WebSocket callback
    async def mqtt_explorer_callback(message_record):
        await manager.broadcast({
            "type": "mqtt_message",
            "payload": message_record
        })
    mqtt_service.mqtt_explorer.add_callback(mqtt_explorer_callback)

    # Start Zigbee service
    ensure_network_credentials("./config/config.yaml")
    network_key = get_conf('zigbee', 'network_key', None)
    asyncio.create_task(zigbee_service.start(network_key=network_key))

    # Start Spectrum Monitor
    async def _start_spectrum_monitor(svc):
        for _ in range(30):
            if svc.app:
                svc.spectrum_monitor.start()
                return
            await asyncio.sleep(5)
        logger.warning("Spectrum monitor: radio never ready, skipping")

    # Start background spectrum monitor
    spectrum_interval = get_conf('zigbee', 'spectrum_scan_interval', 3600)
    if spectrum_interval > 0:
        zigbee_service.spectrum_monitor = SpectrumMonitor(
            app_getter=lambda: zigbee_service.app,
            interval=spectrum_interval
        )
        asyncio.create_task(_start_spectrum_monitor(zigbee_service))

    # Initialize group manager
    zigbee_service.group_manager = GroupManager(zigbee_service)
    logger.info("Group manager initialized")


    # wire handler to the MQTT Service
    mqtt_service.group_command_callback = zigbee_service.group_manager.handle_mqtt_group_command
    logger.info("Wired GroupManager callback to MQTT Service")


    # Initialize Zone Manager
    register_zone_routes(
        app,
        lambda: zigbee_service.zone_manager,
        lambda: zigbee_service.devices
    )
    logger.info("Zone API routes registered")

    # Initialize Automation API
    register_automation_routes(
        app,
        lambda: zigbee_service.automation
    )
    logger.info("Automation API routes registered")

    yield  # Application runs here

    # Shutdown
    logger.info("Shutting down Zigbee Gateway...")
    await zigbee_service.stop()
    await mqtt_service.stop()
    if hasattr(zigbee_service, 'spectrum_monitor'):
        zigbee_service.spectrum_monitor.stop()

    # Stop log listener
    log_listener.stop()


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="Zigbee Gateway",
    description="ZHA-style Zigbee device management",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================================
# ROUTES - STATIC FILES
# ============================================================================

@app.get("/")
async def read_index():
    """Serve the main UI."""
    return FileResponse('static/index.html')


# ============================================================================
# ROUTES - STRUCTURED CONFIGURATION
# ============================================================================

@app.get("/api/config/structured")
async def get_structured_config():
    """Return config as structured JSON for the rich settings UI."""
    try:
        with open("./config/config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

        zigbee = cfg.get("zigbee", {})

        # Normalise network_key and extended_pan_id for display
        def key_to_hex(k):
            if isinstance(k, list):
                return "".join(f"{b:02X}" for b in k)
            return str(k) if k else ""

        def epan_to_hex(v):
            if isinstance(v, list):
                return "".join(f"{b:02X}" for b in v)
            return str(v) if v else ""

        return {
            "success": True,
            "config": {
                "zigbee": {
                    "port": zigbee.get("port", ""),
                    "radio_type": zigbee.get("radio_type", "auto"),
                    "channel": zigbee.get("channel", 15),
                    "pan_id": zigbee.get("pan_id", ""),
                    "extended_pan_id_hex": epan_to_hex(zigbee.get("extended_pan_id")),
                    "network_key_hex": key_to_hex(zigbee.get("network_key")),
                    "topology_scan_interval": zigbee.get("topology_scan_interval", 120),
                    "coordinator_type": zigbee.get("coordinator_type", ""),
                },
                "mqtt": cfg.get("mqtt", {}),
                "web": {k: v for k, v in cfg.get("web", {}).items() if k != "ssl"},
                "web_ssl": cfg.get("web", {}).get("ssl", {}),
                "logging": cfg.get("logging", {}),
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/config/structured")
async def save_structured_config(data: dict):
    """
    Save structured config back to YAML.
    Accepts the same shape returned by GET /api/config/structured.
    Preserves all advanced keys (ezsp, znp, stability settings, etc.)
    """
    try:
        with open("./config/config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

        incoming = data.get("config", data)

        # --- MQTT ---
        if "mqtt" in incoming:
            cfg.setdefault("mqtt", {}).update(incoming["mqtt"])

        # --- Web ---
        if "web" in incoming:
            cfg.setdefault("web", {}).update(incoming["web"])
        if "web_ssl" in incoming:
            cfg.setdefault("web", {}).setdefault("ssl", {}).update(incoming["web_ssl"])

        # --- Logging ---
        if "logging" in incoming:
            cfg.setdefault("logging", {}).update(incoming["logging"])

        # --- Zigbee (merge carefully to preserve advanced keys) ---
        if "zigbee" in incoming:
            z = incoming["zigbee"]
            zigbee_cfg = cfg.setdefault("zigbee", {})

            for simple_key in ("port", "radio_type", "channel", "topology_scan_interval", "coordinator_type"):
                if simple_key in z and z[simple_key] != "" and z[simple_key] is not None:
                    zigbee_cfg[simple_key] = z[simple_key]

            if z.get("pan_id"):
                zigbee_cfg["pan_id"] = z["pan_id"]

            # Convert hex strings back to byte arrays
            if z.get("extended_pan_id_hex"):
                h = z["extended_pan_id_hex"].replace(" ", "").replace(":", "")
                zigbee_cfg["extended_pan_id"] = [int(h[i:i+2], 16) for i in range(0, len(h), 2)]

            if z.get("network_key_hex"):
                h = z["network_key_hex"].replace(" ", "").replace(":", "")
                zigbee_cfg["network_key"] = [int(h[i:i+2], 16) for i in range(0, len(h), 2)]

        with open("./config/config.yaml", "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        logger.info("Structured config saved via API")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to save structured config: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# ROUTES - SPECTRUM ANALYSIS & CHANNEL MANAGEMENT
# ============================================================================

@app.get("/api/zigbee/spectrum")
async def get_spectrum():
    """
    Perform a ZigBee energy scan across all 2.4GHz channels (11-26).
    Requires the Zigbee network to be running.
    Returns energy levels 0-255 per channel (higher = more interference).
    """
    try:
        if not zigbee_service.app:
            return {"success": False, "error": "Zigbee network not started"}

        logger.info("Starting spectrum energy scan...")
        results = await zigbee_service.app.energy_scan(
            channels=range(11, 27),
            count=3,
            duration_exp=4
        )

        spectrum = {int(ch): int(energy) for ch, energy in results.items()}

        # Persist to database alongside background scans
        save_scan(spectrum)

        best = select_best_channel(spectrum)

        current = None
        if zigbee_service.app and hasattr(zigbee_service.app.state, 'network_info'):
            current = getattr(zigbee_service.app.state.network_info, 'channel', None)

        return {
            "success": True,
            "spectrum": spectrum,
            "best_channel": best,
            "current_channel": current,
            "channels": list(range(11, 27))
        }
    except Exception as e:
        logger.error(f"Spectrum scan failed: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/zigbee/channel/auto")
async def auto_select_channel():
    """Run energy scan, pick the best channel, write to config. Requires restart to apply."""
    try:
        scan_result = await get_spectrum()
        if not scan_result.get("success"):
            return scan_result

        best = scan_result["best_channel"]
        spectrum = scan_result["spectrum"]

        # Write to config
        with open("./config/config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("zigbee", {})["channel"] = best
        with open("./config/config.yaml", "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Auto channel selection: channel {best} written to config")
        return {
            "success": True,
            "selected_channel": best,
            "spectrum": spectrum,
            "message": f"Channel {best} selected and saved. Restart service to apply."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/zigbee/spectrum/history")
async def get_spectrum_history(hours: int = 24):
    """
    Return raw spectrum scan records for the past N hours.
    Used to draw the historical trend chart in the UI.
    Max 168 hours (7 days).
    """
    hours = min(hours, 168)
    try:
        records = get_history(hours=hours)
        return {"success": True, "hours": hours, "records": records}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/zigbee/spectrum/averages")
async def get_spectrum_averages(hours: int = 24):
    """
    Return average energy per channel for the past N hours.
    Used for the 'average interference' overlay in the spectrum tab.
    """
    hours = min(hours, 168)
    try:
        averages = get_channel_averages(hours=hours)
        return {"success": True, "hours": hours, "averages": averages}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/zigbee/spectrum/scan-now")
async def trigger_background_scan():
    """Trigger an immediate background scan and store results."""
    try:
        monitor = getattr(zigbee_service, 'spectrum_monitor', None)
        if not monitor:
            return {"success": False, "error": "Spectrum monitor not running"}
        results = await monitor.run_scan_now()
        return {"success": True, "spectrum": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================================================
# ROUTES - CREDENTIAL REGENERATION
# ============================================================================

@app.post("/api/zigbee/credentials/regenerate")
async def regenerate_credentials(data: dict):
    """
    Regenerate one or more network credentials and write to config.
    Body: {"pan_id": true, "extended_pan_id": true, "network_key": true}
    WARNING: Regenerating network_key will require re-pairing all devices.
    """
    try:
        regen = data  # {"pan_id": bool, "extended_pan_id": bool, "network_key": bool}

        with open("./config/config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

        z = cfg.setdefault("zigbee", {})
        regenerated = {}

        if regen.get("pan_id"):
            z["pan_id"] = generate_pan_id()
            regenerated["pan_id"] = z["pan_id"]

        if regen.get("extended_pan_id"):
            z["extended_pan_id"] = generate_extended_pan_id()
            regenerated["extended_pan_id_hex"] = "".join(f"{b:02X}" for b in z["extended_pan_id"])

        if regen.get("network_key"):
            z["network_key"] = generate_network_key()
            regenerated["network_key_hex"] = "".join(f"{b:02X}" for b in z["network_key"])

        with open("./config/config.yaml", "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        logger.warning(f"Credentials regenerated: {list(regenerated.keys())}")
        return {
            "success": True,
            "regenerated": regenerated,
            "message": "Credentials saved. Restart service to apply."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================================================
# ROUTES - CONFIGURATION MANAGEMENT
# ============================================================================

@app.get("/api/config")
async def get_config_file():
    """Get the raw config.yaml content."""
    try:
        if os.path.exists("./config/config.yaml"):
            with open("./config/config.yaml", 'r') as f:
                content = f.read()
            return {"success": True, "content": content}
        return {"success": False, "error": "config.yaml not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/config")
async def update_config_file(request: ConfigUpdateRequest):
    """Update config.yaml."""
    try:
        # Validate YAML format before saving
        try:
            yaml.safe_load(request.content)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"Invalid YAML: {e}"}

        # Write to file
        if os.path.exists("./config/config.yaml"):
            with open("./config/config.yaml", 'w') as f:
                f.write(request.content)

            logger.info("Configuration file updated via API")
            return {"success": True}
    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/system/restart")
async def restart_system():
    """Restart the application in an OS-agnostic way."""
    logger.warning("System restart requested via API")

    # Function to perform the actual restart
    async def perform_restart():
        logger.info("Restarting process...")
        await asyncio.sleep(1)  # Give time for the response to be sent

        # Use os.execv to replace the current process with a new one
        # This works on Linux, macOS, and Windows
        python = sys.executable
        os.execl(python, python, *sys.argv)

    asyncio.create_task(perform_restart())
    return {"success": True, "message": "Restarting application..."}


# ============================================================================
# ROUTES - DEVICE MANAGEMENT
# ============================================================================

@app.get("/api/devices")
async def get_devices():
    """Get list of all devices with their current state."""
    return zigbee_service.get_device_list()


@app.post("/api/permit_join")
async def permit_join(request: Optional[PermitJoinRequest] = None):
    """Enable or disable pairing mode."""
    duration = 240
    target = None

    if request:
        duration = request.duration
        target = request.target_ieee

    result = await zigbee_service.permit_join(duration, target)
    return {"status": "success", **result}


# Status endpoint
@app.get("/api/permit_join")
async def get_permit_join_status():
    """Get current pairing status."""
    return zigbee_service.get_pairing_status()


@app.post("/api/touchlink/scan")
async def touchlink_scan(request: Optional[TouchlinkRequest] = None):
    """Scan for Touchlink devices."""
    channel = request.channel if request else None
    return await zigbee_service.touchlink_scan(channel)


@app.post("/api/touchlink/identify")
async def touchlink_identify(request: Optional[TouchlinkRequest] = None):
    """Identify Touchlink device(s) - make them blink."""
    channel = request.channel if request else None
    ieee = request.ieee if request else None
    return await zigbee_service.touchlink_identify(channel=channel, target_ieee=ieee)


@app.post("/api/touchlink/reset")
async def touchlink_reset(request: Optional[TouchlinkRequest] = None):
    """Factory reset Touchlink device(s)."""
    channel = request.channel if request else None
    ieee = request.ieee if request else None
    return await zigbee_service.touchlink_factory_reset(channel=channel, target_ieee=ieee)


@app.post("/api/device/remove")
async def remove_device(request: DeviceRequest):
    """Remove a device from the network, optionally banning it."""

    if request.ban:
        zigbee_service.ban_device(request.ieee, reason="Banned on removal")

    result = await zigbee_service.remove_device(request.ieee, force=request.force)

    if request.ban:
        result["banned"] = True

    return result

@app.post("/api/device/reconfigure")
async def reconfigure_device_endpoint(request: DeviceRequest):
    """Reconfigure device with optional aggressive LQI reporting."""
    logger.info(f"[{request.ieee}] Starting reconfiguration...")

    try:
        if request.ieee not in zigbee_service.devices:
            return {"success": False, "error": "Device not found"}

        device = zigbee_service.devices[request.ieee]
        role = device.get_role()

        # 1. Always run standard config
        await zigbee_service.configure_device(request.ieee)

        # 2. Handle aggressive mode if specified
        if request.aggressive is True:
            if role not in ("Router", "Coordinator"):
                return {"success": False, "error": f"Device is {role}, not a Router. Only routers support aggressive reporting."}
            logger.info(f"[{request.ieee}] Applying aggressive zone reporting...")
            result = await configure_zone_device_reporting(zigbee_service, [request.ieee])
            return {"success": True, "mode": "aggressive", **result}

        elif request.aggressive is False:
            if role not in ("Router", "Coordinator"):
                return {"success": False, "error": f"Device is {role}, not a Router."}
            logger.info(f"[{request.ieee}] Restoring baseline reporting...")
            result = await remove_aggressive_reporting(zigbee_service, [request.ieee])
            return {"success": True, "mode": "baseline", **result}

        # aggressive=None: standard config only
        return {"success": True, "mode": "standard"}

    except Exception as e:
        logger.error(f"[{request.ieee}] Reconfiguration failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/device/rename")
async def rename_device(request: RenameRequest):
    """Rename a device."""
    return await zigbee_service.rename_device(request.ieee, request.name)


@app.post("/api/device/configure")
async def configure_device(request: ConfigureRequest):
    """Configure device bindings, reporting, and polling."""
    # Handle polling interval separately (uses its own storage)
    if request.polling_interval is not None:
        await zigbee_service.set_polling_interval(request.ieee, request.polling_interval)
        logger.info(f"[{request.ieee}] Polling interval set to {request.polling_interval}s")

    # Convert Pydantic model to dict safely
    config_dict = request.model_dump() if hasattr(request, 'model_dump') else request.dict()

    # Remove polling_interval from config dict (handled above)
    config_dict.pop('polling_interval', None)

    return await zigbee_service.configure_device(request.ieee, config=config_dict)


@app.post("/api/device/interview")
async def interview_device(request: DeviceRequest):
    """Re-interview a device (refresh descriptors)."""
    return await zigbee_service.interview_device(request.ieee)


@app.post("/api/device/poll")
async def poll_device(request: DeviceRequest):
    """Poll device for current attribute values."""
    return await zigbee_service.poll_device(request.ieee)


@app.post("/api/device/command")
async def send_command(request: CommandRequest):
    """Send a command to a device (on / off / brightness / etc)."""
    return await zigbee_service.send_command(
        request.ieee,
        request.command,
        request.value,
        endpoint_id=request.endpoint  # Pass it through
    )


@app.post("/api/device/read_attribute")
async def read_attribute(request: AttributeReadRequest):
    """Read a specific attribute from a device."""
    return await zigbee_service.read_attribute(
        request.ieee,
        request.endpoint_id,
        request.cluster_id,
        request.attribute
    )

@app.post("/api/device/bind")
async def bind_devices(request: BindRequest):
    """Bind two devices."""
    return await zigbee_service.bind_devices(request.source_ieee, request.target_ieee, request.cluster_id)



@app.post("/api/ban")
async def ban_device(request: BanRequest):
    """Ban a device by IEEE address."""
    return zigbee_service.ban_device(request.ieee, request.reason)


@app.post("/api/unban")
async def unban_device(request: UnbanRequest):
    """Remove a device from the ban list."""
    return zigbee_service.unban_device(request.ieee)


@app.get("/api/banned")
async def get_banned_devices():
    """Get list of all banned IEEE addresses."""
    return {
        "banned": zigbee_service.get_banned_devices(),
        "count": len(zigbee_service.get_banned_devices())
    }


@app.get("/api/banned/{ieee}")
async def check_banned(ieee: str):
    """Check if a specific device is banned."""
    return {
        "ieee": ieee,
        "banned": zigbee_service.is_device_banned(ieee)
    }

@app.get("/api/tabs")
async def get_tabs():
    return zigbee_service.get_device_tabs()

@app.post("/api/tabs")
async def create_tab(data: dict):
    return zigbee_service.create_device_tab(data['name'])

@app.delete("/api/tabs/{tab_name}")
async def delete_tab(tab_name: str):
    return zigbee_service.delete_device_tab(tab_name)

@app.post("/api/tabs/{tab_name}/devices")
async def add_device_to_tab(tab_name: str, data: dict):
    return zigbee_service.add_device_to_tab(tab_name, data['ieee'])

@app.delete("/api/tabs/{tab_name}/devices/{ieee}")
async def remove_device_from_tab(tab_name: str, ieee: str):
    return zigbee_service.remove_device_from_tab(tab_name, ieee)

@app.get("/api/devices/orphaned")
async def get_orphaned_devices():
    """Find devices in database but not active in network."""
    return await zigbee_service.find_duplicate_devices()

@app.post("/api/devices/cleanup-orphaned")
async def cleanup_orphaned():
    """Remove all orphaned devices from database."""
    return await zigbee_service.cleanup_orphaned_devices()

# ============================================================================
# ROUTES - DEVICE OVERRIDES
# ============================================================================

@app.get("/api/device_overrides")
async def get_device_overrides():
    """Get all device override definitions."""
    from modules.device_overrides import get_override_manager
    mgr = get_override_manager()
    return {
        "success": True,
        "definitions": mgr.list_definitions(),
        "ieee_overrides": mgr.list_ieee_overrides()
    }


@app.post("/api/device_overrides/definition")
async def add_device_definition(data: dict):
    """Add/update a model-level device definition."""
    from modules.device_overrides import get_override_manager
    model = data.get("model", "")
    manufacturer = data.get("manufacturer", "")
    definition = data.get("definition", {})

    if not model:
        return {"success": False, "error": "model is required"}

    mgr = get_override_manager()
    mgr.add_definition(model, manufacturer, definition)
    return {"success": True}


@app.delete("/api/device_overrides/definition")
async def remove_device_definition(data: dict):
    """Remove a model-level device definition."""
    from modules.device_overrides import get_override_manager
    mgr = get_override_manager()
    result = mgr.remove_definition(data.get("model", ""), data.get("manufacturer", ""))
    return {"success": result}


@app.post("/api/device_overrides/ieee_mapping")
async def set_ieee_mapping(data: dict):
    """Set an attribute mapping for a specific device."""
    from modules.device_overrides import get_override_manager
    mgr = get_override_manager()
    mgr.set_ieee_mapping(
        ieee=data["ieee"],
        raw_key=data["raw_key"],
        friendly_name=data["friendly_name"],
        scale=data.get("scale", 1),
        unit=data.get("unit", ""),
        device_class=data.get("device_class", "")
    )
    return {"success": True}


@app.delete("/api/device_overrides/ieee_mapping")
async def remove_ieee_mapping(data: dict):
    """Remove a per-device attribute mapping."""
    from modules.device_overrides import get_override_manager
    mgr = get_override_manager()
    result = mgr.remove_ieee_mapping(data["ieee"], data["raw_key"])
    return {"success": result}


@app.get("/api/device_overrides/{ieee}")
async def get_device_mappings(ieee: str):
    """Get all override mappings active for a specific device."""
    from modules.device_overrides import get_override_manager
    mgr = get_override_manager()

    # Get device model/manufacturer
    model = ""
    manufacturer = ""
    if ieee in zigbee_service.devices:
        dev = zigbee_service.devices[ieee]
        model = str(getattr(dev.zigpy_dev, 'model', '') or '')
        manufacturer = str(getattr(dev.zigpy_dev, 'manufacturer', '') or '')

    return {
        "success": True,
        "ieee": ieee,
        "model": model,
        "manufacturer": manufacturer,
        "model_definition": mgr.get_definition(model, manufacturer),
        "ieee_mappings": mgr.get_ieee_mappings(ieee),
        # Show raw generic keys currently in state
        "unmapped_keys": [
            k for k in zigbee_service.devices[ieee].state.keys()
            if k.startswith("cluster_")
        ] if ieee in zigbee_service.devices else []
    }


# ============================================================================
# ROUTES - NETWORK INFORMATION
# ============================================================================

@app.get("/api/network/simple-mesh")
async def get_mesh():
    """Get network topology for mesh visualization."""
    return zigbee_service.get_simple_mesh()

@app.post("/api/network/scan")
async def scan_network():
    """Trigger a manual topology scan (LQI)."""
    return await zigbee_service.scan_network_topology()

@app.get("/api/network/scan/status")
async def scan_status():
    return zigbee_service.get_scan_status()

@app.get("/api/join_history")
async def get_join_history():
    """Get device join history."""
    events = zigbee_service.get_join_history()
    return {"success": True, "events": events}


@app.get("/api/join_history/stats")
async def get_join_stats():
    """Get join statistics."""
    import time
    events = zigbee_service.get_join_history()
    now = time.time() * 1000
    day_ago = now - (24 * 60 * 60 * 1000)

    recent_events = [e for e in events if e.get('join_timestamp', 0) > day_ago]

    by_type = {}
    for event in recent_events:
        device_type = event.get('device_type', 'Unknown')
        by_type[device_type] = by_type.get(device_type, 0) + 1

    return {
        "success": True,
        "total_joins_24h": len(recent_events),
        "by_type": by_type
    }


@app.get("/api/network/packet-stats")
async def get_packet_stats():
    """Get per-device packet statistics."""
    from modules.packet_stats import packet_stats
    return {
        "success": True,
        "stats": packet_stats.get_all_stats(),
        "summary": packet_stats.get_summary()
    }


@app.get("/api/network/packet-stats/{ieee}")
async def get_device_packet_stats(ieee: str):
    """Get packet statistics for a specific device."""
    from modules.packet_stats import packet_stats
    stats = packet_stats.get_device_stats(ieee)
    if stats:
        return {"success": True, "stats": stats}
    return {"success": False, "error": "Device not found in statistics"}


@app.post("/api/network/packet-stats/reset")
async def reset_packet_stats():
    """Reset all packet statistics."""
    from modules.packet_stats import packet_stats
    packet_stats.reset()
    return {"success": True, "message": "Statistics reset"}

# ============================================================================
# ROUTES - HOME ASSISTANT STATUS
# ============================================================================

@app.get("/api/ha/status")
async def get_ha_status():
    """
    Get current Home Assistant connection status.
    Returns the status of the MQTT bridge which indicates HA connectivity.
    """
    try:
        if not mqtt_service or not mqtt_service.connected:
            return {"status": "offline", "connected": False}

        # Check if bridge is online by checking MQTT connection
        # The bridge_status_topic should have been published as "online"
        return {
            "status": "online",
            "connected": True,
            "broker": f"{mqtt_service.broker}:{mqtt_service.port}",
            "base_topic": mqtt_service.base_topic,
            "bridge_topic": mqtt_service.bridge_status_topic
        }
    except Exception as e:
        logger.error(f"Failed to get HA status: {e}")
        return {"status": "unknown", "error": str(e)}


# ============================================================================
# ROUTES - DEBUG CONTROL
# ============================================================================

@app.get("/api/debug/status")
async def get_debug_status():
    """Get current debug status."""
    try:
        debugger = get_debugger()
        return debugger.get_stats()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/debug/enable")
async def enable_debug(file_logging: bool = True):
    """Enable debugging with optional file logging."""
    try:
        debugger = get_debugger()
        result = debugger.enable(file_logging=file_logging)
        await manager.broadcast({"type": "debug_status", "payload": result})
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/debug/disable")
async def disable_debug():
    """Disable debugging."""
    try:
        debugger = get_debugger()
        result = debugger.disable()
        await manager.broadcast({"type": "debug_status", "payload": result})
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/debug/clear")
async def clear_debug():
    """Clear all debug data."""
    try:
        debugger = get_debugger()
        result = debugger.clear()
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/packets")
async def get_debug_packets(
        limit: int = 100,
        ieee: str = None,
        cluster: int = None,
        importance: str = None
):
    """Get captured packets with filtering."""
    try:
        debugger = get_debugger()
        return {
            "success": True,
            "packets": debugger.get_packets(
                limit=limit,
                ieee_filter=ieee,
                cluster_filter=cluster,
                importance=importance
            )
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/debug/motion_events")
async def get_motion_events(limit: int = 50):
    """Get recent motion detection events."""
    try:
        debugger = get_debugger()
        return {
            "success": True,
            "events": debugger.get_motion_events(limit=limit)
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/device/{ieee}")
async def get_device_debug(ieee: str):
    """Get debug summary for a specific device."""
    try:
        debugger = get_debugger()
        return debugger.get_device_summary(ieee)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/log_file")
async def get_debug_log_file(lines: int = 500):
    """Get contents of debug log file."""
    try:
        debugger = get_debugger()
        return {
            "success": True,
            "content": debugger.get_log_file_contents(lines=lines)
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# RESILIENCE ENDPOINT
# ============================================================================

@app.get("/api/resilience/stats")
async def get_resilience_stats():
    """Get resilience system statistics."""
    if hasattr(zigbee_service, 'resilience'):
        return zigbee_service.resilience.get_stats()
    return {"error": "Resilience not enabled"}

@app.get("/api/resilience/status")
async def get_resilience_status():
    """Get current resilience status."""
    if hasattr(zigbee_service, 'resilience'):
        return {
            "state": zigbee_service.resilience.get_state(),
            "connected": zigbee_service.resilience.is_connected(),
            "recovery_in_progress": zigbee_service.resilience.recovery_in_progress,
        }
    return {"error": "Resilience not enabled"}

@app.get("/api/error_stats")
async def get_error_stats():
    """Get error handling statistics."""
    from modules.error_handler import get_error_stats
    return get_error_stats()


# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await manager.connect(ws)
    try:
        while True:
            # Keep connection alive, receive any messages from client
            data = await ws.receive_text()
            # Could handle client commands here if needed
            logger.debug(f"WebSocket received: {data}")
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
        manager.disconnect(ws)


# ============================================================================
# MQTT DIAGNOSTIC ENDPOINT
# ============================================================================

@app.get("/api/mqtt/queue_stats")
async def get_mqtt_queue_stats():
    """Get MQTT publish queue statistics."""
    try:
        if mqtt_service and hasattr(mqtt_service, 'get_queue_stats'):
            return mqtt_service.get_queue_stats()
        return {"error": "Queue not available"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/fast_path_stats")
async def get_fast_path_stats():
    """Get fast path processor statistics."""
    try:
        if hasattr(zigbee_service, 'fast_path'):
            return zigbee_service.fast_path.get_stats()
        return {"error": "Fast path not available"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/performance/latency")
async def get_performance_metrics():
    """Get overall performance metrics."""
    try:
        mqtt_stats = mqtt_service.get_queue_stats() if hasattr(mqtt_service, 'get_queue_stats') else {}
        fast_path_stats = zigbee_service.fast_path.get_stats() if hasattr(zigbee_service, 'fast_path') else {}

        return {
            "mqtt_queue": mqtt_stats,
            "fast_path": fast_path_stats,
            "devices_count": len(zigbee_service.devices),
            "timestamp": time.time()
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# MQTT EXPLORER API ENDPOINTS
# ============================================================================

@app.post("/api/mqtt_explorer/start")
async def start_mqtt_explorer():
    """Start MQTT Explorer monitoring."""
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            success = await mqtt_service.mqtt_explorer.start_monitoring()
            return {"success": success, "message": "Monitoring started" if success else "Already monitoring or MQTT not connected"}
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to start MQTT Explorer: {e}")
        return {"error": str(e)}


@app.post("/api/mqtt_explorer/stop")
async def stop_mqtt_explorer():
    """Stop MQTT Explorer monitoring."""
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            await mqtt_service.mqtt_explorer.stop_monitoring()
            return {"success": True, "message": "Monitoring stopped"}
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to stop MQTT Explorer: {e}")
        return {"error": str(e)}


@app.get("/api/mqtt_explorer/messages")
async def get_mqtt_explorer_messages(
        topic: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100
):
    """
    Get MQTT messages with optional filtering.

    Query parameters:
        topic: Filter by topic pattern (supports MQTT wildcards)
        search: Search in topic or payload
        limit: Maximum messages to return (default 100)
    """
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            messages = mqtt_service.mqtt_explorer.get_messages(
                topic_filter=topic,
                search=search,
                limit=limit
            )
            return {"messages": messages}
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to get MQTT Explorer messages: {e}")
        return {"error": str(e)}


@app.get("/api/mqtt_explorer/topics")
async def get_mqtt_explorer_topics():
    """Get all unique topics seen by MQTT Explorer."""
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            topics = mqtt_service.mqtt_explorer.get_topics()
            return {"topics": topics}
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to get MQTT Explorer topics: {e}")
        return {"error": str(e)}


@app.get("/api/mqtt_explorer/stats")
async def get_mqtt_explorer_stats():
    """Get MQTT Explorer statistics."""
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            stats = mqtt_service.mqtt_explorer.get_stats()
            return stats
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to get MQTT Explorer stats: {e}")
        return {"error": str(e)}


@app.post("/api/mqtt_explorer/clear")
async def clear_mqtt_explorer():
    """Clear all MQTT Explorer messages."""
    try:
        if hasattr(mqtt_service, 'mqtt_explorer'):
            mqtt_service.mqtt_explorer.clear_messages()
            return {"success": True, "message": "Messages cleared"}
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to clear MQTT Explorer: {e}")
        return {"error": str(e)}


@app.post("/api/mqtt_explorer/publish")
async def mqtt_explorer_publish(request: dict):
    """
    Publish a test message through MQTT.

    Body:
        topic: MQTT topic
        payload: Message payload
        qos: Quality of Service (0, 1, or 2)
        retain: Whether to retain message (default false)
    """
    try:
        topic = request.get("topic")
        payload = request.get("payload", "")
        qos = request.get("qos", 0)
        retain = request.get("retain", False)

        if not topic:
            return {"error": "Topic required"}

        if hasattr(mqtt_service, 'mqtt_explorer'):
            success = await mqtt_service.mqtt_explorer.publish_test_message(
                topic=topic,
                payload=payload,
                qos=qos,
                retain=retain
            )
            return {
                "success": success,
                "message": "Message published" if success else "Publish failed"
            }
        return {"error": "MQTT Explorer not available"}
    except Exception as e:
        logger.error(f"Failed to publish via MQTT Explorer: {e}")
        return {"error": str(e)}


# ============================================================================
# MQTT EXPLORER WEBSOCKET NOTIFICATIONS
# ============================================================================

async def mqtt_explorer_callback(message_record):
    """Callback for broadcasting MQTT Explorer messages to WebSocket clients."""
    await manager.broadcast({
        "type": "mqtt_message",
        "payload": message_record
    })

# ============================================================================
# GROUPS API ENDPOINTS
# ============================================================================

@app.get("/api/groups")
async def get_groups():
    """Get all Zigbee groups"""
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return []
        return zigbee_service.group_manager.get_all_groups()
    except Exception as e:
        logger.error(f"Failed to get groups: {e}")
        return {"error": str(e)}


@app.post("/api/groups/create")
async def create_group(data: dict):
    """
    Create a new Zigbee group
    Body: {"name": "Living Room Lights", "devices": ["ieee1", "ieee2", ...]}
    """
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return {"error": "Group manager not initialized"}

        name = data.get('name')
        devices = data.get('devices', [])

        if not name:
            return {"error": "Group name required"}

        if len(devices) < 2:
            return {"error": "At least 2 devices required"}

        result = await zigbee_service.group_manager.create_group(name, devices)

        # Broadcast update to all WebSocket clients
        if 'success' in result:
            await manager.broadcast({
                "type": "group_created",
                "group": result['group']
            })

        return result

    except Exception as e:
        logger.error(f"Failed to create group: {e}")
        return {"error": str(e)}


@app.post("/api/groups/{group_id}/add_device")
async def add_device_to_group(group_id: int, data: dict):
    """Add device to existing group"""
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return {"error": "Group manager not initialized"}

        ieee = data.get('ieee')
        if not ieee:
            return {"error": "Device IEEE required"}

        result = await zigbee_service.group_manager.add_device_to_group(group_id, ieee)

        if 'success' in result:
            await manager.broadcast({
                "type": "group_updated",
                "group": result['group']
            })

        return result

    except Exception as e:
        logger.error(f"Failed to add device to group: {e}")
        return {"error": str(e)}


@app.post("/api/groups/{group_id}/remove_device")
async def remove_device_from_group(group_id: int, data: dict):
    """Remove device from group"""
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return {"error": "Group manager not initialized"}

        ieee = data.get('ieee')
        if not ieee:
            return {"error": "Device IEEE required"}

        result = await zigbee_service.group_manager.remove_device_from_group(group_id, ieee)

        if 'success' in result:
            await manager.broadcast({
                "type": "group_updated",
                "group": result.get('group')
            })

        return result

    except Exception as e:
        logger.error(f"Failed to remove device from group: {e}")
        return {"error": str(e)}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    """Delete a group"""
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return {"error": "Group manager not initialized"}

        result = await zigbee_service.group_manager.remove_group(group_id)

        if 'success' in result:
            await manager.broadcast({
                "type": "group_deleted",
                "group_id": group_id
            })

        return result

    except Exception as e:
        logger.error(f"Failed to delete group: {e}")
        return {"error": str(e)}


@app.post("/api/groups/{group_id}/control")
async def control_group(group_id: int, data: dict):
    """
    Control all devices in a group
    Body: {"state": "ON", "brightness": 200, "color_temp": 370}
    """
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return {"error": "Group manager not initialized"}

        result = await zigbee_service.group_manager.control_group(group_id, data)
        return result

    except Exception as e:
        logger.error(f"Failed to control group: {e}")
        return {"error": str(e)}


@app.get("/api/devices/{ieee}/compatible")
async def get_compatible_devices(ieee: str):
    """Get devices compatible with this device for grouping"""
    try:
        if not hasattr(zigbee_service, 'group_manager'):
            return []

        compatible = zigbee_service.group_manager.get_compatible_devices_for(ieee)
        return compatible

    except Exception as e:
        logger.error(f"Failed to get compatible devices: {e}")
        return {"error": str(e)}

### ssl

@app.post("/api/ssl/toggle")
async def toggle_ssl(data: dict):
    """Enable or disable HTTPS. Generates cert if needed."""
    enable = data.get('enabled', False)
    try:
        with open('./config/config.yaml', 'r') as f:
            cfg = yaml.safe_load(f)

        cfg.setdefault('web', {}).setdefault('ssl', {})
        cfg['web']['ssl']['enabled'] = enable

        cert = cfg['web']['ssl'].get('cert_file', 'certs/cert.pem')
        key  = cfg['web']['ssl'].get('key_file',  'certs/key.pem')

        if enable:
            os.makedirs('certs', exist_ok=True)
            if not (os.path.exists(cert) and os.path.exists(key)):
                result = subprocess.run([
                    'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                    '-keyout', key, '-out', cert,
                    '-days', '3650', '-nodes',
                    '-subj', '/CN=zigbee-manager'
                ], capture_output=True, text=True)
                if result.returncode != 0:
                    return {"success": False, "error": result.stderr}
                logger.info("Self-signed certificate generated")

        with open('./config/config.yaml', 'w') as f:
            yaml.dump(cfg, f, default_flow_style=False)

        return {"success": True, "enabled": enable}
    except Exception as e:
        logger.error(f"SSL toggle failed: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/ssl/status")
async def ssl_status():
    """Return current SSL enabled state."""
    ssl_cfg = CONFIG.get('web', {}).get('ssl', {})
    return {"enabled": ssl_cfg.get('enabled', False)}

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    ssl_cfg = CONFIG.get('web', {}).get('ssl', {})
    ssl_enabled = ssl_cfg.get('enabled', False)

    kwargs = dict(
        host=get_conf('web', 'host', '0.0.0.0'),
        port=get_conf('web', 'port', 8000),
        log_level="info"
    )

    if ssl_enabled:
        kwargs['ssl_certfile'] = ssl_cfg.get('cert_file', 'certs/cert.pem')
        kwargs['ssl_keyfile'] = ssl_cfg.get('key_file', 'certs/key.pem')
        logger.info("HTTPS enabled")

    uvicorn.run(app, **kwargs)