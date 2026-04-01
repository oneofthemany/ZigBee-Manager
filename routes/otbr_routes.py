"""
OTBR / Thread routes.
Extracted from modules/otbr_api.py to follow routes/ pattern.
"""
import asyncio
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger("routes.otbr")


class FormNetworkRequest(BaseModel):
    channel: Optional[int] = Field(None, ge=11, le=26)


async def _ot_ctl(*args: str, timeout: float = 10.0) -> dict:
    """Run an ot-ctl command and return parsed output."""
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

        if proc.returncode == 0:
            lines = output.split("\n")
            clean = [l for l in lines if l.strip() != "Done"]
            return {"success": True, "output": "\n".join(clean).strip(), "error": None}
        else:
            return {"success": False, "output": output, "error": err or output}

    except asyncio.TimeoutError:
        return {"success": False, "output": "", "error": "ot-ctl timed out"}
    except FileNotFoundError:
        return {"success": False, "output": "", "error": "ot-ctl not found"}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


def register_otbr_routes(app: FastAPI, get_zigbee_service):
    """Register OTBR/Thread routes."""

    def _get_multipan():
        svc = get_zigbee_service()
        if svc and hasattr(svc, 'multipan') and svc.multipan:
            return svc.multipan
        return None

    @app.get("/api/otbr/status")
    async def otbr_status():
        result = {
            "available": False,
            "daemon_running": False,
            "thread_state": "disabled",
            "network": None,
            "ipaddrs": [],
            "version": None,
        }

        mpan = _get_multipan()
        if mpan:
            daemons = mpan.get_status().get("daemons", {})
            otbr_daemon = daemons.get("otbr-agent", {})
            result["daemon_running"] = otbr_daemon.get("running", False)
            result["available"] = mpan.is_otbr_available()

        if not result["available"]:
            return result

        state = await _ot_ctl("state")
        if state["success"]:
            result["thread_state"] = state["output"].strip().lower()

        ver = await _ot_ctl("version")
        if ver["success"]:
            result["version"] = ver["output"].strip()

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

            addrs = await _ot_ctl("ipaddr")
            if addrs["success"] and addrs["output"]:
                result["ipaddrs"] = [a.strip() for a in addrs["output"].split("\n") if a.strip()]

        return result

    @app.post("/api/otbr/form-network")
    async def form_network(req: FormNetworkRequest = FormNetworkRequest()):
        state = await _ot_ctl("state")
        if state["success"] and state["output"].strip().lower() in ("leader", "router", "child"):
            raise HTTPException(
                status_code=409,
                detail=f"Thread network already active (state: {state['output'].strip()}). "
                       f"Stop it first with /api/otbr/stop."
            )

        steps = []

        result = await _ot_ctl("dataset", "init", "new")
        steps.append({"step": "dataset init new", "success": result["success"], "output": result["output"]})
        if not result["success"]:
            return {"success": False, "steps": steps, "error": "Failed to initialise dataset"}

        if req.channel:
            result = await _ot_ctl("dataset", "channel", str(req.channel))
            steps.append({"step": f"dataset channel {req.channel}", "success": result["success"]})

        result = await _ot_ctl("dataset", "commit", "active")
        steps.append({"step": "dataset commit active", "success": result["success"]})
        if not result["success"]:
            return {"success": False, "steps": steps, "error": "Failed to commit dataset"}

        result = await _ot_ctl("ifconfig", "up")
        steps.append({"step": "ifconfig up", "success": result["success"]})
        if not result["success"]:
            return {"success": False, "steps": steps, "error": "Failed to bring up interface"}

        result = await _ot_ctl("thread", "start")
        steps.append({"step": "thread start", "success": result["success"]})
        if not result["success"]:
            return {"success": False, "steps": steps, "error": "Failed to start Thread"}

        await asyncio.sleep(3)
        state = await _ot_ctl("state")
        final_state = state["output"].strip() if state["success"] else "unknown"
        steps.append({"step": "check state", "success": True, "output": final_state})

        logger.info(f"Thread network formed — state: {final_state}")
        return {"success": True, "state": final_state, "steps": steps}

    @app.post("/api/otbr/start")
    async def start_thread():
        result = await _ot_ctl("ifconfig", "up")
        if not result["success"]:
            raise HTTPException(status_code=500, detail=f"ifconfig up failed: {result['error']}")

        result = await _ot_ctl("thread", "start")
        if not result["success"]:
            raise HTTPException(status_code=500, detail=f"thread start failed: {result['error']}")

        await asyncio.sleep(2)
        state = await _ot_ctl("state")
        return {"success": True, "state": state["output"].strip() if state["success"] else "unknown"}

    @app.post("/api/otbr/stop")
    async def stop_thread():
        await _ot_ctl("thread", "stop")
        await _ot_ctl("ifconfig", "down")
        return {"success": True, "state": "disabled"}

    @app.get("/api/otbr/dataset")
    async def get_dataset():
        result = await _ot_ctl("dataset", "active", "-x")
        if not result["success"]:
            return {"success": False, "error": result["error"], "dataset_hex": None}

        readable = await _ot_ctl("dataset", "active")
        network = {}
        if readable["success"]:
            for line in readable["output"].split("\n"):
                line = line.strip()
                if ": " in line:
                    key, val = line.split(": ", 1)
                    network[key.strip().lower().replace(" ", "_")] = val.strip()

        return {"success": True, "dataset_hex": result["output"].strip(), "network": network}

    logger.info("OTBR/Thread routes registered")