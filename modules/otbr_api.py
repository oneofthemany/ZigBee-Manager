"""
OTBR / Thread API — Routes for Thread border router management.
================================================================
Provides endpoints for the frontend to:
  1. Check Thread/OTBR status
  2. Form a new Thread network
  3. Get/set the active Thread dataset
  4. Start/stop the Thread interface

All operations go through ot-ctl CLI since otbr-agent exposes
that as the management interface.

Registration:
  Called from main.py:
    from modules.otbr_api import register_otbr_routes
    register_otbr_routes(app)
"""

import asyncio
import logging
from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger("modules.otbr_api")

router = APIRouter(prefix="/api/otbr", tags=["otbr"])

_zigbee_service = None


class FormNetworkRequest(BaseModel):
    """Optional: provide a specific channel or let Thread auto-select."""
    channel: Optional[int] = Field(None, ge=11, le=26, description="Thread channel (11-26, or null for auto)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ot_ctl(*args: str, timeout: float = 10.0) -> dict:
    """
    Run an ot-ctl command and return parsed output.
    Returns {"success": bool, "output": str, "error": str|None}
    """
    cmd = ["ot-ctl"] + list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        # ot-ctl returns "Done" on success lines
        if proc.returncode == 0:
            # Strip trailing "Done" line
            lines = output.split("\n")
            clean = [l for l in lines if l.strip() != "Done"]
            return {"success": True, "output": "\n".join(clean).strip(), "error": None}
        else:
            return {"success": False, "output": output, "error": err or output}

    except asyncio.TimeoutError:
        return {"success": False, "output": "", "error": "ot-ctl timed out"}
    except FileNotFoundError:
        return {"success": False, "output": "", "error": "ot-ctl not found — otbr-agent may not be installed"}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


def _get_multipan_manager():
    """Get the MultiPanManager from the zigbee service if available."""
    if _zigbee_service and hasattr(_zigbee_service, 'multipan') and _zigbee_service.multipan:
        return _zigbee_service.multipan
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def otbr_status():
    """
    Get comprehensive Thread/OTBR status.
    Returns state, network info, and daemon status.
    """
    result = {
        "available": False,
        "daemon_running": False,
        "thread_state": "disabled",
        "network": None,
        "ipaddrs": [],
        "version": None,
    }

    # Check daemon status from multipan manager
    mpan = _get_multipan_manager()
    if mpan:
        daemons = mpan.get_status().get("daemons", {})
        otbr_daemon = daemons.get("otbr-agent", {})
        result["daemon_running"] = otbr_daemon.get("running", False)
        result["available"] = mpan.is_otbr_available()

    if not result["available"]:
        return result

    # Get Thread state
    state = await _ot_ctl("state")
    if state["success"]:
        result["thread_state"] = state["output"].strip().lower()

    # Get version
    ver = await _ot_ctl("version")
    if ver["success"]:
        result["version"] = ver["output"].strip()

    # Get network info if thread is active
    if result["thread_state"] not in ("disabled", "detached"):
        dataset = await _ot_ctl("dataset", "active")
        if dataset["success"]:
            network = {}
            for line in dataset["output"].split("\n"):
                line = line.strip()
                if ": " in line:
                    key, val = line.split(": ", 1)
                    network[key.strip().lower().replace(" ", "_")] = val.strip()
            result["network"] = network

        # Get IP addresses
        addrs = await _ot_ctl("ipaddr")
        if addrs["success"] and addrs["output"]:
            result["ipaddrs"] = [a.strip() for a in addrs["output"].split("\n") if a.strip()]

    return result


@router.post("/form-network")
async def form_network(req: FormNetworkRequest = FormNetworkRequest()):
    """
    Form a new Thread network.
    Initialises a new dataset, commits it, brings up the interface,
    and starts the Thread protocol.
    """
    # Check current state first
    state = await _ot_ctl("state")
    if state["success"] and state["output"].strip().lower() in ("leader", "router", "child"):
        raise HTTPException(
            status_code=409,
            detail=f"Thread network already active (state: {state['output'].strip()}). "
                   f"Stop it first with /api/otbr/stop."
        )

    steps = []

    # 1. Init new dataset
    result = await _ot_ctl("dataset", "init", "new")
    steps.append({"step": "dataset init new", "success": result["success"], "output": result["output"]})
    if not result["success"]:
        return {"success": False, "steps": steps, "error": "Failed to initialise dataset"}

    # 2. Optionally set channel
    if req.channel:
        result = await _ot_ctl("dataset", "channel", str(req.channel))
        steps.append({"step": f"dataset channel {req.channel}", "success": result["success"]})

    # 3. Commit dataset
    result = await _ot_ctl("dataset", "commit", "active")
    steps.append({"step": "dataset commit active", "success": result["success"]})
    if not result["success"]:
        return {"success": False, "steps": steps, "error": "Failed to commit dataset"}

    # 4. Interface up
    result = await _ot_ctl("ifconfig", "up")
    steps.append({"step": "ifconfig up", "success": result["success"]})
    if not result["success"]:
        return {"success": False, "steps": steps, "error": "Failed to bring up interface"}

    # 5. Start Thread
    result = await _ot_ctl("thread", "start")
    steps.append({"step": "thread start", "success": result["success"]})
    if not result["success"]:
        return {"success": False, "steps": steps, "error": "Failed to start Thread"}

    # 6. Wait briefly and check state
    await asyncio.sleep(3)
    state = await _ot_ctl("state")
    final_state = state["output"].strip() if state["success"] else "unknown"
    steps.append({"step": "check state", "success": True, "output": final_state})

    logger.info(f"Thread network formed — state: {final_state}")

    return {
        "success": True,
        "state": final_state,
        "steps": steps,
    }


@router.post("/start")
async def start_thread():
    """Start the Thread interface and protocol."""
    result = await _ot_ctl("ifconfig", "up")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"ifconfig up failed: {result['error']}")

    result = await _ot_ctl("thread", "start")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"thread start failed: {result['error']}")

    await asyncio.sleep(2)
    state = await _ot_ctl("state")
    return {"success": True, "state": state["output"].strip() if state["success"] else "unknown"}


@router.post("/stop")
async def stop_thread():
    """Stop the Thread protocol and bring down the interface."""
    await _ot_ctl("thread", "stop")
    await _ot_ctl("ifconfig", "down")

    return {"success": True, "state": "disabled"}


@router.get("/dataset")
async def get_dataset():
    """Get the active Thread dataset."""
    result = await _ot_ctl("dataset", "active", "-x")
    if not result["success"]:
        return {"success": False, "error": result["error"], "dataset_hex": None}

    # Also get human-readable version
    readable = await _ot_ctl("dataset", "active")
    network = {}
    if readable["success"]:
        for line in readable["output"].split("\n"):
            line = line.strip()
            if ": " in line:
                key, val = line.split(": ", 1)
                network[key.strip().lower().replace(" ", "_")] = val.strip()

    return {
        "success": True,
        "dataset_hex": result["output"].strip(),
        "network": network,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_otbr_routes(app: FastAPI, zigbee_service=None):
    """Register OTBR routes on the FastAPI app."""
    global _zigbee_service
    _zigbee_service = zigbee_service
    app.include_router(router)
    logger.info("OTBR/Thread API routes registered")