"""
Dongle Jedi API — FastAPI routes for the coordinator setup wizard.
===================================================================
Provides endpoints for the frontend setup wizard to:
  1. Check if setup is needed
  2. List serial ports (fast, no probing)
  3. Run a full dongle scan (streams progress via WebSocket)
  4. Apply detected settings to config.yaml

Endpoints:
  GET  /api/setup/status     — Check if setup wizard should be shown
  GET  /api/setup/ports      — Quick USB port enumeration (no serial I/O)
  POST /api/setup/scan       — Start full dongle scan
  GET  /api/setup/scan/status — Get current scan state / last results
  POST /api/setup/apply      — Write detected config to config.yaml
  POST /api/setup/skip       — Skip setup (user will configure manually)

Registration:
  Called from main.py lifespan:
    from modules.dongle_jedi_api import register_setup_routes, get_setup_status
    register_setup_routes(app, manager)
"""

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules.dongle_jedi import DongleJedi, list_serial_ports, ScanProgress

logger = logging.getLogger("modules.dongle_jedi_api")

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Module-level state
_jedi: Optional[DongleJedi] = None
_ws_manager = None   # WebSocket connection manager (set during registration)
_setup_skipped = False


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ApplyRequest(BaseModel):
    """Request to apply detected adapter config."""
    port: str = Field(..., description="Serial port path")
    adapter_family: str = ""
    baud_rate: int = 0
    flow_control: str = "none"
    firmware_version: str = ""
    stack_version: str = ""
    hardware_id: str = ""
    eui64: str = ""
    board_name: str = ""
    extra: dict = Field(default_factory=dict)


class ScanRequest(BaseModel):
    """Optional: specify a port to scan."""
    port: Optional[str] = None


# ---------------------------------------------------------------------------
# Progress streaming via WebSocket
# ---------------------------------------------------------------------------

async def _broadcast_scan_progress(progress: ScanProgress):
    """Send scan progress to all connected WebSocket clients."""
    if _ws_manager:
        await _ws_manager.broadcast({
            "type": "setup_scan_progress",
            "payload": progress.to_dict(),
        })


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def setup_status():
    """
    Check whether the setup wizard should be shown.

    Returns:
        needs_setup: bool — whether the wizard should intercept
        reason: str — why (no_config, no_port_configured, port_missing, configured)
        current_port: str — currently configured port
        skipped: bool — whether user has skipped setup this session
    """
    status = DongleJedi.needs_setup()
    status["skipped"] = _setup_skipped
    return status


@router.get("/ports")
async def list_ports():
    """
    Quick USB serial port enumeration (no serial I/O).
    Returns immediately — safe to call on page load.
    """
    return {"ports": list_serial_ports()}


@router.post("/scan")
async def start_scan(request: ScanRequest = ScanRequest()):
    """
    Start a full dongle scan.

    Progress is streamed via WebSocket (type: "setup_scan_progress").
    Returns immediately with scan_id. Poll /scan/status or listen to WS.
    """
    global _jedi

    if _jedi and _jedi.is_scanning:
        raise HTTPException(409, "Scan already in progress")

    _jedi = DongleJedi()

    # Fire and forget — progress comes via WebSocket
    asyncio.create_task(
        _jedi.scan_async(
            port=request.port,
            progress_cb=_broadcast_scan_progress,
        )
    )

    return {"success": True, "message": "Scan started", "scanning": True}


@router.get("/scan/status")
async def scan_status():
    """Get current scan state and last results."""
    if not _jedi:
        return {"scanning": False, "results": []}

    return {
        "scanning": _jedi.is_scanning,
        "results": _jedi.last_results,
    }


@router.post("/apply")
async def apply_config(request: ApplyRequest):
    """
    Apply detected adapter settings to config.yaml.

    This writes the port, radio_type, baud_rate, and flow_control
    to the zigbee section of config.yaml. The app should be restarted
    after this to pick up the new config.
    """
    global _setup_skipped

    try:
        result_dict = request.model_dump()
        updated = DongleJedi.apply_config(result_dict)
        _setup_skipped = False  # Setup is done, clear skip flag

        return {
            "success": True,
            "message": "Configuration saved",
            "config": updated,
            "restart_required": True,
        }
        logger.info(f"{message}: {updated}")
    except Exception as e:
        logger.error(f"Failed to apply config: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to save configuration: {e}")


@router.post("/skip")
async def skip_setup():
    """
    User wants to skip the setup wizard and configure manually.
    Sets a session flag so the wizard doesn't re-appear until restart.
    """
    global _setup_skipped
    _setup_skipped = True
    return {"success": True, "message": "Setup skipped"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class IntegrationRequest(BaseModel):
    """Request to set integration mode and MQTT config."""
    mode: str = Field(..., description="'standalone' or 'homeassistant'")
    mqtt: Optional[dict] = None


@router.post("/apply-integration")
async def apply_integration(request: IntegrationRequest):
    """
    Apply integration mode (standalone/HA) and MQTT settings.
    Called as step 2 of the setup wizard after coordinator config.
    """
    try:
        updated = DongleJedi.apply_integration_config(
            mode=request.mode,
            mqtt_settings=request.mqtt,
        )
        return {
            "success": True,
            "message": f"Integration mode set to {request.mode}",
            "config": updated,
        }
    except Exception as e:
        logger.error(f"Failed to apply integration config: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to save integration config: {e}")


class MqttTestRequest(BaseModel):
    """MQTT connection test parameters."""
    broker_host: str = ""
    broker_port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = ""
    discovery_prefix: str = ""


@router.post("/test-mqtt")
async def test_mqtt_connection(request: MqttTestRequest):
    """
    Test MQTT broker connectivity.
    Attempts a quick connect/disconnect to validate credentials.
    """
    if not request.broker_host:
        return {"success": False, "error": "No broker host specified"}

    try:
        from aiomqtt import Client

        client = Client(
            hostname=request.broker_host,
            port=request.broker_port,
            username=request.username or None,
            password=request.password or None,
            clean_session=True,
            keepalive=10,
        )

        # Try to connect and immediately disconnect
        await client.__aenter__()
        await client.__aexit__(None, None, None)

        return {"success": True, "message": "Connected successfully"}

    except ImportError:
        return {"success": False, "error": "aiomqtt not installed"}
    except Exception as e:
        error_msg = str(e)
        # Clean up common error messages
        if "Not authorized" in error_msg or "135" in error_msg:
            error_msg = "Authentication failed — check username and password"
        elif "Connection refused" in error_msg:
            error_msg = f"Connection refused at {request.broker_host}:{request.broker_port}"
        elif "Name or service not known" in error_msg or "getaddrinfo" in error_msg:
            error_msg = f"Cannot resolve hostname: {request.broker_host}"

        return {"success": False, "error": error_msg}

def register_setup_routes(app: FastAPI, ws_manager=None):
    """
    Register setup wizard API routes.

    Args:
        app: FastAPI instance
        ws_manager: WebSocket ConnectionManager for progress streaming
    """
    global _ws_manager
    _ws_manager = ws_manager
    app.include_router(router)
    logger.info("Setup wizard routes registered")


def get_setup_status() -> dict:
    """Convenience for checking setup status from main.py."""
    return DongleJedi.needs_setup()