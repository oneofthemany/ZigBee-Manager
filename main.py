"""
ZigBee Matter Manager - Main Application
FastAPI-based web server for ZigBee & Matter device management.

Routes are split into:
  routes/config_routes.py   - Config, spectrum, credentials
  routes/device_routes.py   - Device CRUD, commands, banning, tabs, overrides
  routes/network_routes.py  - Mesh, topology, packet stats, join history
  routes/system_routes.py   - Debug, restart, HA status, resilience, MQTT explorer
  routes/matter_routes.py   - Matter commission/remove/status
  routes/websocket_routes.py - WebSocket connection manager
  routes/ota_routes.py      - OTA firmware (already existed)
  modules/zones_api.py      - Zone CRUD (already existed)
  modules/automation_api.py - Automation CRUD (already existed)
"""
import uvicorn
import json
import yaml
import os
import sys
import logging
import threading
import time
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import queue
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Import services
from core import ZigbeeService
from mqtt import MQTTService
from modules.boot_guard_hooks import clear_boot_failure_counter
from modules.zigbee_debug import get_debugger
from modules.json_helpers import prepare_for_json, safe_json_dumps
from modules.mqtt_explorer import MQTTExplorer
from modules.zones_api import register_zone_routes
from modules.automation_api import register_automation_routes
from modules.network_init import ensure_network_credentials
from modules.spectrum_monitor import SpectrumMonitor
from modules.ai_assistant import AIAssistant
from modules.ai_automations import AIAutomations
from modules.ai_api import register_ai_routes
from modules.safe_deploy import register_deploy_routes, check_deploy_on_startup
from modules.system_monitor import SystemMonitor
from modules.telemetry_collector import TelemetryCollector
from modules.telemetry_api import register_telemetry_routes
from modules.dongle_jedi_api import register_setup_routes

# Import route registrations
from routes import (
    register_config_routes,
    register_device_routes,
    register_network_routes,
    register_system_routes,
    register_matter_routes,
    register_group_routes,
    register_editor_routes,
    register_ota_routes,
    register_test_recovery_routes,
    register_websocket_routes,
    manager, broadcast_event,
)


# ============================================================================
# LOGGING CONFIGURATION (NON-BLOCKING)
# ============================================================================

os.makedirs("logs", exist_ok=True)

log_queue = queue.Queue(-1)

file_handler = RotatingFileHandler('logs/zigbee.log', maxBytes=1024*1024, backupCount=3)
console_handler = logging.StreamHandler()

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

log_listener = QueueListener(log_queue, file_handler, console_handler)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = []

queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

logging.getLogger('handlers').setLevel(logging.INFO)
logging.getLogger('handlers.base').setLevel(logging.INFO)
logging.getLogger('core').setLevel(logging.INFO)
logging.getLogger('device').setLevel(logging.INFO)

logger = logging.getLogger('main')


# ============================================================================
# CONFIGURATION
# ============================================================================

def load_config() -> dict:
    """Load configuration from config.yaml."""
    try:
        if not os.path.exists("./config/config.yaml"):
            return {}
        with open("./config/config.yaml", 'r') as config_file:
            return yaml.safe_load(config_file) or {}
    except FileNotFoundError as e:
        logging.error(f"Config file not found: {e}")
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.error(f"Error parsing YAML: {e}")
        sys.exit(1)


def get_conf(section: str, key: str, default=None):
    """Get a configuration value from the current in-memory config."""
    return CONFIG.get(section, {}).get(key, default)


# ── Config file watcher (debug) ──────────────────────────────────────────────
def _watch_config_file():
    _path = "./config/config.yaml"
    try:
        _last = open(_path).read()
    except Exception:
        return
    while True:
        time.sleep(0.3)
        try:
            _cur = open(_path).read()
            if _cur != _last:
                import traceback
                logger.error(
                    "CONFIG FILE CHANGED:\n%s\nSTACK:\n%s",
                    _cur[:500],
                    "".join(traceback.format_stack()),
                )
                _last = _cur
        except Exception:
            pass

