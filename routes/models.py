"""
Pydantic request models for all API endpoints.
Extracted from main.py for maintainability.
"""
from pydantic import BaseModel
from typing import Optional, Any


class StructuredConfigRequest(BaseModel):
    zigbee: dict
    mqtt: dict
    web: dict
    logging: dict


class DeviceRequest(BaseModel):
    ieee: str
    force: Optional[bool] = False
    ban: bool = False
    aggressive: Optional[bool] = None  # Only used by reconfigure


class RenameRequest(BaseModel):
    ieee: str
    name: str


class ConfigureRequest(BaseModel):
    ieee: str
    qos: Optional[int] = None
    polling_interval: Optional[int] = None
    reporting: Optional[dict] = None
    tuya_settings: Optional[dict] = None
    updates: Optional[dict] = None


class CommandRequest(BaseModel):
    ieee: str
    command: str
    value: Optional[Any] = None
    endpoint: Optional[int] = None


class AttributeReadRequest(BaseModel):
    ieee: str
    endpoint_id: int
    cluster_id: int
    attribute: str


class BindRequest(BaseModel):
    source_ieee: str
    target_ieee: str
    cluster_id: int


class ConfigUpdateRequest(BaseModel):
    content: str


class PermitJoinRequest(BaseModel):
    duration: int = 240
    target_ieee: Optional[str] = None


class BanRequest(BaseModel):
    ieee: str
    reason: Optional[str] = None


class UnbanRequest(BaseModel):
    ieee: str


class TouchlinkRequest(BaseModel):
    ieee: Optional[str] = None
    channel: Optional[int] = None


class MatterCommissionRequest(BaseModel):
    code: str  # Setup code from QR or manual pairing code


class MatterRemoveRequest(BaseModel):
    node_id: int
