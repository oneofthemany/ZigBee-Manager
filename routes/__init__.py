"""
Routes package - all API endpoints split by domain.
Each module exposes a register_*_routes(app, ...) function.
"""
from routes.config_routes import register_config_routes
from routes.device_routes import register_device_routes
from routes.network_routes import register_network_routes
from routes.system_routes import register_system_routes
from routes.matter_routes import register_matter_routes
from routes.group_routes import register_group_routes
from routes.ota_routes import register_ota_routes
from routes.websocket_routes import register_websocket_routes, manager, broadcast_event

__all__ = [
    'register_config_routes',
    'register_device_routes',
    'register_network_routes',
    'register_system_routes',
    'register_matter_routes',
    'register_group_routes',
    'register_ota_routes',
    'register_websocket_routes',
    'manager',
    'broadcast_event',
]