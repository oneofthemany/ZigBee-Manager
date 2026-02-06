# Aqara Manufacturer Cluster (0xFCC0) Implementation Guide

## Overview

The Aqara manufacturer-specific cluster (0xFCC0) is used by many Aqara/Xiaomi/LUMI devices to provide proprietary functionality beyond standard Zigbee clusters. This implementation is based on patterns from ZHA (Zigbee Home Assistant) and zhaquirks.

**Manufacturer Code:** `0x115F` (LUMI/Aqara)

## Why Do You Need This Cluster?

### Critical Functionality
The 0xFCC0 cluster is **essential** for:

1. **TRV/Thermostat Configuration:**
   - Window detection (auto-off when window opens)
   - Child lock (disable physical buttons)
   - Valve error detection
   - Motor calibration
   - External temperature sensor support
   
2. **Motion Sensor Configuration:**
   - Detection interval adjustment
   - Sensitivity settings (low/medium/high)
   - LED indicator control

3. **Switch/Relay Configuration:**
   - Decoupled mode (switch triggers events without controlling relay)
   - Multi-click detection mode
   - Indicator light behavior
   - Power memory after outage

**Without this cluster:** Your Aqara devices will work for basic stuff, without configuration options and advanced features.

## Implementation Details

### Class Structure

```python
@register_handler(0xFCC0)
class AqaraManufacturerCluster(ClusterHandler):
    """Handles Aqara manufacturer-specific cluster."""
    CLUSTER_ID = 0xFCC0
    MANUFACTURER_CODE = 0x115F  # CRITICAL: Must use this manufacturer code
```

### Key Principles from ZHA

1. **No Binding Required:**
   - Unlike standard clusters, 0xFCC0 does NOT need binding
   - Binding manufacturer clusters can cause issues
   - Just read/write attributes with manufacturer code

2. **Manufacturer Code is Mandatory:**
   ```python
   # CORRECT - Always include manufacturer parameter
   await cluster.read_attributes([attr], manufacturer=0x115F)
   await cluster.write_attributes({attr: value}, manufacturer=0x115F)
   
   # WRONG - Will fail or timeout
   await cluster.read_attributes([attr])  # Missing manufacturer code!
   ```

3. **Device-Specific Attributes:**
   - Not all attributes exist on all devices
   - Implementation intelligently reads based on device type
   - Gracefully handles missing attributes

## Supported Attributes

### Common Attributes (Most Devices)
| Attribute ID  | Name                | Type   | Values | Description                     |
|---------------|---------------------|--------|--------|---------------------------------|
| 0x0009        | mode                | uint8  | -      | Device mode                     |
| 0x0201        | power_outage_memory | Bool   | 0/1    | Remember state after power loss |

### Thermostat/TRV Attributes
| Attribute ID | Name                | Type    | Values                 | Description                 |
|--------------|---------------------|---------|------------------------|-----------------------------|
| 0x0271       | system_mode         | uint8   | -                      | System operating mode       |
| 0x0272       | window_detection    | uint8   | 0=Off, 1=On            | Auto-off when window opens  |
| 0x0273       | valve_detection     | uint8   | 0=Off, 1=On            | Detect valve errors         |
| 0x0274       | child_lock          | uint8   | 0=Unlock, 1=Lock       | Lock physical controls      |
| 0x0276       | battery_replace     | uint8   | 0/1                    | Battery low indicator       |
| 0x0277       | window_open         | uint8   | 0=Closed, 1=Open       | Current window status       |
| 0x0278       | valve_alarm         | uint8   | 0/1                    | Valve error alarm           |
| 0x0279       | motor_calibration   | uint8   | 0=Idle, 1=Start        | Calibrate valve motor       |
| 0x027C       | sensor_type         | uint8   | 0=Internal, 1=External | Temperature sensor source   |
| 0x027D       | external_temp_input | int16   | temp×100               | External sensor temperature |