threading.Thread(target=_watch_config_file, daemon=True, name="config-watcher").start()

# Load config once at startup — use CONFIG (uppercase) everywhere so it's
# clear this is the module-level singleton.  Re-read via load_config() when
# needed after wizard writes the file.
CONFIG = load_config()


# ============================================================================
# SERVICES INITIALIZATION
# ============================================================================

mqtt_service = MQTTService(
    broker_host=CONFIG.get("mqtt", {}).get("broker_host"),
    port=CONFIG.get("mqtt", {}).get("broker_port"),
    username=CONFIG.get("mqtt", {}).get("username"),
    password=CONFIG.get("mqtt", {}).get("password"),
    base_topic=CONFIG.get("mqtt", {}).get("base_topic"),
    qos=CONFIG.get("mqtt", {}).get("qos"),
    log_callback=None,
)

zigbee_service = ZigbeeService(
    port=CONFIG.get("zigbee", {}).get("port"),
    mqtt_client=mqtt_service,
    config=CONFIG.get("zigbee", {}),
    event_callback=broadcast_event,
)

# Matter is intentionally NOT initialised
matter_server = None
matter_bridge = None


# ============================================================================
# LAZY GETTERS for route modules
# ============================================================================

def get_zigbee_service():
    return zigbee_service

def get_mqtt_service():
    return mqtt_service

def get_matter_server():
    return matter_server

def get_matter_bridge():
    return matter_bridge

def get_manager():
    return manager


