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
from routes.editor_routes import register_editor_routes
from routes.ota_routes import register_ota_routes
from routes.test_recovery_routes import register_test_recovery_routes
from routes.websocket_routes import register_websocket_routes, manager, broadcast_event
from routes.otbr_routes import register_otbr_routes
from routes.matter_attribute_routes import register_matter_attribute_routes
from routes.backup_routes import register_backup_routes
from routes.matter_definitions_routes import register_matter_definition_routes
from routes.rotary_bindings_routes import register_rotary_binding_routes
from routes.weather_routes import register_weather_routes
from routes.heating_routes import register_heating_routes
from routes.heating_controller_routes import register_heating_controller_routes
from routes.upgrade_routes import register_upgrade_routes
from routes.api_docs_routes import register_api_docs_routes



__all__ = [
    'register_backup_routes',
    'register_config_routes',
    'register_upgrade_routes',
    'register_device_routes',
    'register_network_routes',
    'register_system_routes',
    'register_matter_routes',
    'register_rotary_binding_routes',
    'register_group_routes',
    'register_editor_routes',
    'register_ota_routes',
    'register_otbr_routes',
    'register_matter_attribute_routes',
    'register_matter_definition_routes',
    'register_test_recovery_routes',
    'register_websocket_routes',
    'register_weather_routes',
    'register_heating_routes',
    'register_heating_controller_routes',
    'register_api_docs_routes',
    'manager',
    'broadcast_event',
]