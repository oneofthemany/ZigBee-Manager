"""
Zigbee Cluster Handlers Package
"""
import logging

logger = logging.getLogger("handlers")

# Import base infrastructure FIRST
from .base import (
    ClusterHandler,
    LocalDataCluster,
    EventableCluster,
    HANDLER_REGISTRY,
    register_handler,
)

# Import all handler modules to trigger registration decorators
# The order matters - security should be first to handle IAS Zone
from .security import *
from .sensors import *
from .basic import *
from .switches import *
from .power import *
from .hvac import *
from .blinds import *
from .tuya import *
from .aqara import *
from .lighting import *
from .lightlink import *
from .general import *
from .sonoff_quirk import *

# Re-export commonly used handlers for convenience
from .security import (
    IASZoneHandler,
)


from .sonoff_quirk import (
    SonoffManufacturerHandler,
)

from .sensors import (
    OccupancySensingHandler,
    DeviceTemperatureHandler,
    TemperatureMeasurementHandler,
    IlluminanceMeasurementHandler,
    RelativeHumidityHandler,
    PressureMeasurementHandler,
    CO2MeasurementHandler,
    PM25MeasurementHandler,
    PowerConfigurationHandler,
)

from .aqara import (
    MultistateInputHandler,
    AqaraAnalogInputHandler,
    AqaraManufacturerCluster,
)

from .basic import (
    BasicHandler,
    IdentifyHandler,
)

from .power import (
    ElectricalMeasurementHandler,
    MeteringHandler,
)

from .general import (
    LevelControlHandler,
    GroupsHandler,
    ScenesHandler,
)

from .hvac import (
    ThermostatHandler,
    FanControlHandler,
)

from .tuya import (
    TuyaClusterHandler,
    AnalogInputHandler,
)

from .blinds import (
    WindowCoveringHandler,
)

from .lighting import (
    BallastClusterHandler,
    ColorClusterHandler,

)

from .lightlink import (
    LightLinkHandler,
)
# Public API
__all__ = [
    # Base
    "ClusterHandler",
    "LocalDataCluster",
    "EventableCluster",
    "HANDLER_REGISTRY",
    "register_handler",

    # Security (IAS Zone - motion, door/window, etc.)
    "IASZoneHandler",

    # Sensors
    "OccupancySensingHandler",
    "DeviceTemperatureHandler",
    "TemperatureMeasurementHandler",
    "IlluminanceMeasurementHandler",
    "RelativeHumidityHandler",
    "PressureMeasurementHandler",
    "CO2MeasurementHandler",
    "PM25MeasurementHandler",
    "PowerConfigurationHandler",

    # Basic
    "BasicHandler",
    "IdentifyHandler",

    # Power
    "ElectricalMeasurementHandler",
    "MeteringHandler",

    # General
    "OnOffHandler",
    "LevelControlHandler",

    "GroupsHandler",
    "ScenesHandler",

    # HVAC
    "ThermostatHandler",

    # Blinds
    "WindowCoveringHandler",
    "FanControlHandler",

    # Tuya
    "TuyaClusterHandler",
    "AnalogInputHandler",

    # Aqara
    "MultistateInputHandler",
    "AqaraAnalogInputHandler",
    "AqaraManufacturerCluster",

    # Lighting
    "BallastClusterHandler",
    "ColorClusterHandler",

    # lightlink
    "LightLinkHandler",

    # sonoff
    "SonoffManufacturerHandler",
]


def get_handler_for_cluster(cluster_id: int):
    """Get the handler class for a given cluster ID."""
    return HANDLER_REGISTRY.get(cluster_id)


def get_supported_clusters():
    """Get list of supported cluster IDs."""
    return list(HANDLER_REGISTRY.keys())


def print_registered_handlers():
    """Print all registered handlers (useful for debugging)."""
    print("\n=== Registered Cluster Handlers ===")
    for cluster_id, handler_cls in sorted(HANDLER_REGISTRY.items()):
        print(f"  0x{cluster_id:04X}: {handler_cls.__name__}")
    print(f"\nTotal: {len(HANDLER_REGISTRY)} handlers registered\n")


# Log registered handlers at import time
logger.info(f"Loaded {len(HANDLER_REGISTRY)} cluster handlers")

# Print handlers in debug mode
if logger.isEnabledFor(logging.DEBUG):
    for cluster_id, handler_cls in sorted(HANDLER_REGISTRY.items()):
        logger.debug(f"  Cluster 0x{cluster_id:04X}: {handler_cls.__name__}")