### Motion Sensor Attributes
| Attribute ID | Name                 | Type    | Values               | Description                  |
|--------------|----------------------|---------|----------------------|------------------------------|
| 0x0102       | detection_interval   | uint8   | 5-300                | Seconds between detections   |
| 0x010C       | motion_sensitivity   | uint8   | 1=Low, 2=Med, 3=High | Detection sensitivity        |
| 0x0152       | trigger_indicator    | uint8   | 0=Off, 1=On          | Flash LED on detection       |

### Switch/Relay Attributes
| Attribute ID | Name             | Type    | Values                  | Description                   |
|--------------|------------------|---------|-------------------------|-------------------------------|
| 0x0200       | operation_mode   | uint8   | 0=Decoupled, 1=Coupled  | Switch/relay linkage          |
| 0x0004       | switch_mode      | uint8   | 1=Fast, 2=Multi         | Response speed vs multi-click |
| 0x000A       | switch_type      | uint8   | 1=Toggle, 2=Momentary   | Switch behavior               |
| 0x00F0       | indicator_light  | uint8   | 0=Normal, 1=Reverse     | LED indicator behavior        |

### Temperature/Humidity Sensor Attributes
| Attribute ID | Name                 | Type    | Values      | Description           |
|--------------|----------------------|---------|-------------|-----------------------|
| 0xFF01       | temp_display_unit    | uint8   | 0=°C, 1=°F  | Display unit          |
| 0x00EF       | measurement_interval | uint16  | seconds     | Measurement frequency |

## Usage Examples

### Reading Attributes

```python
# Read single attribute
value = await handler.read_attribute(handler.ATTR_WINDOW_DETECTION)

# Reading happens automatically during configure()
await handler.configure()  # Reads all relevant attributes
```

### Writing Attributes

```python
# Enable window detection
success = await handler.write_attribute(
    handler.ATTR_WINDOW_DETECTION,
    1  # Enable
)

# Set motion sensitivity to high
success = await handler.write_attribute(
    handler.ATTR_MOTION_SENSITIVITY,
    3  # High
)

# Enable decoupled mode (switch doesn't control relay)
success = await handler.write_attribute(
    handler.ATTR_OPERATION_MODE,
    0  # Decoupled
)
```

### Calibrating TRV Valve

```python
# Start calibration (takes ~2 minutes)
await handler.write_attribute(handler.ATTR_MOTOR_CALIBRATION, 1)

# Monitor status (will auto-update to 0 when complete)
# Or manually check:
status = await handler.read_attribute(handler.ATTR_MOTOR_CALIBRATION)
```

## Device Type Detection

The handler automatically detects device type and only reads/configures relevant attributes:

```python
async def poll(self):
    attrs_to_read = [self.ATTR_POWER_OUTAGE_MEM]  # Common to all
    
    # Add thermostat attributes if device has HVAC
    if hasattr(self.device, 'hvac'):
        attrs_to_read.extend([
            self.ATTR_WINDOW_DETECTION,
            self.ATTR_CHILD_LOCK,
            # ...
        ])
    
    # Add motion attributes if device has occupancy sensor
    if hasattr(self.device, 'occupancy'):
        attrs_to_read.extend([
            self.ATTR_DETECTION_INTERVAL,
            self.ATTR_MOTION_SENSITIVITY,
            # ...
        ])
```

## ZHA Pattern References

This implementation follows ZHA's established patterns:

### XiaomiAqaraE1Cluster Base
- Located in `zhaquirks/xiaomi/__init__.py`
- Provides base handling for Aqara E1 series devices
- Handles manufacturer-specific attributes with proper codes

### OppleCluster Derivatives
- Found in `zhaquirks/xiaomi/aqara/opple_*.py` files
- OppleCluster extends XiaomiAqaraE1Cluster
- Each device type (switch, remote, motion) has specific attributes

### Common Patterns
```python
# From ZHA's opple_remote.py
class OppleCluster(XiaomiAqaraE1Cluster):
    attributes = {
        0x0009: ("mode", types.uint8_t, True),
    }
    attr_config = {0x0009: 0x01}
    
    async def bind(self):
        result = await super().bind()
        await self.write_attributes(
            self.attr_config, 
            manufacturer=OPPLE_MFG_CODE  # 0x115F
        )
        return result
```

## Integration with Home Assistant

### MQTT Discovery

The handler generates appropriate discovery configs:

