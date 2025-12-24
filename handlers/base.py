"""
Enhanced Base Cluster Handler with Debug Instrumentation
Compatible with zigpy's listener system, now with comprehensive debugging.
"""
import logging
import asyncio
from typing import Dict, Any, Optional, TYPE_CHECKING, List
import traceback

if TYPE_CHECKING:
    from zigpy.zcl import Cluster

# Import the debugger
try:
    from zigbee_debug import get_debugger, CLUSTER_NAMES
except ImportError:
    # Fallback if debugger not available
    def get_debugger():
        return None
    CLUSTER_NAMES = {}

logger = logging.getLogger("handlers.base")

# Registry to map Cluster IDs to Handler Classes
HANDLER_REGISTRY: Dict[int, type] = {}


def register_handler(cluster_id: int):
    """Decorator to register a cluster handler for a specific cluster ID."""
    def decorator(cls):
        HANDLER_REGISTRY[cluster_id] = cls
        cluster_name = CLUSTER_NAMES.get(cluster_id, f"0x{cluster_id:04X}")
        logger.info(f"📋 Registered handler {cls.__name__} for cluster {cluster_name}")
        return cls
    return decorator


class ClusterHandler:
    """
    Base class for handling a specific Zigbee Cluster.
    Implements zigpy's listener interface with full debugging support.
    """
    CLUSTER_ID: Optional[int] = None
    REPORT_CONFIG: list = []

    def __init__(self, device, cluster: 'Cluster'):
        self.device = device
        self.cluster = cluster
        self.endpoint = cluster.endpoint
        self.cluster_id = cluster.cluster_id
        self._attr_cache: Dict[int, Any] = {}
        self._listener_registered = False

        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")

        # Subscribe to zigpy cluster events
        try:
            self.cluster.add_listener(self)
            self._listener_registered = True
            logger.info(
                f"✅ [{self.device.ieee}] EP{self.endpoint.endpoint_id} - "
                f"Listener registered for {cluster_name} ({self.__class__.__name__})"
            )
        except Exception as e:
            logger.error(
                f"❌ [{self.device.ieee}] EP{self.endpoint.endpoint_id} - "
                f"FAILED to register listener for {cluster_name}: {e}"
            )
            traceback.print_exc()

    # ============================================================
    # UI & CONFIGURATION EXPOSURE
    # ============================================================

    def get_configuration_options(self) -> List[Dict]:
        """
        Return a list of configuration options this handler supports.
        """
        return []

    # ============================================================
    # HOME ASSISTANT DISCOVERY
    # ============================================================
    def get_discovery_configs(self) -> List[Dict]:
        """
        Return list of Home Assistant discovery configurations.
        """
        return []

    # ============================================================
    # ZIGPY LISTENER INTERFACE
    # ============================================================

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """
        Called by zigpy when a cluster attribute is updated.
        """
        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")

        logger.debug( # Changed to DEBUG level to reduce log spam
            f"📡 [{self.device.ieee}] {cluster_name} attribute_updated callback! "
            f"attr=0x{attrid:04X}, value={value}, type={type(value).__name__}"
        )

        # Record in debugger
        debugger = get_debugger()
        if debugger:
            debugger.record_attribute_update(
                ieee=self.device.ieee,
                cluster_id=self.cluster_id,
                endpoint_id=self.endpoint.endpoint_id,
                attr_id=attrid,
                value=value,
                handler_name=self.__class__.__name__
            )

        try:
            # Cache the raw value
            self._attr_cache[attrid] = value

            # Handle wrapped zigpy types
            if hasattr(value, 'value'):
                value = value.value

            # Parse and format the value
            formatted_value = self.parse_value(attrid, value)
            attr_name = self.get_attr_name(attrid)

            logger.debug(
                f"[{self.device.ieee}] Parsed: 0x{attrid:04X} = {value} -> {attr_name}={formatted_value}"
            )

            # Update device state, passing endpoint_id for smart duplicate detection
            self.device.update_state(
                {attr_name: formatted_value},
                endpoint_id=self.endpoint.endpoint_id
            )

        except Exception as e:
            logger.error(
                f"❌ [{self.device.ieee}] Error processing attribute 0x{attrid:04X}: {e}"
            )
            traceback.print_exc()
            if debugger:
                debugger.record_error(
                    self.device.ieee,
                    str(e),
                    f"attribute_updated for 0x{attrid:04X}"
                )

    def cluster_command(self, tsn: int, command_id: int, args):
        """
        Called by zigpy when a cluster command is received.
        Override in subclasses for command handling.
        """
        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")

        # Log that we received the callback
        # CRITICAL: Changed to DEBUG level to prevent INFO log spamming
        logger.debug(
            f"📡 [{self.device.ieee}] {cluster_name} cluster_command callback! "
            f"tsn={tsn}, cmd=0x{command_id:02X}, args={args}"
        )

        # Record in debugger
        debugger = get_debugger()
        if debugger:
            debugger.record_cluster_command(
                ieee=self.device.ieee,
                cluster_id=self.cluster_id,
                endpoint_id=self.endpoint.endpoint_id,
                command_id=command_id,
                args=args,
                handler_name=self.__class__.__name__
            )

    def general_command(self, hdr, args):
        """Called by zigpy for general ZCL commands."""
        logger.debug(
            f"[{self.device.ieee}] general_command: hdr={hdr}, args={args}"
        )

    def zdo_command(self, *args, **kwargs):
        """Called by zigpy for ZDO commands."""
        logger.debug(f"[{self.device.ieee}] zdo_command: args={args}, kwargs={kwargs}")

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """
        Called by zigpy for cluster requests.
        """
        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")

        logger.info(
            f"📡 [{self.device.ieee}] {cluster_name} handle_cluster_request! "
            f"hdr={hdr}, args={args}, dst={dst_addressing}"
        )

        # Try to extract attribute reports from the request
        try:
            if hasattr(hdr, 'command_id'):
                cmd_id = hdr.command_id
                # 0x0A = Report Attributes
                if cmd_id == 0x0A and args:
                    logger.info(f"[{self.device.ieee}] Processing Report Attributes from handle_cluster_request")
                    for attr_rec in args:
                        if hasattr(attr_rec, 'attrid') and hasattr(attr_rec, 'value'):
                            self.attribute_updated(attr_rec.attrid, attr_rec.value.value)
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Error in handle_cluster_request: {e}")
            traceback.print_exc()

    def device_announce(self, *args, **kwargs):
        """Called when device announces itself."""
        logger.debug(f"[{self.device.ieee}] device_announce: args={args}, kwargs={kwargs}")

    # ============================================================
    # CONFIGURATION METHODS
    # ============================================================

    async def configure(self):
        """Bind cluster and configure attribute reporting."""
        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")
        logger.info(f"[{self.device.ieee}] Configuring {cluster_name}...")

        try:
            # Bind the cluster
            # Use timeout to prevent hanging on dead devices
            async with asyncio.timeout(5.0):
                result = await self.cluster.bind()
            logger.info(f"[{self.device.ieee}] ✅ Bound {cluster_name}, result: {result}")

            # Configure reporting if defined
            for config in self.REPORT_CONFIG:
                try:
                    attr_name, min_int, max_int, change = config
                    async with asyncio.timeout(5.0):
                        result = await self.cluster.configure_reporting(
                            attr_name,
                            min_int,
                            max_int,
                            change
                        )
                    logger.info(
                        f"[{self.device.ieee}] ✅ Configured reporting for {attr_name}: "
                        f"min={min_int}s, max={max_int}s, change={change}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{self.device.ieee}] ⚠️ Failed to configure reporting for {attr_name}: {e}"
                    )

            return True
        except asyncio.TimeoutError:
            logger.warning(f"[{self.device.ieee}] ⏳ Configuration timed out for {cluster_name}")
            return False
        except Exception as e:
            logger.warning(
                f"[{self.device.ieee}] ❌ Configuration failed for {cluster_name}: {e}"
            )
            # traceback.print_exc() # Less spam
            return False


    async def poll(self) -> Dict[str, Any]:
        """
        Poll the cluster for current attribute values.

        NOTE: Resilience (timeouts, retries) is now handled by the CommandWrapper
        in ZHADevice.poll, so local try/except blocks are removed.
        """
        cluster_name = CLUSTER_NAMES.get(self.cluster_id, f"0x{self.cluster_id:04X}")
        logger.info(f"[{self.device.ieee}] Polling {cluster_name}...")

        results = {}

        for attr_id, attr_name in self.get_pollable_attributes().items():

            result = await self.cluster.read_attributes([attr_id])

            logger.debug(f"[{self.device.ieee}] Poll result for 0x{attr_id:04X}: {result}")

            if result and attr_id in result[0]:
                value = result[0][attr_id]
                if hasattr(value, 'value'):
                    value = value.value

                # Skip None values - device doesn't support this attribute
                if value is None:  # ← ADD THIS CHECK
                    logger.debug(f"[{self.device.ieee}] Skipping {attr_name} - returned None")
                    continue

                formatted = self.parse_value(attr_id, value)
                results[attr_name] = formatted
                results[attr_name + "_raw"] = value # Store raw for config forms
                logger.info(f"[{self.device.ieee}] Polled {attr_name} = {formatted}")

        return results

    # ============================================================
    # HELPER METHODS
    # ============================================================

    def get_attr_name(self, attrid: int) -> str:
        """Convert attribute ID to human-readable name."""
        return f"attr_{self.cluster_id:04x}_{attrid:04x}"

    def parse_value(self, attr_id: int, value: Any) -> Any:
        """Parse raw attribute value. Override in subclasses."""
        if value is None:
            return None
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        """Return dict of {attr_id: attr_name} for polling."""
        return {}

    def get_debug_info(self) -> Dict:
        """Get debug information about this handler."""
        return {
            "handler_class": self.__class__.__name__,
            "cluster_id": f"0x{self.cluster_id:04X}",
            "cluster_name": CLUSTER_NAMES.get(self.cluster_id, "Unknown"),
            "endpoint_id": self.endpoint.endpoint_id,
            "listener_registered": self._listener_registered,
            "report_config": self.REPORT_CONFIG,
            "pollable_attributes": self.get_pollable_attributes(),
            "config_options": self.get_configuration_options(),
            "cached_attributes": {
                f"0x{k:04X}": str(v) for k, v in self._attr_cache.items()
            },
        }


class LocalDataCluster(ClusterHandler):
    """
    A cluster handler that provides local state without talking to the device.
    """
    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._state: Dict[str, Any] = {}

    def update_state(self, updates: Dict[str, Any]):
        """Update local state and notify device."""
        self._state.update(updates)
        self.device.update_state(updates)


class EventableCluster(ClusterHandler):
    """
    A cluster handler that can emit events to the device.
    """
    def emit_event(self, event_type: str, event_data: Dict[str, Any]):
        """Emit an event through the device service."""
        self.device.emit_event(event_type, event_data)


# Export registry for external access
def get_handler_registry() -> Dict[int, type]:
    """Get the current handler registry."""
    return HANDLER_REGISTRY.copy()


def print_registered_handlers():
    """Print all registered handlers for debugging."""
    print("\n" + "="*60)
    print("REGISTERED CLUSTER HANDLERS")
    print("="*60)
    for cluster_id, handler_cls in sorted(HANDLER_REGISTRY.items()):
        cluster_name = CLUSTER_NAMES.get(cluster_id, "Unknown")
        print(f"  0x{cluster_id:04X} ({cluster_name}): {handler_cls.__name__}")
    print("="*60 + "\n")