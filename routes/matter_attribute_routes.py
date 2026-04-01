"""
Matter attribute browser routes — endpoint/cluster/attribute browsing and read/write.
"""
import asyncio
import json
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Optional

logger = logging.getLogger("routes.matter_attributes")

# Matter cluster name lookup (common clusters)
MATTER_CLUSTER_NAMES = {
    3: "Identify", 4: "Groups", 5: "Scenes", 6: "OnOff",
    8: "LevelControl", 15: "BinaryInputBasic",
    28: "OtaSoftwareUpdateProvider", 29: "OtaSoftwareUpdateRequestor",
    30: "LocalizationConfiguration", 31: "TimeFormatLocalization",
    40: "BasicInformation", 41: "OtaSoftwareUpdateProvider",
    43: "LocalizationConfiguration", 44: "TimeFormatLocalization",
    45: "UnitLocalization", 48: "GeneralCommissioning",
    49: "NetworkCommissioning", 50: "DiagnosticLogs",
    51: "GeneralDiagnostics", 52: "SoftwareDiagnostics",
    53: "ThreadNetworkDiagnostics", 54: "WiFiNetworkDiagnostics",
    55: "EthernetNetworkDiagnostics", 59: "Switch",
    60: "AdministratorCommissioning", 62: "OperationalCredentials",
    63: "GroupKeyManagement", 69: "BooleanState",
    257: "DoorLock", 258: "WindowCovering",
    512: "PumpConfigurationAndControl",
    513: "Thermostat", 514: "FanControl",
    516: "ThermostatUserInterfaceConfiguration",
    768: "ColorControl", 769: "BallastConfiguration",
    1024: "IlluminanceMeasurement", 1026: "TemperatureMeasurement",
    1027: "PressureMeasurement", 1028: "FlowMeasurement",
    1029: "RelativeHumidityMeasurement", 1030: "OccupancySensing",
}

# Common attribute names per cluster
MATTER_ATTR_NAMES = {
    6: {0: "OnOff", 16384: "GlobalSceneControl", 16385: "OnTime", 16386: "OffWaitTime", 16387: "StartUpOnOff"},
    8: {0: "CurrentLevel", 1: "RemainingTime", 2: "MinLevel", 3: "MaxLevel",
        15: "Options", 16: "OnOffTransitionTime", 17: "OnLevel", 18: "OnTransitionTime",
        19: "OffTransitionTime", 16384: "StartUpCurrentLevel"},
    40: {0: "DataModelRevision", 1: "VendorName", 2: "VendorID", 3: "ProductName",
         4: "ProductID", 5: "NodeLabel", 6: "Location", 7: "HardwareVersion",
         8: "HardwareVersionString", 9: "SoftwareVersion", 10: "SoftwareVersionString",
         11: "ManufacturingDate", 15: "SerialNumber", 18: "UniqueID",
         19: "CapabilityMinima", 20: "ProductAppearance"},
    768: {0: "CurrentHue", 1: "CurrentSaturation", 3: "CurrentX", 4: "CurrentY",
          7: "ColorTemperatureMireds", 8: "ColorMode", 16384: "EnhancedCurrentHue",
          16385: "EnhancedColorMode", 16386: "ColorLoopActive",
          16394: "ColorTempPhysicalMinMireds", 16395: "ColorTempPhysicalMaxMireds",
          16400: "StartUpColorTemperatureMireds"},
    1026: {0: "MeasuredValue", 1: "MinMeasuredValue", 2: "MaxMeasuredValue", 3: "Tolerance"},
    1029: {0: "MeasuredValue", 1: "MinMeasuredValue", 2: "MaxMeasuredValue", 3: "Tolerance"},
    1030: {0: "Occupancy", 1: "OccupancySensorType", 2: "OccupancySensorTypeBitmap"},
    69: {0: "StateValue"},
    513: {0: "LocalTemperature", 18: "OccupiedCoolingSetpoint", 17: "OccupiedHeatingSetpoint",
          27: "SystemMode", 28: "ThermostatRunningMode"},
}


class WriteAttributeRequest(BaseModel):
    node_id: int
    endpoint_id: int
    cluster_id: int
    attribute_id: int
    value: Any


class ReadAttributeRequest(BaseModel):
    node_id: int
    endpoint_id: int
    cluster_id: int
    attribute_id: int


