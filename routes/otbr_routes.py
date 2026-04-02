"""
OTBR / Thread routes — status, network formation, topology, diagnostics.
"""
import asyncio
import json
import logging
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger("routes.otbr")

# Persistent storage for Thread dataset — survives container restarts
# via the /app/data volume mount
APP_DIR = os.path.dirname(os.path.abspath(__file__))
THREAD_DATASET_FILE = os.path.join(APP_DIR, "data", "thread_dataset.json")


class FormNetworkRequest(BaseModel):
    channel: Optional[int] = Field(None, ge=11, le=26)
    network_name: Optional[str] = Field(None, max_length=16)


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


async def _parse_dataset() -> dict:
    """Parse ot-ctl dataset active into a normalised dict."""
    result = await _ot_ctl("dataset", "active")
    if not result["success"]:
        return {}

    # Map ot-ctl key names to normalised frontend keys
    key_map = {
        "network name": "network_name",
        "channel": "channel",
        "pan id": "pan_id",
        "ext pan id": "ext_pan_id",
        "mesh local prefix": "mesh_local_prefix",
        "network key": "network_key",
        "active timestamp": "active_timestamp",
        "pskc": "pskc",
        "security policy": "security_policy",
        "channel mask": "channel_mask",
    }

    network = {}
    for line in result["output"].split("\n"):
        line = line.strip()
        if ": " in line:
            raw_key, val = line.split(": ", 1)
            normalised = key_map.get(raw_key.strip().lower(), raw_key.strip().lower().replace(" ", "_"))
            network[normalised] = val.strip()
    return network


# =========================================================================
# THREAD DATASET PERSISTENCE
# =========================================================================

async def save_thread_dataset() -> bool:
    """
    Save the active Thread dataset hex to disk for restore on next startup.
    Called after successful network formation.
    """
    try:
        hex_result = await _ot_ctl("dataset", "active", "-x")
        if not hex_result["success"]:
            logger.warning(f"Cannot save Thread dataset: {hex_result['error']}")
            return False

        dataset_hex = hex_result["output"].strip()
        if not dataset_hex:
            logger.warning("Cannot save Thread dataset: empty hex")
            return False

        network = await _parse_dataset()

        os.makedirs(os.path.dirname(THREAD_DATASET_FILE), exist_ok=True)
        payload = {
            "dataset_hex": dataset_hex,
            "network": network,
            "saved_at": time.time(),
        }
        with open(THREAD_DATASET_FILE, "w") as f:
            json.dump(payload, f, indent=2)

        logger.info(
            f"Thread dataset saved — network: {network.get('network_name', '?')}, "
            f"channel: {network.get('channel', '?')}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to save Thread dataset: {e}")
        return False


def load_thread_dataset() -> Optional[dict]:
    """Load previously saved Thread dataset from disk. Returns None if not found."""
    if not os.path.isfile(THREAD_DATASET_FILE):
        return None
    try:
        with open(THREAD_DATASET_FILE) as f:
            data = json.load(f)
        if data.get("dataset_hex"):
            return data
        return None
    except Exception as e:
        logger.warning(f"Failed to read Thread dataset file: {e}")
        return None


async def restore_thread_dataset() -> bool:
    """
    Restore a previously saved Thread dataset and start the Thread network.

    Called from multipan.py after otbr-agent is ready. The sequence is:
      1. Check for stored dataset
      2. Verify Thread isn't already running (e.g. otbr-agent auto-restored)
      3. Set the active dataset from stored hex
      4. Bring up interface + start Thread
      5. Wait for leader/router/child state
    """
    stored = load_thread_dataset()
    if not stored:
        logger.debug("No stored Thread dataset — skipping restore")
        return False

    dataset_hex = stored["dataset_hex"]
    network_name = stored.get("network", {}).get("network_name", "?")
    logger.info(f"Found stored Thread dataset: {network_name}")

    # Check if Thread is already running (otbr-agent may have auto-restored)
    state = await _ot_ctl("state")
    if state["success"] and state["output"].strip().lower() in ("leader", "router", "child"):
        logger.info(
            f"Thread already active (state: {state['output'].strip()}) — "
            f"skipping restore"
        )
        return True

    # Restore: set dataset → ifconfig up → thread start
    result = await _ot_ctl("dataset", "set", "active", dataset_hex)
    if not result["success"]:
        logger.error(f"Failed to set Thread dataset: {result['error']}")
        return False

    result = await _ot_ctl("ifconfig", "up")
    if not result["success"]:
        logger.error(f"Thread ifconfig up failed: {result['error']}")
        return False

    result = await _ot_ctl("thread", "start")
    if not result["success"]:
        logger.error(f"Thread start failed: {result['error']}")
        return False

    # Wait for the network to attach (up to 15s)
    for i in range(15):
        await asyncio.sleep(1)
        state = await _ot_ctl("state")
        if state["success"]:
            s = state["output"].strip().lower()
            if s in ("leader", "router", "child"):
                logger.info(f"Thread network restored — state: {s}, network: {network_name}")
                return True

    state = await _ot_ctl("state")
    final = state["output"].strip() if state["success"] else "unknown"
    logger.warning(f"Thread restore: network not fully attached after 15s (state: {final})")
    return True  # dataset is committed, it may just need more time


