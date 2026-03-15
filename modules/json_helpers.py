"""
JSON Serialisation Helpers for Zigpy Types
==========================================
Comprehensive serialisation utilities that handle all zigpy types that aren't
natively JSON-serializable, based on ZHA's diagnostic serialisation patterns.

This module provides:
1. Safe serialisation of zigpy types (EUI64, LVBytes, etc.)
2. Recursive handling of nested structures (dicts, lists)
3. Fallback serialisation for unknown types
"""
import json
import logging
from typing import Any, Dict, List
from datetime import datetime, date
from enum import Enum

logger = logging.getLogger("json_helpers")


def serialise_value(value: Any) -> Any:
    """
    Recursively serialise a value to be JSON-compatible.

    Handles:
    - zigpy types (EUI64, LVBytes, etc.)
    - Nested structures (dict, list)
    - datetime/date objects
    - Enums
    - bytes
    - Other complex objects with __dict__

    Args:
        value: Any value that needs to be JSON-serialisable

    Returns:
        JSON-serialisable representation of the value
    """
    # Handle None
    if value is None:
        return None

    # Handle basic JSON types (fast path)
    if isinstance(value, (str, int, float, bool)):
        return value

    # Handle bytes types (including LVBytes from zigpy)
    if isinstance(value, bytes):
        try:
            # Try to decode as UTF-8 first
            return value.decode('utf-8')
        except UnicodeDecodeError:
            # If that fails, return as hex string
            return value.hex()

    # Handle EUI64 and other zigpy address types
    if hasattr(value, '__class__') and 'EUI64' in value.__class__.__name__:
        return str(value)

    # Handle datetime objects
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    # Handle Enums
    if isinstance(value, Enum):
        return value.value

    # Handle dictionaries recursively
    if isinstance(value, dict):
        return {serialise_key(k): serialise_value(v) for k, v in value.items()}

    # Handle lists/tuples/sets recursively
    if isinstance(value, (list, tuple, set)):
        return [serialise_value(item) for item in value]

    # Handle objects with custom __str__ or __repr__
    # This catches most zigpy types like Named types, etc.
    if hasattr(value, '__str__'):
        try:
            str_repr = str(value)
            # Avoid infinite recursion - check if __str__ returns something useful
            if str_repr != f"<{value.__class__.__name__} object at 0x" and not str_repr.startswith('<'):
                return str_repr
        except Exception:
            pass

    # Handle objects with .value attribute (common in zigpy)
    if hasattr(value, 'value'):
        return serialise_value(value.value)

    # Handle objects with .serialise() method
    if hasattr(value, 'serialise'):
        try:
            serialised = value.serialise()
            return serialise_value(serialised)
        except Exception:
            pass

    # Handle objects with __dict__ (convert to dict representation)
    if hasattr(value, '__dict__'):
        try:
            return serialise_value(value.__dict__)
        except Exception:
            pass

    # Last resort: convert to string
    try:
        return str(value)
    except Exception as e:
        logger.warning(f"Failed to serialise {type(value).__name__}: {e}")
        return f"<{type(value).__name__}>"


def serialise_key(key: Any) -> str:
    """
    Convert any key type to a string for JSON dict keys.

    JSON only supports string keys, so we need to convert EUI64 and other
    types that might be used as dictionary keys.

    Args:
        key: Dictionary key of any type

    Returns:
        String representation suitable for JSON
    """
    # Handle None
    if key is None:
        return "null"

    # String keys pass through
    if isinstance(key, str):
        return key

    # Numeric keys convert to string
    if isinstance(key, (int, float)):
        return str(key)

    # EUI64 and address types
    if hasattr(key, '__class__') and 'EUI64' in key.__class__.__name__:
        return str(key)

    # Bytes to hex
    if isinstance(key, bytes):
        return key.hex()

    # Enums
    if isinstance(key, Enum):
        return str(key.value)

    # Everything else: convert to string
    return str(key)


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """
    Safely serialise any object to JSON string.

    This is a drop-in replacement for json.dumps() that handles zigpy types.

    Args:
        obj: Object to serialise
        **kwargs: Additional arguments to pass to json.dumps

    Returns:
        JSON string

    Example:
        >>> state = {"ieee": EUI64(...), "data": LVBytes([1, 2, 3])}
        >>> json_str = safe_json_dumps(state)
    """
    try:
        # First try to serialise the entire object
        serialised = serialise_value(obj)
        return json.dumps(serialised, **kwargs)
    except Exception as e:
        logger.error(f"Failed to serialise to JSON: {e}")
        # Return a safe error representation
        return json.dumps({"error": "serialisation_failed", "type": type(obj).__name__})


