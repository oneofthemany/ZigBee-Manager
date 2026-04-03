"""
WebSocket connection manager and endpoint.
Extracted from main.py.
"""
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from modules.json_helpers import prepare_for_json

logger = logging.getLogger("websocket")


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

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

        for ws in disconnected:
            self.disconnect(ws)


# Singleton instance
manager = ConnectionManager()


async def broadcast_event(event_type: str, data: dict):
    """Helper to broadcast events via WebSocket."""
    await manager.broadcast({"type": event_type, "payload": data})


def register_websocket_routes(app: FastAPI):
    """Register WebSocket endpoint."""

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        """WebSocket endpoint for real-time updates."""
        await manager.connect(ws)
        try:
            while True:
                data = await ws.receive_text()
                logger.debug(f"WebSocket received: {data}")
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception as e:
            logger.warning(f"WebSocket error: {e}")
            manager.disconnect(ws)