def register_otbr_routes(app: FastAPI, get_zigbee_service):
    """Register OTBR/Thread routes."""

    def _get_multipan():
        svc = get_zigbee_service()
        if svc and hasattr(svc, 'multipan') and svc.multipan:
            return svc.multipan
        return None

    # ── Status ──────────────────────────────────────────────────────

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
            result["network"] = await _parse_dataset()

            addrs = await _ot_ctl("ipaddr")
            if addrs["success"] and addrs["output"]:
                result["ipaddrs"] = [a.strip() for a in addrs["output"].split("\n") if a.strip()]

        return result

    # ── Network Formation ───────────────────────────────────────────

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
        steps.append({"step": "dataset init new", "success": result["success"]})
        if not result["success"]:
            return {"success": False, "steps": steps, "error": "Failed to initialise dataset"}

        if req.channel:
            result = await _ot_ctl("dataset", "channel", str(req.channel))
            steps.append({"step": f"dataset channel {req.channel}", "success": result["success"]})

        if req.network_name:
            result = await _ot_ctl("dataset", "networkname", req.network_name)
            steps.append({"step": f"dataset networkname {req.network_name}", "success": result["success"]})

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

        logger.info(f"Thread network formed — state: {final_state}")

        # Persist dataset for auto-restore on next startup
        await save_thread_dataset()

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

    # ── Dataset ─────────────────────────────────────────────────────

    @app.get("/api/otbr/dataset")
    async def get_dataset():
        hex_result = await _ot_ctl("dataset", "active", "-x")
        network = await _parse_dataset()

        if not hex_result["success"]:
            return {"success": False, "error": hex_result["error"], "dataset_hex": None}

        return {
            "success": True,
            "dataset_hex": hex_result["output"].strip(),
            "network": network,
        }

    # ── Topology ────────────────────────────────────────────────────

    @app.get("/api/otbr/topology")
    async def get_thread_topology():
        """Get Thread network topology — routers, children, and links."""
        nodes = []
        links = []

        # Get our own info
        state = await _ot_ctl("state")
        my_state = state["output"].strip().lower() if state["success"] else "unknown"

        rloc = await _ot_ctl("rloc16")
        my_rloc = rloc["output"].strip() if rloc["success"] else "0000"

        eui = await _ot_ctl("eui64")
        my_eui = eui["output"].strip() if eui["success"] else ""

        ext_addr = await _ot_ctl("extaddr")
        my_ext = ext_addr["output"].strip() if ext_addr["success"] else ""

        nodes.append({
            "id": my_ext or my_rloc,
            "rloc16": my_rloc,
            "eui64": my_eui,
            "role": my_state,
            "is_self": True,
        })

        # Get router table
        router_table = await _ot_ctl("router", "table")
        if router_table["success"]:
            for line in router_table["output"].split("\n"):
                line = line.strip()
                # Skip header lines
                if not line or line.startswith("|") and "ID" in line or line.startswith("+"):
                    continue
                if "|" in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 5:
                        try:
                            router_id = parts[0]
                            rloc16 = parts[1]
                            next_hop = parts[2]
                            path_cost = parts[3]
                            link_quality = parts[4] if len(parts) > 4 else "0"

                            if rloc16 != my_rloc:
                                nodes.append({
                                    "id": rloc16,
                                    "rloc16": rloc16,
                                    "role": "router",
                                    "router_id": router_id,
                                    "is_self": False,
                                })

                                if int(link_quality) > 0:
                                    links.append({
                                        "source": my_rloc,
                                        "target": rloc16,
                                        "link_quality": int(link_quality),
                                    })
                        except (ValueError, IndexError):
                            continue

        # Get child table
        child_table = await _ot_ctl("child", "table")
        if child_table["success"]:
            for line in child_table["output"].split("\n"):
                line = line.strip()
                if not line or line.startswith("|") and "ID" in line or line.startswith("+"):
                    continue
                if "|" in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 4:
                        try:
                            child_id = parts[0]
                            rloc16 = parts[1]

                            nodes.append({
                                "id": rloc16,
                                "rloc16": rloc16,
                                "role": "child",
                                "child_id": child_id,
                                "is_self": False,
                            })

                            links.append({
                                "source": my_rloc,
                                "target": rloc16,
                                "link_quality": 3,
                            })
                        except (ValueError, IndexError):
                            continue

        # Get neighbor table for link quality details
        neighbor_table = await _ot_ctl("neighbor", "table")
        if neighbor_table["success"]:
            for line in neighbor_table["output"].split("\n"):
                line = line.strip()
                if not line or line.startswith("|") and "Role" in line or line.startswith("+"):
                    continue
                if "|" in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 6:
                        try:
                            role = parts[0]
                            rloc16 = parts[1]
                            age = parts[2]
                            avg_rssi = parts[3]
                            last_rssi = parts[4]

                            # Update existing node with RSSI info
                            for node in nodes:
                                if node["rloc16"] == rloc16:
                                    node["avg_rssi"] = avg_rssi
                                    node["last_rssi"] = last_rssi
                                    break
                        except (ValueError, IndexError):
                            continue

        return {
            "success": True,
            "state": my_state,
            "nodes": nodes,
            "links": links,
        }

    # ── Diagnostics ─────────────────────────────────────────────────

    @app.get("/api/otbr/counters")
    async def get_counters():
        """Get Thread MAC and MLE counters."""
        mac = await _ot_ctl("counters", "mac")
        mle = await _ot_ctl("counters", "mle")
        return {
            "success": True,
            "mac": mac["output"] if mac["success"] else mac["error"],
            "mle": mle["output"] if mle["success"] else mle["error"],
        }

    logger.info("OTBR/Thread routes registered")