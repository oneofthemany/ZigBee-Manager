"""
Network information routes - mesh, topology, packet stats, join history.
Extracted from main.py.
"""
import logging
import time
from fastapi import FastAPI

logger = logging.getLogger("routes.network")


def register_network_routes(app: FastAPI, get_zigbee_service):
    """Register network information routes."""

    @app.get("/api/network/simple-mesh")
    async def get_mesh():
        """Get network topology for mesh visualization."""
        return get_zigbee_service().get_simple_mesh()

    @app.post("/api/network/scan")
    async def scan_network():
        """Trigger a manual topology scan (LQI)."""
        return await get_zigbee_service().scan_network_topology()

    @app.get("/api/network/scan/status")
    async def scan_status():
        return get_zigbee_service().get_scan_status()

    @app.get("/api/join_history")
    async def get_join_history():
        """Get device join history."""
        events = get_zigbee_service().get_join_history()
        return {"success": True, "events": events}

    @app.get("/api/join_history/stats")
    async def get_join_stats():
        """Get join statistics."""
        events = get_zigbee_service().get_join_history()
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