```python
def get_discovery_configs(self):
    return [
        {
            "component": "binary_sensor",
            "object_id": "window_open",
            "config": {
                "name": "Window Open",
                "device_class": "window",
                "value_template": "{{ value_json.window_open }}",
            }
        },
        # More configs...
    ]
```

### Configuration UI

Configuration options are exposed via `get_configuration_options()`:

```python
{
    "name": "window_detection",
    "label": "Window Detection",
    "type": "select",
    "options": [
        {"value": 0, "label": "Disabled"},
        {"value": 1, "label": "Enabled"}
    ],
    "attribute_id": 0x0272,
    "manufacturer_code": 0x115F
}
```

## Debugging

### Enable Debug Logging

```python
import logging
logging.getLogger("handlers.aqara").setLevel(logging.DEBUG)
```

### Common Issues

**Problem:** Attributes return timeout or "unsupported"
**Solution:** Ensure you're using `manufacturer=0x115F` in all read/write operations

**Problem:** Device doesn't respond to writes
**Solution:** 
1. Check device is powered and reachable
2. Verify attribute ID is correct for your device model
3. Try reading attribute first to confirm device supports it

**Problem:** Unknown attributes logged
**Solution:** This is normal - devices may have undocumented attributes. Check logs:
```
Aqara 0xFCC0 unknown attr 0x0123 = 45
```

## Device-Specific Notes

### Aqara TRV E1 (lumi.airrtc.agl001)
- Supports all thermostat attributes
- Window detection is HIGHLY recommended
- Calibration needed after battery replacement
- External sensor support via 0x027C/0x027D

### Aqara Motion Sensor P1 (lumi.motion.ac02)
- Use 0x0102 to prevent rapid re-triggering
- High sensitivity (0x010C=3) may cause false triggers
- Disable LED (0x0152=0) to save battery

### Aqara Wall Switch H1 (lumi.switch.acn0xx)
- Decoupled mode (0x0200=0) enables scene controller functionality
- Multi mode (0x0004=2) enables double/triple click detection
- Power outage memory (0x0201) remembers last state

## Comparison: With vs Without This Cluster

### WITHOUT 0xFCC0 Implementation
✗ No window detection - TRV keeps heating with window open  
✗ Cannot disable child lock - buttons remain locked  
✗ No motion sensor tuning - fixed sensitivity and interval  
✗ No decoupled switch mode - can't use as scene controller  
✗ No power memory - devices reset after power loss  

### WITH 0xFCC0 Implementation
✓ Full device configuration access  
✓ Energy saving via window detection  
✓ Child lock control  
✓ Motion sensor customization  
✓ Advanced switch modes (decoupled, multi-click)  
✓ Power outage memory  
✓ External temperature sensor support  

## Testing Checklist

- [ ] Read common attributes (power_outage_memory)
- [ ] Read device-specific attributes (varies by type)
- [ ] Write configuration changes
- [ ] Verify attribute updates trigger state changes
- [ ] Check Home Assistant discovery configs
- [ ] Test configuration UI options
- [ ] Monitor for unknown attributes

## References

- [ZHA Device Handlers Repository](https://github.com/zigpy/zha-device-handlers)
- [XiaomiAqaraE1Cluster Source](https://github.com/zigpy/zha-device-handlers/blob/dev/zhaquirks/xiaomi/__init__.py)
- [Aqara OppleCluster Implementations](https://github.com/zigpy/zha-device-handlers/tree/dev/zhaquirks/xiaomi/aqara)
- [Zigbee Cluster Library Specification](https://zigbeealliance.org/wp-content/uploads/2019/12/07-5123-06-zigbee-cluster-library-specification.pdf)

## Summary

The Aqara manufacturer cluster (0xFCC0) is **essential** for full device functionality. Without it, you lose access to all configuration options and advanced features. The implementation:

1. Uses manufacturer code 0x115F for all operations
2. Does NOT require binding (manufacturer-specific cluster)
3. Intelligently detects device type and reads relevant attributes
4. Provides write_attribute() for configuration changes
5. Follows ZHA's battle-tested patterns
6. Gracefully handles missing attributes

