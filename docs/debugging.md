# Zigbee Manager Debugging Guide

## Overview

The Zigbee Manager includes comprehensive debugging capabilities to help troubleshoot device communication issues, especially for motion sensors and other battery-powered devices.

## Features

### 1. Frontend Debug Controls
- **Enable/Disable Debugging**: Toggle debugging on/off via the web interface
- **File Logging**: Optionally log all debug data to rotating log files
- **Live Packet View**: See Zigbee packets in real-time
- **Motion Event Tracking**: Dedicated view for motion sensor events
- **Downloadable Logs**: Download debug logs for offline analysis

### 2. Debug Capabilities
- Raw Zigbee packet capture with full decoding
- ZCL frame header and payload parsing
- Cluster command identification
- Attribute report tracking
- Handler trigger monitoring
- Motion detection event logging
- IAS Zone status change tracking
- On/Off cluster command analysis (including `on_with_timed_off`)

### 3. File Logging
- Automatic log rotation (10MB per file, 5 backups)
- Structured log format for easy parsing
- Separate debug log from main application log
- Configurable retention policies

## Usage

### Enabling Debugging

1. **Via Web Interface**:
   - Navigate to the "Debug Log" tab
   - Click "Enable Debug" button
   - Debugging starts immediately with file logging

2. **Via API**:
   ```bash
   curl -X POST http://localhost:8000/api/debug/enable
   ```

### Viewing Debug Data

1. **Live Logs**: Real-time logs appear in the Debug tab

2. **Packet View**: Click "Packets" to see captured Zigbee packets with:
   - Timestamp
   - Source device
   - Direction (RX/TX)
   - Cluster information
   - Command details
   - Decoded payload

3. **Motion Events**: Click "Motion" to see motion detection events including:
   - Detection source (IAS Zone, Occupancy Sensing, On/Off cluster)
   - Detection time and duration
   - Device information

4. **Download Logs**: Click "Download" to save debug logs locally

### Filtering

- **Log Level**: Filter by INFO, WARNING, ERROR, DEBUG
- **Device**: Filter by IEEE address
- **Packet Importance**: Filter by critical, high, medium, normal
- **Cluster**: Filter by cluster ID

## Understanding the Output

### Philips Hue Motion Sensor Example

When a Philips Hue SML001 motion sensor detects motion, you'll see:

```
[18:06:19.826] RX | [00:17:88:] | EP1→1 | On/Off
CMD: On With Timed Off | 🚨 MOTION DETECTED
→ OnOffHandler

Details:
- on_off_control: 0
- on_time: 3000 (300 seconds)
- off_wait_time: 0
```

### IAS Zone Motion Sensor Example

For sensors using IAS Zone cluster:

```
[18:06:19.826] RX | [00:17:88:] | EP1→1 | IAS Zone
CMD: Zone Status Change Notification | 🚨 MOTION DETECTED
→ IASZoneHandler

Zone Status: 0x0001
- alarm1_motion: True
- tamper: False
- battery_low: False
```

### Attribute Reports

Temperature sensor reporting:

```
[18:06:19.826] RX | [00:17:88: | EP1→1 | Temperature Measurement
Report Attributes | temperature=22.5°C
→ TemperatureMeasurementHandler
```

## Log Files

### Locations

- **Main Log**: `logs/zigbee.log`
- **Debug Log**: `logs/zigbee_debug.log`
- **Rotated Logs**: `logs/zigbee_debug.log.YYYYMMDD`

### Log Format

```
[TIMESTAMP] | DIRECTION | [IEEE] | EP_SRC→EP_DST | CLUSTER_NAME | DETAILS
```

Example:
```
2025-01-26 18:06:19.826 | 📡 [00:17:88:] On/Off cluster_command callback! tsn=24, cmd=0x42, args=(0, 3000, 0)
2025-01-26 18:06:19.827 | 🚨 MOTION: [00:17:88:] via on_with_timed_off (on_time=300.0s)
```

## Log Rotation

### Automatic Rotation (Built-in)

The Python `RotatingFileHandler` automatically rotates logs when they reach 10MB.

### System Logrotate (Recommended)

For production deployments, use the provided logrotate configuration:

