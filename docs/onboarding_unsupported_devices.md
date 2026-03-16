# Onboarding Unsupported Devices

ZigBee Manager can now onboard devices that don't have dedicated cluster handlers. When a device joins the network with clusters that aren't in the handler registry, the system automatically attaches a **GenericClusterHandler** that captures all attribute reports and commands — nothing is silently dropped.

You can then map raw attribute keys to friendly names through the UI, and optionally promote those mappings to model-level definitions so all devices of the same type get the same treatment automatically.

---

## How It Works

### The Three Layers

1. **GenericClusterHandler** — automatically attached to any cluster without a dedicated handler. Captures all data as raw keys like `cluster_0500_attr_0000`.

2. **Device Override Manager** — a JSON-driven definition system (`data/device_overrides.json`) that maps raw keys to friendly names, with optional scaling, units, and Home Assistant device classes.

3. **Mappings UI** — a tab in the device modal that lets you map attributes visually, no code changes or restarts required.

### What Gets Skipped

Infrastructure clusters are excluded from generic fallback automatically:

- `0x0019` — OTA Upgrade
- `0x0020` — Poll Control
- `0x0021` — Green Power Proxy
- `0x000A` — Time

These are network housekeeping, not device data.

---

## Step-by-Step: Onboarding a New Device

### 1. Pair the Device

Enable pairing from the web UI (or via a specific router) and put the device into pairing mode. Once joined, the device appears in the device list.

Check the application logs — you should see messages like:

```
[00:11:22:33:44:55:66:77] No handler for 0x0B04 on EP1 — using GenericClusterHandler
```

This confirms the generic fallback is working.

### 2. Exercise the Device

Trigger the device to produce data:

- Open/close a door sensor
- Trigger motion on a PIR
- Press buttons on a remote
- Change temperature on a TRV
- Toggle a switch

Each action should generate attribute reports or cluster commands that the GenericClusterHandler captures into the device state.

### 3. Open the Device Modal

Click **Manage** on the device in the device list. If any generic cluster data has been captured, a **Mappings** tab will appear alongside the standard Overview, Control, Binding, Clusters, and Automation tabs.

### 4. Review Unmapped Attributes

The Mappings tab shows two sections:

**Unmapped Attributes** — raw keys the device is reporting that don't have friendly names yet. Each row shows the raw key (e.g. `cluster_0500_attr_0000`) and its current value.

**Active Mappings** — any keys you've already mapped.

### 5. Map an Attribute

Click the **Map** button next to an unmapped key. A dialog appears with:

| Field               | Description                  | Example                              |
|---------------------|------------------------------|--------------------------------------|
| **Friendly Name**   | The state key this becomes   | `contact`, `temperature`, `humidity` |
| **Scale (divisor)** | Raw value is divided by this | `100` for ZCL centidegrees → °C      |
| **Unit**            | Unit of measurement for HA   | `°C`, `%`, `lux`                     |
| **Device Class**    | Home Assistant device class  | `temperature`, `humidity`, `door`    |

Click **Save**. The mapping takes effect immediately — no restart needed. Next time the device reports that attribute, it will appear under the friendly name in the device state and in Home Assistant.

### 6. Verify

After mapping, trigger the device again. Check:

- The **Overview** tab shows the friendly-named values
- Home Assistant receives the entity via MQTT discovery (if device class was set)
- The **Active Mappings** section in the Mappings tab reflects your configuration

### 7. Promote to Model Definition (Optional)

Once you're happy with the mappings for a device, click **Promote to Model Definition** at the bottom of the Mappings tab. This converts your per-device (IEEE) mappings into a model-level definition.

What this means: any other device with the same model and manufacturer that joins the network will automatically get the same friendly names, scaling, and units without any manual mapping.

Model definitions are stored in `data/device_overrides.json` and persist across restarts.

---

## Common Mapping Scenarios

### Door/Window Contact Sensor

A typical contact sensor uses IAS Zone (cluster `0x0500`). If the IAS Zone handler doesn't cover it:

