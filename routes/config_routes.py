"""
Configuration management routes.
Extracted from main.py.
"""
import logging
import os
import yaml
from fastapi import FastAPI
from models import ConfigUpdateRequest
from modules.network_init import (
    generate_pan_id, generate_extended_pan_id,
    generate_network_key, select_best_channel, ZIGBEE_CHANNELS
)
from modules.spectrum_monitor import get_history, get_channel_averages, get_channel_stats, save_scan

logger = logging.getLogger("routes.config")


def register_config_routes(app: FastAPI, get_zigbee_service):
    """Register configuration management routes."""

    @app.get("/api/config/structured")
    async def get_structured_config():
        """Return config as structured JSON for the rich settings UI."""
        try:
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}

            zigbee = cfg.get("zigbee", {})

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
                    "weather": cfg.get("weather", {}),
                }
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/config/structured")
    async def save_structured_config(data: dict):
        """Save structured config back to YAML."""
        try:
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}

            incoming = data.get("config", data)

            if "mqtt" in incoming:
                cfg.setdefault("mqtt", {}).update(incoming["mqtt"])
            if "web" in incoming:
                cfg.setdefault("web", {}).update(incoming["web"])
            if "web_ssl" in incoming:
                cfg.setdefault("web", {}).setdefault("ssl", {}).update(incoming["web_ssl"])
            if "logging" in incoming:
                cfg.setdefault("logging", {}).update(incoming["logging"])

            if "weather" in incoming:
                w = incoming["weather"]
                weather_cfg = cfg.setdefault("weather", {})
                if "enabled" in w:
                    weather_cfg["enabled"] = bool(w["enabled"])
                if w.get("latitude") is not None:
                    weather_cfg["latitude"] = float(w["latitude"])
                if w.get("longitude") is not None:
                    weather_cfg["longitude"] = float(w["longitude"])
                if w.get("poll_interval_minutes"):
                    weather_cfg["poll_interval_minutes"] = int(w["poll_interval_minutes"])
                if "mqtt_publish" in w:
                    weather_cfg["mqtt_publish"] = bool(w["mqtt_publish"])

            if "zigbee" in incoming:
                z = incoming["zigbee"]
                zigbee_cfg = cfg.setdefault("zigbee", {})

                for simple_key in ("port", "radio_type", "channel", "topology_scan_interval", "coordinator_type"):
                    if simple_key in z and z[simple_key] != "" and z[simple_key] is not None:
                        zigbee_cfg[simple_key] = z[simple_key]

                if z.get("pan_id"):
                    zigbee_cfg["pan_id"] = z["pan_id"]

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
            try:
                yaml.safe_load(request.content)
            except yaml.YAMLError as e:
                return {"success": False, "error": f"Invalid YAML: {e}"}

            if os.path.exists("./config/config.yaml"):
                with open("./config/config.yaml", 'w') as f:
                    f.write(request.content)
                logger.info("Configuration file updated via API")
                return {"success": True}
        except Exception as e:
            logger.error(f"Failed to update config: {e}")
            return {"success": False, "error": str(e)}

    # ---- Spectrum & Channel ----

    @app.get("/api/zigbee/spectrum")
    async def get_spectrum():
        """Perform a ZigBee energy scan across all 2.4GHz channels (11-26)."""
        try:
            zigbee_service = get_zigbee_service()
            if not zigbee_service.app:
                return {"success": False, "error": "Zigbee network not started"}

            logger.info("Starting spectrum energy scan...")
            results = await zigbee_service.app.energy_scan(
                channels=range(11, 27), count=3, duration_exp=4
            )
            spectrum = {int(ch): int(energy) for ch, energy in results.items()}
            save_scan(spectrum)
            best = select_best_channel(spectrum)

            current = None
            if zigbee_service.app and hasattr(zigbee_service.app.state, 'network_info'):
                current = getattr(zigbee_service.app.state.network_info, 'channel', None)

            return {
                "success": True, "spectrum": spectrum,
                "best_channel": best, "current_channel": current,
                "channels": list(range(11, 27))
            }
        except NotImplementedError:
            return {"success": False, "error": "Energy scan not supported by this coordinator"}
        except Exception as e:
            logger.error(f"Spectrum scan failed: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/support")
    async def get_spectrum_support():
        """Check if the coordinator hardware supports energy scanning."""
        zigbee_service = get_zigbee_service()
        if not zigbee_service.app:
            return {"supported": False, "reason": "Zigbee network not started"}

        monitor = getattr(zigbee_service, 'spectrum_monitor', None)
        auto_enabled = monitor is not None and monitor._running if monitor else False

        try:
            result = await zigbee_service.app.energy_scan(
                channels=range(11, 12), count=1, duration_exp=2
            )
            return {
                "supported": bool(result),
                "auto_scan_enabled": auto_enabled,
                "auto_scan_interval": monitor.interval if monitor else 0,
                "last_scan_ts": monitor.last_scan_ts if monitor else None
            }
        except NotImplementedError:
            return {"supported": False, "reason": "Coordinator does not support energy_scan"}
        except Exception as e:
            return {"supported": False, "reason": str(e)}

    @app.post("/api/zigbee/channel/auto")
    async def auto_select_channel():
        """Run energy scan, pick the best channel, write to config."""
        try:
            scan_result = await get_spectrum()
            if not scan_result.get("success"):
                return scan_result

            best = scan_result["best_channel"]
            spectrum = scan_result["spectrum"]

            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("zigbee", {})["channel"] = best
            with open("./config/config.yaml", "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

            logger.info(f"Auto channel selection: channel {best} written to config")
            return {
                "success": True, "selected_channel": best,
                "spectrum": spectrum,
                "message": f"Channel {best} selected and saved. Restart service to apply."
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/history")
    async def get_spectrum_history(hours: int = 24):
        """Return raw spectrum scan records for the past N hours."""
        hours = min(hours, 168)
        try:
            records = get_history(hours=hours)
            return {"success": True, "hours": hours, "records": records}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/averages")
    async def get_spectrum_averages(hours: int = 24):
        """Return average energy per channel for the past N hours."""
        hours = min(hours, 168)
        try:
            averages = get_channel_averages(hours=hours)
            return {"success": True, "hours": hours, "averages": averages}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/stats")
    async def get_spectrum_stats(hours: int = 24):
        """Return per-channel statistics (min, max, mean, stddev, percentiles)."""
        hours = min(hours, 168)
        try:
            stats = get_channel_stats(hours=hours)
            return {"success": True, "hours": hours, "stats": stats}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/zigbee/spectrum/scan-now")
    async def trigger_background_scan():
        """Trigger an immediate background scan and store results."""
        try:
            zigbee_service = get_zigbee_service()
            monitor = getattr(zigbee_service, 'spectrum_monitor', None)
            if not monitor:
                return {"success": False, "error": "Spectrum monitor not running"}
            results = await monitor.run_scan_now()
            return {"success": True, "spectrum": results}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---- Credential Regeneration ----

    @app.post("/api/zigbee/credentials/regenerate")
    async def regenerate_credentials(data: dict):
        """Regenerate one or more network credentials and write to config."""
        try:
            regen = data
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
                "success": True, "regenerated": regenerated,
                "message": "Credentials saved. Restart service to apply."
            }
        except Exception as e:
            return {"success": False, "error": str(e)}