```bash
# Install logrotate config
sudo cp zigbee-logrotate.conf /etc/logrotate.d/zigbee-gateway
sudo chmod 644 /etc/logrotate.d/zigbee-gateway

# Test configuration
sudo logrotate -d /etc/logrotate.d/zigbee-gateway

# Force rotation (for testing)
sudo logrotate -f /etc/logrotate.d/zigbee-gateway
```

### Customising Rotation

Edit `/etc/logrotate.d/zigbee-gateway` to customize:

- **Rotation frequency**: `daily`, `hourly`, `weekly`, `monthly`, `size 10M`
- **Retention**: `rotate 7` (keep 7 rotated files)
- **Compression**: `compress`, `delaycompress`
- **Max age**: `maxage 30` (delete files older than 30 days)

## Troubleshooting Common Issues

### Motion Sensor Not Triggering

1. **Enable debugging** to see raw Zigbee traffic
2. **Check for packets** from the sensor's IEEE address
3. **Look for**:
   - IAS Zone Status Change Notifications (cluster 0x0500)
   - Occupancy Sensing attribute reports (cluster 0x0406)
   - On/Off cluster commands (cluster 0x0006, command 0x42)

4. **Verify handler registration**:
   - Look for "✅ HANDLER" or "✅ COMMAND" log entries
   - Ensure the appropriate handler is triggered

### Debug Logs Growing Too Large

1. **Disable debugging** when not needed
2. **Configure logrotate** for more aggressive rotation:
   ```
   # Rotate every hour, keep only 10 files
   hourly
   rotate 10
   size 5M
   ```

3. **Use filters** to reduce captured data:
   ```python
   debugger.set_filter(ieee="00:17:88:)  # Only one device
   debugger.set_filter(cluster=0x0006)  # Only On/Off cluster
   ```

### Performance Impact

Debugging has minimal performance impact:
- **CPU**: <5% increase
- **Memory**: ~50MB for 1000 packets
- **Disk I/O**: ~1MB/minute during active debugging

Disable debugging in production or use filters to minimize overhead.

## API Reference

### Enable Debugging
```
POST /api/debug/enable
Body: { "file_logging": true }
Response: { "status": "enabled", "file_logging": true }
```

### Disable Debugging
```
POST /api/debug/disable
Response: { "status": "disabled" }
```

### Get Debug Status
```
GET /api/debug/status
Response: {
  "enabled": true,
  "file_logging": true,
  "packets_captured": 1234,
  "total_packets": 5678,
  "motion_events": 42
}
```

### Get Packets
```
GET /api/debug/packets?limit=100&importance=critical
Response: {
  "success": true,
  "packets": [...]
}
```

### Get Motion Events
```
GET /api/debug/motion_events?limit=50
Response: {
  "success": true,
  "events": [...]
}
```

### Download Log File
```
GET /api/debug/log_file?lines=1000
Response: {
  "success": true,
  "content": "log file contents..."
}
```

## Best Practices

1. **Enable debugging only when needed** - it generates significant log data
2. **Use filters** to focus on specific devices or clusters
3. **Configure logrotate** for production environments
4. **Monitor disk space** when debugging is enabled for extended periods
5. **Download and analyze logs offline** for complex issues
6. **Disable file logging** if you only need live monitoring
7. **Clear debug data** periodically to free memory

## Advanced Debugging

### Analysing Packet Flows

1. Enable debugging
2. Trigger the issue (e.g., move in front of motion sensor)
3. Download debug log
4. Analyze the sequence of packets:
   ```
   grep "00:17:88:" zigbee_debug.log
   ```

### Identifying Missing Handlers

Look for "No explicit handler for cluster command" messages:

```
DEBUG:zigpy.zcl:[0xC58E:1:0x0006] No explicit handler for cluster command 0x42
```

This indicates a command was received but no handler processed it.

### Checking Binding Status

Enable debugging and look for binding-related messages during device configuration:

```
INFO:handlers.base:[00:17:88:] ✅ Bound On/Off, result: [0]
```

## Support

For issues or questions:
1. Enable debugging
2. Reproduce the issue
3. Download debug logs
4. Create an issue with logs attached

## License

See main project LICENSE file.