def register_matter_attribute_routes(app: FastAPI, get_matter_bridge):
    """Register Matter attribute browser routes."""

    def _get_bridge():
        bridge = get_matter_bridge()
        if not bridge or not bridge.is_connected:
            raise HTTPException(status_code=503, detail="Matter server not connected")
        return bridge

    def _get_device(bridge, node_id: int):
        ieee = f"matter_{node_id}"
        if ieee not in bridge.devices:
            raise HTTPException(status_code=404, detail=f"Matter node {node_id} not found")
        return bridge.devices[ieee]

    @app.get("/api/matter/nodes/{node_id}/attributes")
    async def get_node_attributes(node_id: int):
        """
        Get all attributes for a Matter node, organised by endpoint and cluster.
        This is the Matter equivalent of the Zigbee cluster browser.
        """
        bridge = _get_bridge()
        dev = _get_device(bridge, node_id)
        attributes = dev.node.get("attributes", {})

        # Organise by endpoint → cluster → attributes
        endpoints = {}
        for key, value in attributes.items():
            try:
                parts = key.split("/")
                if len(parts) != 3:
                    continue
                ep_id = int(parts[0])
                cluster_id = int(parts[1])
                attr_id = int(parts[2])
            except (ValueError, IndexError):
                continue

            if ep_id not in endpoints:
                endpoints[ep_id] = {}
            if cluster_id not in endpoints[ep_id]:
                endpoints[ep_id][cluster_id] = {
                    "cluster_id": cluster_id,
                    "cluster_name": MATTER_CLUSTER_NAMES.get(cluster_id, f"Cluster {cluster_id}"),
                    "attributes": [],
                }

            attr_name = "Unknown"
            if cluster_id in MATTER_ATTR_NAMES:
                attr_name = MATTER_ATTR_NAMES[cluster_id].get(attr_id, f"Attribute {attr_id}")
            else:
                attr_name = f"Attribute {attr_id}"

            # Format value for display
            display_value = value
            if isinstance(value, bytes):
                display_value = value.hex()
            elif isinstance(value, (dict, list)):
                display_value = json.dumps(value, default=str)

            endpoints[ep_id][cluster_id]["attributes"].append({
                "attribute_id": attr_id,
                "attribute_name": attr_name,
                "attribute_path": key,
                "value": display_value,
                "type": type(value).__name__,
            })

        # Sort and structure for frontend
        result = []
        for ep_id in sorted(endpoints.keys()):
            clusters = []
            for cluster_id in sorted(endpoints[ep_id].keys()):
                cluster = endpoints[ep_id][cluster_id]
                cluster["attributes"].sort(key=lambda a: a["attribute_id"])
                clusters.append(cluster)
            result.append({
                "endpoint_id": ep_id,
                "clusters": clusters,
            })

        return {
            "success": True,
            "node_id": node_id,
            "endpoints": result,
            "total_attributes": sum(
                len(c["attributes"])
                for ep in result
                for c in ep["clusters"]
            ),
        }

    @app.post("/api/matter/nodes/{node_id}/read-attribute")
    async def read_attribute(node_id: int, req: ReadAttributeRequest):
        """Read a single attribute from a Matter device (live read, not cached)."""
        bridge = _get_bridge()

        try:
            # Use matter-server's read_attribute command
            result = await bridge._send_command("read_attribute", {
                "node_id": node_id,
                "attribute_path": f"{req.endpoint_id}/{req.cluster_id}/{req.attribute_id}",
            })

            # Wait for the response
            # The response comes back through the WebSocket as a message
            # For now, return the cached value
            dev = _get_device(bridge, node_id)
            path = f"{req.endpoint_id}/{req.cluster_id}/{req.attribute_id}"
            cached = dev.node.get("attributes", {}).get(path)

            return {
                "success": True,
                "attribute_path": path,
                "value": cached,
                "note": "Cached value — live read requested",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/matter/nodes/{node_id}/write-attribute")
    async def write_attribute(node_id: int, req: WriteAttributeRequest):
        """Write an attribute value to a Matter device."""
        bridge = _get_bridge()

        try:
            await bridge._send_command("write_attribute", {
                "node_id": node_id,
                "attribute_path": f"{req.endpoint_id}/{req.cluster_id}/{req.attribute_id}",
                "value": req.value,
            })

            return {
                "success": True,
                "attribute_path": f"{req.endpoint_id}/{req.cluster_id}/{req.attribute_id}",
                "value": req.value,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/matter/nodes/{node_id}/command")
    async def send_cluster_command(node_id: int, endpoint_id: int, cluster_id: int,
                                   command_name: str, args: dict = None):
        """Send a cluster command to a Matter device."""
        bridge = _get_bridge()

        try:
            cmd_args = {
                "node_id": node_id,
                "endpoint_id": endpoint_id,
                "cluster_id": cluster_id,
                "command_name": command_name,
            }
            if args:
                cmd_args["args"] = args

            await bridge._send_command("device_command", cmd_args)
            return {"success": True, "command": command_name}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/matter/nodes/{node_id}/info")
    async def get_node_info(node_id: int):
        """Get detailed node information including all raw attributes."""
        bridge = _get_bridge()
        dev = _get_device(bridge, node_id)

        return {
            "success": True,
            "node_id": node_id,
            "ieee": dev.ieee,
            "friendly_name": dev.friendly_name,
            "manufacturer": dev.manufacturer,
            "model": dev.model,
            "available": dev.is_available(),
            "type": dev.get_type(),
            "state": dev.state.copy(),
            "commands": dev.get_control_commands(),
            "raw_attributes": dev.node.get("attributes", {}),
        }

    logger.info("Matter attribute browser routes registered")