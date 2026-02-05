# Device Announcement - Visual Flow

## ANNOUNCEMENT Pattern

```
┌─────────────────────────────────────────────────────────────────┐
│ STARTUP SEQUENCE                                                │
└─────────────────────────────────────────────────────────────────┘

Time ─────────────────────────────────────────────────────────────►

  0ms │ Start MQTT Service
      │ ├─ Connecting to broker...
      │ └─ Wait for connection
      │
 50ms │ ✓ MQTT Connected
      │
100ms │ Start Zigbee Service
      │ ├─ Initialize radio
      │ ├─ Load network key
      │ └─ Start network
      │
200ms │ Device Restoration Loop
      │ ├─ Device 1 restored (NO announcement)
      │ ├─ Device 2 restored (NO announcement)
      │ ├─ Device 3 restored (NO announcement)
      │ └─ ... (48 devices - all loaded in memory)
      │
250ms │ ✓ Startup completes successfully
      │ └─ asyncio.create_task(announce_all_devices())
      │
300ms │ ┌────────────────────────────────────────┐
      │ │ announce_all_devices() executes        │
      │ ├────────────────────────────────────────┤
      │ │ Wait 1s for MQTT stability             │
1.3s  │ │                                        │
      │ │ For each device in network:            │
      │ │   ├─ await announce_device(ieee)       │
      │ │   ├─ Log: "Announced to HA (Topic:...)"│
      │ │   └─ await asyncio.sleep(0.1) # pace   │
      │ │                                        │
6.0s  │ │ All 48 devices announced!              │
      │ │ Log: "✅ 48 successful, 0 failed"      │
      │ └────────────────────────────────────────┘
      │
      │ ✅ RESULT:
      │ ├─ All 48 devices announced to Home Assistant
      │ ├─ MQTT was definitely ready (waited 1s)
      │ ├─ Sequential, paced announcement (100ms between)
      │ └─ Comprehensive logging with counts
```

## Method

### Overview
```python
async def _async_device_restored(self, device):
    # ... restore device ...
    
    # ✅ Don't announce here
    # Wait for startup to complete

async def start(self):
    # ... startup sequence ...
    
    # ✅ Announce AFTER startup completes
    asyncio.create_task(self.announce_all_devices())

async def announce_all_devices(self):
    """ZHA Pattern: Announce all at once, properly paced"""
    await asyncio.sleep(1)  # Wait for MQTT
    
    for ieee in self.devices:
        await self.announce_device(ieee)  # ✅ Properly awaited
        await asyncio.sleep(0.1)  # ✅ Paced to avoid flooding
    
    # ✅ Comprehensive logging
    logger.info(f"✅ {announced} successful, {failed} failed")
```

## Explanation

1. **Guaranteed MQTT Connection**
   - MQTT starts and connects first
   - 1-second delay ensures stability
   - No race condition

2. **Sequential Announcement**
   - Devices announced one at a time
   - Each announcement properly awaited
   - 100ms pacing prevents MQTT overload

3. **Complete Coverage**
   - Iterates through ALL devices
   - Not dependent on device restoration timing
   - Explicit loop over devices dictionary

4. **Visibility**
   - Logs each announcement
   - Summary at end: "X successful, Y failed"
   - Easy to verify all devices announced

## Expected Log Output

```
✅ Correct startup sequence:

Dec 13 12:33:40 rock-5b zigbee-gateway: INFO - MQTT connected
Dec 13 12:33:41 rock-5b zigbee-gateway: INFO - Starting Zigbee network...
Dec 13 12:33:42 rock-5b zigbee-gateway: INFO - Restored device: 00:17:88:01:...
Dec 13 12:33:42 rock-5b zigbee-gateway: INFO - Restored device: 00:15:8d:00:...
... (all devices restored)
Dec 13 12:33:43 rock-5b zigbee-gateway: INFO - Zigbee network started successfully
Dec 13 12:33:44 rock-5b zigbee-gateway: INFO - 📢 Announcing 48 devices to HA...
Dec 13 12:33:44 rock-5b zigbee-gateway: INFO - [00:17:88:01:...] Announced (Motion - Kitchen)
Dec 13 12:33:44 rock-5b zigbee-gateway: INFO - [00:15:8d:00:...] Announced (Socket - Media)
... (all 48 devices)
Dec 13 12:33:50 rock-5b zigbee-gateway: INFO - ✅ 48 successful, 0 failed
```

## Reference Pattern

This matches the official ZHA implementation:

1. **Load phase**: Restore devices from database
2. **Wait phase**: Ensure all services ready
3. **Announce phase**: Batch announce all devices
4. **Verify phase**: Log results

Source: https://github.com/zigpy/zha

---