| Raw Key                  | Friendly Name | Scale  | Unit  | Device Class  |
|--------------------------|---------------|--------|-------|---------------|
| `cluster_0500_attr_0000` | `contact`     | 1      |       | `door`        |

Zone status value `1` typically means open, `0` means closed.

### Temperature/Humidity Sensor

Standard ZCL temperature (cluster `0x0402`) reports in centidegrees:

| Raw Key                    | Friendly Name   | Scale | Unit | Device Class  |
|----------------------------|-----------------|-------|------|---------------|
| `cluster_0402_attr_0000`   | `temperature`   | 100   | °C   | `temperature` |
| `cluster_0405_attr_0000`   | `humidity`      | 100   | %    | `humidity`    |

### Power Monitoring Plug

Electrical measurement (cluster `0x0B04`):

| Raw Key                  | Friendly Name  | Scale  | Unit  | Device Class  |
|--------------------------|----------------|--------|-------|---------------|
| `cluster_0b04_attr_0505` | `rms_voltage`  | 10     | V     | `voltage`     |
| `cluster_0b04_attr_0508` | `rms_current`  | 1000   | A     | `current`     |
| `cluster_0b04_attr_050b` | `active_power` | 10     | W     | `power`       |

### Unknown Manufacturer Cluster

For manufacturer-specific clusters (e.g. `0xFC01`), use the debugger to observe what values appear, then map accordingly. There's no standard — each manufacturer defines their own attributes.

---

## Managing Definitions via API

All override management is available through the REST API for scripting or bulk operations.

### View all overrides

```
GET /api/device_overrides
```

### View mappings for a specific device

```
GET /api/device_overrides/{ieee}
```

Returns model definition (if any), per-device mappings, and a list of unmapped `cluster_*` keys currently in the device state.

### Add a per-device mapping

```
POST /api/device_overrides/ieee_mapping
Content-Type: application/json

{
    "ieee": "00:11:22:33:44:55:66:77",
    "raw_key": "cluster_0402_attr_0000",
    "friendly_name": "temperature",
    "scale": 100,
    "unit": "°C",
    "device_class": "temperature"
}
```

### Remove a per-device mapping

```
DELETE /api/device_overrides/ieee_mapping
Content-Type: application/json

{
    "ieee": "00:11:22:33:44:55:66:77",
    "raw_key": "cluster_0402_attr_0000"
}
```

### Add a model-level definition

```
POST /api/device_overrides/definition
Content-Type: application/json

{
    "model": "lumi.sensor_magnet.aq2",
    "manufacturer": "LUMI",
    "definition": {
        "clusters": {
            "0x0500": {
                "attributes": {
                    "0x0000": {
                        "name": "contact",
                        "device_class": "door"
                    }
                }
            }
        }
    }
}
```

### Remove a model-level definition

```
DELETE /api/device_overrides/definition
Content-Type: application/json

{
    "model": "lumi.sensor_magnet.aq2",
    "manufacturer": "LUMI"
}
```

---

## File Structure

```
data/
  device_overrides.json       # Persisted definitions and IEEE overrides

handlers/
  generic.py                  # GenericClusterHandler (fallback)

modules/
  device_overrides.py         # DeviceOverrideManager singleton

static/js/modal/
  mappings.js                 # Mappings tab UI
```

---

## Tips

- **Use the debugger** — if you're not sure what a device is reporting, check the Debug Packets modal or `logs/zigbee_debug.log`. The GenericClusterHandler logs every attribute and command it receives.

- **Scale matters** — ZCL attributes often use integer representations. Temperature is centidegrees (÷100), humidity is centipercent (÷100), voltage might be in decivolts (÷10). Check the ZCL specification for the correct divisor.

- **Promote early** — once you've confirmed the mappings work for one device, promote to a model definition immediately. If you buy a second one, it'll just work.

- **Restart not required** — all mapping changes via the UI or API are applied immediately. The override manager reloads from `device_overrides.json` at startup, and saves after every change.

- **Dedicated handlers are still better** — the generic system is great for getting devices working quickly, but if a device needs complex logic (state machines, command generation, binding quirks), a dedicated handler in `handlers/` is the right long-term solution. Use the generic system to understand the device, then write a proper handler if needed.