# ============================================================================
# LIFESPAN (startup / shutdown)
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global CONFIG, matter_server, matter_bridge

    clear_boot_failure_counter()

    log_listener.start()
    logger.info("Starting Zigbee Gateway (Threaded Logging Enabled)...")

    # Wire debugger to WebSocket
    async def debug_callback(packet_data):
        await manager.broadcast({"type": "debug_packet", "payload": packet_data})

    debugger = get_debugger()
    if debugger:
        debugger.add_callback(debug_callback)
        logger.info("Registered debug callback for live streaming")

    await manager.broadcast({
        "type": "log",
        "payload": {"level": "INFO", "message": "System Starting...", "timestamp": None}
    })

    # Setup wizard routes must be registered before the setup check
    register_setup_routes(app, ws_manager=manager)

    # ── Check if first-run setup is needed ───────────────────────────────────
    from modules.dongle_jedi import DongleJedi
    setup_status = DongleJedi.needs_setup()

    if setup_status["needs_setup"]:
        logger.warning(f"Setup needed: {setup_status['reason']}")
        logger.info("Web UI is up — setup wizard will guide the user")
        await manager.broadcast({
            "type": "log",
            "payload": {
                "level": "WARN",
                "message": f"Setup needed ({setup_status['reason']}). Open the web UI.",
                "timestamp": None,
            }
        })

    else:
        # ── Normal startup: config is complete ───────────────────────────────
        mqtt_enabled = get_conf("mqtt", "enabled", True)

        if mqtt_enabled:
            try:
                await mqtt_service.start()
                logger.info("MQTT connected")
            except Exception as e:
                logger.warning(f"MQTT connection failed: {e}")

            mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)
            async def mqtt_explorer_callback(message_record):
                await manager.broadcast({"type": "mqtt_message", "payload": message_record})
            mqtt_service.mqtt_explorer.add_callback(mqtt_explorer_callback)
            logger.info("MQTT Explorer initialized")
        else:
            logger.info("MQTT disabled (standalone mode)")

        # Ensure network credentials are generated before starting Zigbee
        ensure_network_credentials("./config/config.yaml")
        CONFIG = load_config()

        network_key = get_conf("zigbee", "network_key", None)
        await zigbee_service.start(network_key=network_key)
        logger.info("Zigbee network started")

        if mqtt_enabled:
            mqtt_service.group_command_callback = zigbee_service.group_manager.handle_mqtt_group_command
            logger.info("Wired GroupManager callback to MQTT Service")

        # ── Matter (only when setup is complete) ─────────────────────────────
        matter_cfg = get_conf("matter", "", {}) or CONFIG.get("matter", {})
        if matter_cfg.get("enabled", False):
            matter_port = matter_cfg.get("port", 5580)      # integer, no trailing comma
            storage_path = matter_cfg.get("storage_path", "./data/matter")

            from modules.matter_server import MatterServerManager
            matter_server = MatterServerManager(
                storage_path=storage_path,
                port=matter_port,
            )

            from modules.matter_bridge import MatterBridge
            matter_bridge = MatterBridge(
                server_url=f"ws://localhost:{matter_port}/ws",
                mqtt_service=mqtt_service,
                event_callback=broadcast_event,
            )
            logger.info("Matter integration enabled (embedded server + bridge)")

        if matter_server:
            try:
                started = await matter_server.start()
                if started:
                    logger.info("Embedded Matter server started")
            except Exception as e:
                logger.error(f"Failed to start Matter server: {e}")

        if matter_bridge:
            try:
                await matter_bridge.start()
                logger.info("Matter bridge started")
            except Exception as e:
                logger.error(f"Failed to start Matter bridge: {e}")

        # ── Spectrum monitor ──────────────────────────────────────────────────
        spectrum_interval = get_conf("zigbee", "spectrum_scan_interval", 3600)
        if spectrum_interval > 0:
            zigbee_service.spectrum_monitor = SpectrumMonitor(
                app_getter=lambda: zigbee_service.app,
                interval=spectrum_interval,
            )

            async def _start_spectrum_monitor(svc):
                is_multipan = getattr(svc, "multipan", None) is not None
                max_wait = 300 if is_multipan else 150
                poll_interval = 5
                if is_multipan:
                    logger.info(f"Spectrum monitor: MultiPAN detected, extending radio wait to {max_wait}s")
                for i in range(max_wait // poll_interval):
                    if svc.app:
                        try:
                            result = await svc.app.energy_scan(channels=range(11, 12), count=1, duration_exp=2)
                            if result:
                                svc.spectrum_monitor.start()
                                logger.info(f"Spectrum monitor started (interval={spectrum_interval}s, waited {i * poll_interval}s)")
                            else:
                                logger.warning("Spectrum monitor: energy_scan returned empty — disabled")
                        except NotImplementedError:
                            logger.warning("Spectrum monitor: energy_scan not supported — disabled")
                        except Exception as e:
                            logger.warning(f"Spectrum monitor: energy_scan probe failed ({e}) — disabled")
                        return
                    await asyncio.sleep(poll_interval)
                logger.warning(f"Spectrum monitor: radio never ready after {max_wait}s — disabled")

            asyncio.create_task(_start_spectrum_monitor(zigbee_service))

    # ── Groups ────────────────────────────────────────────────────────────────
    if hasattr(zigbee_service, "group_manager"):
        logger.info("Group manager initialized")

    # ── System Monitor & Telemetry ────────────────────────────────────────────
    system_monitor = SystemMonitor(interval=30, event_callback=broadcast_event)
    system_monitor.start()
    logger.info("System monitor started")

    telemetry_collector = TelemetryCollector(
        device_registry_getter=lambda: zigbee_service.devices,
        retention_days=7,
    )
    telemetry_collector.start()
    logger.info("Telemetry collector started")

    register_telemetry_routes(app, lambda: system_monitor)
    zigbee_service.telemetry_collector = telemetry_collector

    # ── Recovery ──────────────────────────────────────────────────────────────
    from modules.test_recovery import get_test_recovery_manager
    trm = get_test_recovery_manager(broadcast_event)
    startup_result = trm.check_pending_on_startup()
    if startup_result:
        if startup_result.get("rolled_back"):
            logger.warning(f"Auto-rolled back test deployment: {startup_result.get('path')}")
        elif startup_result.get("pending"):
            logger.info(f"Pending test: {startup_result.get('path')} — {startup_result.get('remaining')}s to confirm")

    # ── AI Assistant ──────────────────────────────────────────────────────────
    ai_config = CONFIG.get("ai", {})
    ai_assistant = AIAssistant(ai_config)
    ai_automations = AIAutomations(ai_assistant, zigbee_service.automation)
    logger.info(
        f"AI Assistant initialised: {ai_assistant.provider}/{ai_assistant.model} "
        f"configured={ai_assistant.is_configured()}"
    )

    def _save_ai_config(ai_cfg):
        try:
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}
            cfg["ai"] = ai_cfg
            with open("./config/config.yaml", "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
            logger.info("AI config saved to config.yaml")
        except Exception as e:
            logger.error(f"Failed to save AI config: {e}")

    register_ai_routes(
        app,
        ai_assistant_getter=lambda: ai_assistant,
        ai_automations_getter=lambda: ai_automations,
        config_saver=_save_ai_config,
    )

    # ── Safe Deploy ───────────────────────────────────────────────────────────
    register_deploy_routes(app, service_name="zigbee_matter_manager")
    logger.info("Safe deploy routes registered")
    asyncio.create_task(check_deploy_on_startup())

    yield  # ── Application runs ──────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down Zigbee Matter Manager...")

    system_monitor.stop()
    telemetry_collector.stop()
    from modules.telemetry_db import close as close_telemetry_db
    close_telemetry_db()

    if zigbee_service.multipan and zigbee_service.multipan.is_running:
        await zigbee_service.multipan.stop()
    await zigbee_service.stop()
    await mqtt_service.stop()
    if hasattr(zigbee_service, "spectrum_monitor"):
        zigbee_service.spectrum_monitor.stop()
    if matter_bridge:
        await matter_bridge.stop()
    if matter_server:
        await matter_server.stop()
    log_listener.stop()


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="Zigbee Gateway",
    description="ZHA-style Zigbee device management",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================================
# STATIC FILE ROUTES
# ============================================================================

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ============================================================================
# REGISTER ALL ROUTE MODULES
# ============================================================================

register_config_routes(app, get_zigbee_service)
register_device_routes(app, get_zigbee_service, get_matter_bridge)
register_network_routes(app, get_zigbee_service)
register_system_routes(app, get_zigbee_service, get_mqtt_service, get_manager)
register_matter_routes(app, get_zigbee_service, get_matter_server, get_matter_bridge)
register_group_routes(app, get_zigbee_service, get_manager)
register_editor_routes(app, get_zigbee_service)
register_test_recovery_routes(app, get_manager)
register_websocket_routes(app)
register_zone_routes(app, lambda: zigbee_service.zone_manager, lambda: zigbee_service.devices)
register_ota_routes(app, lambda: zigbee_service.ota_manager)
register_automation_routes(app, lambda: zigbee_service.automation)


# ============================================================================
# POST-SETUP ZIGBEE HOT-START
# ============================================================================

@app.post("/api/setup/start-services")
async def start_services_after_setup():
    """
    Called by the setup wizard after all config is applied.
    Starts MQTT (if enabled) and Zigbee, streaming probe progress via WS.
    """
    global CONFIG, matter_server, matter_bridge

    try:
        # Re-read config so we pick up everything the wizard just wrote
        CONFIG = load_config()

        mqtt_enabled = CONFIG.get("mqtt", {}).get("enabled", True)

        # ── Step 1: MQTT ─────────────────────────────────────────────────────
        await manager.broadcast({
            "type": "setup_phase",
            "payload": {"phase": "mqtt", "message": "Configuring MQTT..."}
        })

        if mqtt_enabled:
            mqtt_service.broker = CONFIG.get("mqtt", {}).get("broker_host")
            mqtt_service.port = CONFIG.get("mqtt", {}).get("broker_port")
            mqtt_service.username = CONFIG.get("mqtt", {}).get("username")
            mqtt_service.password = CONFIG.get("mqtt", {}).get("password")
            mqtt_service.base_topic = CONFIG.get("mqtt", {}).get("base_topic")

            await mqtt_service.stop()
            await mqtt_service.start()

            mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)
            async def mqtt_explorer_callback(message_record):
                await manager.broadcast({"type": "mqtt_message", "payload": message_record})
            mqtt_service.mqtt_explorer.add_callback(mqtt_explorer_callback)

            await manager.broadcast({
                "type": "setup_phase",
                "payload": {
                    "phase": "mqtt_done",
                    "message": f"MQTT connected to {mqtt_service.broker}",
                    "success": mqtt_service.connected,
                }
            })
        else:
            await manager.broadcast({
                "type": "setup_phase",
                "payload": {"phase": "mqtt_done", "message": "MQTT disabled (standalone)", "success": True}
            })

        # ── Step 2: Zigbee ────────────────────────────────────────────────────
        await manager.broadcast({
            "type": "setup_phase",
            "payload": {"phase": "zigbee_probe", "message": "Detecting Zigbee coordinator..."}
        })

        new_port = CONFIG.get("zigbee", {}).get("port")
        zigbee_service.port = new_port
        zigbee_service._config = CONFIG.get("zigbee", {})

        ensure_network_credentials("./config/config.yaml")
        CONFIG = load_config()
        network_key = CONFIG.get("zigbee", {}).get("network_key", None)  # no trailing comma

        async def probe_progress(progress):
            await manager.broadcast({
                "type": "setup_probe_progress",
                "payload": progress.to_dict(),
            })

        await zigbee_service.start(
            network_key=network_key,
            probe_progress_cb=probe_progress,
        )

        if mqtt_enabled:
            mqtt_service.group_command_callback = zigbee_service.group_manager.handle_mqtt_group_command

        # ── Step 3: Matter (if enabled in config) ─────────────────────────────
        matter_cfg = CONFIG.get("matter", {})
        if matter_cfg.get("enabled", False):
            matter_port = matter_cfg.get("port", 5580)      # integer, no trailing comma
            storage_path = matter_cfg.get("storage_path", "./data/matter")

            from modules.matter_server import MatterServerManager
            matter_server = MatterServerManager(
                storage_path=storage_path,
                port=matter_port,
            )

            from modules.matter_bridge import MatterBridge
            matter_bridge = MatterBridge(
                server_url=f"ws://localhost:{matter_port}/ws",
                mqtt_service=mqtt_service,
                event_callback=broadcast_event,
            )
            logger.info("Matter integration enabled (embedded server + bridge)")

            try:
                started = await matter_server.start()
                if started:
                    logger.info("Embedded Matter server started")
            except Exception as e:
                logger.error(f"Failed to start Matter server: {e}")

            try:
                await matter_bridge.start()
                logger.info("Matter bridge started")
            except Exception as e:
                logger.error(f"Failed to start Matter bridge: {e}")

        await manager.broadcast({
            "type": "setup_complete",
            "payload": {"message": "All services started successfully"}
        })

        return {"success": True, "message": f"Services started on {new_port}"}

    except Exception as e:
        logger.error(f"Failed to start services: {e}", exc_info=True)
        await manager.broadcast({
            "type": "setup_error",
            "payload": {"error": str(e)}
        })
        return {"success": False, "error": str(e)}


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    web_cfg = CONFIG.get("web", {})
    ssl_cfg = web_cfg.get("ssl", {})

    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 8000)               # local variable, not shadowing anything
    ssl_enabled = ssl_cfg.get("enabled", False)

    kwargs = {
        "app": "main:app",
        "host": host,
        "port": port,
        "log_level": CONFIG.get("logging", {}).get("level", "info").lower(),
    }

    if ssl_enabled:
        kwargs["ssl_certfile"] = ssl_cfg.get("cert_file", "certs/cert.pem")
        kwargs["ssl_keyfile"] = ssl_cfg.get("key_file", "certs/key.pem")
        logger.info(f"Starting with SSL on https://{host}:{port}")
    else:
        logger.info(f"Starting on http://{host}:{port}")

    uvicorn.run(**kwargs)