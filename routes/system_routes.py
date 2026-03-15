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
    async def get_debug_packets(limit: int = 100, importance: str = None):
        """Get captured debug packets."""
        try:
            debugger = get_debugger()
            packets = list(debugger.packets)[-limit:]
            return {"success": True, "packets": [p.to_dict() for p in packets]}
        except Exception as e:
            return {"error": str(e)}

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
        """Enable or disable HTTPS. Generates cert if needed."""
        import subprocess
        enable = data.get('enabled', False)
        try:
            with open('./config/config.yaml', 'r') as f:
                cfg = yaml.safe_load(f)

            cfg.setdefault('web', {}).setdefault('ssl', {})
            cfg['web']['ssl']['enabled'] = enable

            cert = cfg['web']['ssl'].get('cert_file', 'certs/cert.pem')
            key = cfg['web']['ssl'].get('key_file', 'certs/key.pem')

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
        try:
            with open('./config/config.yaml', 'r') as f:
                cfg = yaml.safe_load(f) or {}
            ssl_cfg = cfg.get('web', {}).get('ssl', {})
            return {"enabled": ssl_cfg.get('enabled', False)}
        except Exception:
            return {"enabled": False}