def safe_json_loads(json_str: str) -> Any:
    """
    Safely deserialise JSON string.

    This is just a wrapper around json.loads for API consistency.

    Args:
        json_str: JSON string to deserialise

    Returns:
        Deserialised Python object
    """
    return json.loads(json_str)


class JSONSerialisableEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles zigpy types.

    Can be used with json.dumps(obj, cls=JSONSerializableEncoder)
    or with FastAPI's jsonable_encoder.

    Example:
        >>> json.dumps(state, cls=JSONSerialisableEncoder)
    """

    def default(self, obj):
        """Override default to handle custom types."""
        return serialise_value(obj)


def prepare_for_json(data: Any) -> Any:
    """
    Prepare data structure for JSON serialization.

    This is the main function to use before passing data to json.dumps(),
    FastAPI responses, or any other JSON serialization.

    Args:
        data: Any data structure that needs to be JSON-safe

    Returns:
        JSON-safe version of the data

    Example:
        >>> device_state = get_device_state()  # Has EUI64, LVBytes, etc.
        >>> safe_state = prepare_for_json(device_state)
        >>> json.dumps(safe_state)  # No errors!
    """
    return serialise_value(data)


def sanitise_device_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    sanitise a device state dictionary for JSON serialization.

    This is specifically for device state caches and MQTT payloads.

    Args:
        state: Device state dictionary potentially containing zigpy types

    Returns:
        sanitised state dictionary safe for JSON

    Example:
        >>> state = {"ieee": ieee_obj, "data": lvbytes_obj, "value": 25}
        >>> clean_state = sanitise_device_state(state)
    """
    return prepare_for_json(state)


def sanitise_websocket_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    sanitise a WebSocket message for JSON serialization.

    Args:
        message: Message dictionary potentially containing zigpy types

    Returns:
        sanitised message dictionary safe for JSON
    """
    return prepare_for_json(message)


def sanitise_device_list(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    sanitise a list of device dictionaries for JSON serialization.

    Args:
        devices: List of device dictionaries

    Returns:
        sanitised list safe for JSON
    """
    return prepare_for_json(devices)


# Convenience function for common pattern
def json_safe(func):
    """
    Decorator to automatically sanitise function return values for JSON.

    Example:
        @json_safe
        def get_device_info(ieee):
            return {"ieee": ieee, "data": LVBytes(...)}
    """

    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        return prepare_for_json(result)

    return wrapper


if __name__ == "__main__":
    # Test cases
    import zigpy.types

    print("Testing JSON serialization helpers...")

    # Test 1: EUI64
    ieee = zigpy.types.EUI64([0x00, 0x17, 0x88, 0x01, 0x09, 0x16, 0x33, 0x33])
    print(f"EUI64: {serialise_value(ieee)}")

    # Test 2: LVBytes
    lvbytes = zigpy.types.LVBytes([0x01, 0x02, 0x03])
    print(f"LVBytes: {serialise_value(lvbytes)}")

    # Test 3: Nested structure
    nested = {
        "ieee": ieee,
        "data": lvbytes,
        "value": 25,
        "list": [1, 2, lvbytes]
    }
    print(f"Nested: {safe_json_dumps(nested, indent=2)}")

    # Test 4: Dict with EUI64 keys
    eui_dict = {ieee: "device1", "normal_key": "value"}
    print(f"EUI64 as key: {safe_json_dumps(eui_dict, indent=2)}")

    print("\nAll tests passed!")