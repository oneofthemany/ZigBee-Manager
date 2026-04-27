"""
System management routes - debug, restart, HA status, resilience, MQTT explorer, performance.
Extracted from main.py.
"""
import asyncio
import logging
import os
import sys
import time
import yaml
from fastapi import FastAPI
from modules.zigbee_debug import get_debugger

logger = logging.getLogger("routes.system")

def register_system_routes(app: FastAPI, get_zigbee_service, get_mqtt_service, get_manager):
    """Register system management routes."""

    @app.post("/api/system/restart")
    async def restart_system():
        """Restart the application."""
        logger.warning("System restart requested via API")

        async def perform_restart():
            logger.info("Restarting process...")
            await asyncio.sleep(1)
            python = sys.executable
            os.execl(python, python, *sys.argv)

        asyncio.create_task(perform_restart())
        return {"success": True, "message": "Restarting application..."}

    # ---- HA Status ----

    @app.get("/api/ha/status")
    async def get_ha_status():
        """Get current Home Assistant connection status."""
        try:
            mqtt_service = get_mqtt_service()
            if not mqtt_service or not mqtt_service.connected:
                return {"status": "offline", "connected": False}
            return {
                "status": "online", "connected": True,
                "broker": f"{mqtt_service.broker}:{mqtt_service.port}",
                "base_topic": mqtt_service.base_topic,
                "bridge_topic": mqtt_service.bridge_status_topic
            }
        except Exception as e:
            logger.error(f"Failed to get HA status: {e}")
            return {"status": "unknown", "error": str(e)}

    # ---- Debug ----

    @app.get("/api/debug/status")
    async def get_debug_status():
        """Get current debug status."""
        try:
            return get_debugger().get_stats()
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/debug/enable")
    async def enable_debug(file_logging: bool = True):
        """Enable debugging with optional file logging."""
        try:
            debugger = get_debugger()
            result = debugger.enable(file_logging=file_logging)
            await get_manager().broadcast({"type": "debug_status", "payload": result})
            return result
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/debug/disable")
    async def disable_debug():
        """Disable debugging."""
        try:
            debugger = get_debugger()
            result = debugger.disable()
            await get_manager().broadcast({"type": "debug_status", "payload": result})
            return result
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/debug/packets")
    async def get_debug_packets(limit: int = 100, importance: str = None, ieee: str = None, cluster: int = None):
        """Get captured debug packets."""
        try:
            debugger = get_debugger()
            packets = debugger.get_packets(
                limit=limit,
                ieee_filter=ieee,
                cluster_filter=cluster,
                importance=importance
            )
            return {"success": True, "packets": packets, "count": len(packets)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/debug/log_file")
    async def get_debug_log(lines: int = 1000):
        """Get debug log file contents."""
        try:
            log_path = "logs/zigbee_debug.log"
            if not os.path.exists(log_path):
                return {"success": False, "error": "Debug log file not found"}
            with open(log_path, 'r') as f:
                all_lines = f.readlines()
            content = "".join(all_lines[-lines:])
            return {"success": True, "content": content, "total_lines": len(all_lines)}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/debug/fast_path_stats")
    async def get_fast_path_stats():
        """Get fast path processor statistics."""
        try:
            zigbee_service = get_zigbee_service()
            if hasattr(zigbee_service, 'fast_path'):
                return zigbee_service.fast_path.get_stats()
            return {"error": "Fast path not available"}
        except Exception as e:
            return {"error": str(e)}

    # ---- Resilience ----

    @app.get("/api/resilience/stats")
    async def get_resilience_stats():
        """Get resilience statistics."""
        zigbee_service = get_zigbee_service()
        if hasattr(zigbee_service, 'resilience'):
            return zigbee_service.resilience.get_stats()
        return {"error": "Resilience not enabled"}

    @app.get("/api/resilience/status")
    async def get_resilience_status():
        """Get current resilience status."""
        zigbee_service = get_zigbee_service()
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
        from modules.error_handler import get_error_stats as _get_error_stats
        return _get_error_stats()

    # ---- Performance ----

    @app.get("/api/performance/latency")
    async def get_performance_metrics():
        """Get overall performance metrics."""
        try:
            mqtt_service = get_mqtt_service()
            zigbee_service = get_zigbee_service()
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

    @app.get("/api/mqtt/queue_stats")
    async def get_mqtt_queue_stats():
        """Get MQTT publish queue statistics."""
        try:
            mqtt_service = get_mqtt_service()
            if mqtt_service and hasattr(mqtt_service, 'get_queue_stats'):
                return mqtt_service.get_queue_stats()
            return {"error": "Queue not available"}
        except Exception as e:
            return {"error": str(e)}


    @app.get("/api/system/health")
    async def health_check():
        """
        Lightweight health endpoint. Used by the Containerfile HEALTHCHECK
        and by the in-app upgrade health probe. Must remain cheap and
        synchronous-friendly — no DB queries, no I/O, no waiting on
        subsystems. If it returns 200, the FastAPI process is up and
        serving; that's all callers need.
        """
        return {"status": "ok"}

    # ---- MQTT Explorer ----

    @app.post("/api/mqtt_explorer/start")
    async def start_mqtt_explorer():
        """Start MQTT Explorer monitoring."""
        try:
            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                success = await mqtt_service.mqtt_explorer.start_monitoring()
                return {"success": success, "message": "Monitoring started" if success else "Already monitoring or MQTT not connected"}
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/mqtt_explorer/stop")
    async def stop_mqtt_explorer():
        """Stop MQTT Explorer monitoring."""
        try:
            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                await mqtt_service.mqtt_explorer.stop_monitoring()
                return {"success": True, "message": "Monitoring stopped"}
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/mqtt_explorer/messages")
    async def get_mqtt_messages(limit: int = 100, topic_filter: str = None):
        """Get captured MQTT messages."""
        try:
            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                messages = mqtt_service.mqtt_explorer.get_messages(limit=limit, topic_filter=topic_filter)
                return {"success": True, "messages": messages}
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/mqtt_explorer/stats")
    async def get_mqtt_explorer_stats():
        """Get MQTT Explorer statistics."""
        try:
            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                return mqtt_service.mqtt_explorer.get_stats()
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/mqtt_explorer/clear")
    async def clear_mqtt_explorer():
        """Clear all MQTT Explorer messages."""
        try:
            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                mqtt_service.mqtt_explorer.clear_messages()
                return {"success": True, "message": "Messages cleared"}
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/mqtt_explorer/publish")
    async def mqtt_explorer_publish(request: dict):
        """Publish a test message through MQTT."""
        try:
            topic = request.get("topic")
            payload = request.get("payload", "")
            qos = request.get("qos", 0)
            retain = request.get("retain", False)

            if not topic:
                return {"error": "Topic required"}

            mqtt_service = get_mqtt_service()
            if hasattr(mqtt_service, 'mqtt_explorer'):
                success = await mqtt_service.mqtt_explorer.publish_test_message(
                    topic=topic, payload=payload, qos=qos, retain=retain
                )
                return {"success": success, "message": "Message published" if success else "Publish failed"}
            return {"error": "MQTT Explorer not available"}
        except Exception as e:
            return {"error": str(e)}

    # ---- SSL ----

    @app.post("/api/ssl/toggle")
    async def toggle_ssl(data: dict):
        """
        Enable or disable HTTPS in config.yaml.

        Cert handling:
        - If SSL is being ENABLED and certs already exist → use them as-is.
          NEVER regenerate. Regenerating breaks every browser that already
          trusts the existing cert and is the leading cause of "this site
          is unsafe" warnings after a config tweak.
        - If SSL is being ENABLED and certs DO NOT exist → generate a fresh
          self-signed pair with sensible SAN entries (localhost, 127.0.0.1,
          configured hostname).
        - If SSL is being DISABLED → never touch the cert files. They stay
          on disk for reuse if SSL is re-enabled later.

        Note: changes only take effect after a container/server restart;
        uvicorn does not support hot cert reload.
        """
        import subprocess
        import socket as _socket

        enable = data.get('enabled', False)
        try:
            with open('./config/config.yaml', 'r') as f:
                cfg = yaml.safe_load(f) or {}

            cfg.setdefault('web', {}).setdefault('ssl', {})
            cfg['web']['ssl']['enabled'] = enable

            # Use the SAME defaults main.py uses so paths agree across the
            # codebase. Both files MUST resolve cert_file/key_file to the
            # same location or trust between them breaks.
            cert = cfg['web']['ssl'].get('cert_file', './data/certs/cert.pem')
            key  = cfg['web']['ssl'].get('key_file',  './data/certs/key.pem')

            cert_action = "unchanged"  # for response payload + logs

            if enable:
                # Make sure the directory containing the cert exists.
                # Use os.path.dirname so we honour whatever path the user
                # configured — never hard-code 'certs/'.
                cert_dir = os.path.dirname(cert) or '.'
                os.makedirs(cert_dir, exist_ok=True)

                certs_present = os.path.isfile(cert) and os.path.isfile(key)
                if certs_present:
                    # Existing certs — preserve them. The user may have
                    # already set up browser trust against this cert, or
                    # imported a CA-signed cert manually.
                    logger.info(f"SSL enabled — preserving existing certs at {cert}")
                    cert_action = "preserved"
                else:
                    # No certs found — generate a self-signed pair with
                    # sensible SAN entries so browsers don't immediately
                    # complain about IP/name mismatches.
                    hostname = _socket.gethostname() or 'zigbee-manager'
                    san = f"subjectAltName=DNS:localhost,DNS:{hostname},IP:127.0.0.1"

                    logger.warning(
                        f"SSL enabled but certs missing — generating self-signed "
                        f"cert at {cert} (CN={hostname}, SAN={san})"
                    )
                    result = subprocess.run([
                        'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                        '-keyout', key, '-out', cert,
                        '-days', '3650', '-nodes',
                        '-subj', f'/CN={hostname}',
                        '-addext', san,
                    ], capture_output=True, text=True)
                    if result.returncode != 0:
                        logger.error(f"openssl failed: {result.stderr}")
                        return {"success": False, "error": result.stderr}
                    cert_action = "generated"

                    # Lock down the private key — openssl writes 0644 by default
                    try:
                        os.chmod(key, 0o600)
                    except Exception as e:
                        logger.warning(f"Could not chmod key file: {e}")

            with open('./config/config.yaml', 'w') as f:
                yaml.dump(cfg, f, default_flow_style=False)

            return {
                "success": True,
                "enabled": enable,
                "cert_action": cert_action,
                "cert_path": cert if enable else None,
                "restart_required": True,
                "message": (
                    "SSL config saved. Restart the application for changes to "
                    "take effect."
                ),
            }
        except Exception as e:
            logger.error(f"SSL toggle failed: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/ssl/status")
    async def ssl_status():
        """Return current SSL enabled state."""
        try:
            with open('./config/config.yaml', 'r') as f:
                cfg = yaml.safe_load(f) or {}
            ssl_cfg = cfg.get('web', {}).get('ssl', {})
            return {"enabled": ssl_cfg.get('enabled', False)}
        except Exception:
            return {"enabled": False}