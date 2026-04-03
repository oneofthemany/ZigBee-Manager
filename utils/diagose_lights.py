#!/usr/bin/env python3
"""
Simplified Zigbee Light vs Switch Diagnostic Tool

This script analyzes your Zigbee devices without starting the full service.
It reads directly from device_state_cache.json and checks device signatures.
"""
import json
import sys
import os
from pathlib import Path

# Suppress zigpy warnings
import logging
logging.getLogger('zigpy.zcl').setLevel(logging.ERROR)

# Add project to path  
sys.path.insert(0, '/opt/zigbee_manager')

# ZHA's Light Device Types (from zigpy.profiles.zha)
LIGHT_DEVICE_TYPES = {
    0x0100,  # ON_OFF_LIGHT
    0x0101,  # DIMMABLE_LIGHT  
    0x0102,  # COLOR_DIMMABLE_LIGHT
    0x010C,  # COLOR_TEMPERATURE_LIGHT
    0x010D,  # EXTENDED_COLOR_LIGHT
    0x0301,  # DIMMABLE_BALLAST
    0x0303,  # DIMMABLE_PLUG_IN_UNIT
    0x0304,  # LEVEL_CONTROLLABLE_OUTPUT
}

# ZHA's Switch Device Types
SWITCH_DEVICE_TYPES = {
    0x0000,  # ON_OFF_SWITCH
    0x0103,  # ON_OFF_LIGHT_SWITCH (this is a controller, not a light!)
    0x0051,  # SMART_PLUG
    0x010A,  # ON_OFF_PLUG_IN_UNIT
    0x0300,  # ON_OFF_BALLAST
}

DEVICE_TYPE_NAMES = {
    0x0000: "ON_OFF_SWITCH",
    0x0002: "ROUTER", 
    0x0100: "ON_OFF_LIGHT",
    0x0101: "DIMMABLE_LIGHT",
    0x0102: "COLOR_DIMMABLE_LIGHT",
    0x0103: "ON_OFF_LIGHT_SWITCH",
    0x010C: "COLOR_TEMPERATURE_LIGHT",
    0x010D: "EXTENDED_COLOR_LIGHT",
    0x0051: "SMART_PLUG",
    0x010A: "ON_OFF_PLUG_IN_UNIT",
    0x0300: "ON_OFF_BALLAST",
    0x0301: "DIMMABLE_BALLAST",
    0x0303: "DIMMABLE_PLUG_IN_UNIT",
    0x0304: "LEVEL_CONTROLLABLE_OUTPUT",
}


def load_device_cache():
    """Load device state cache."""
    cache_file = Path('/opt/zigbee_manager/device_state_cache.json')
    if not cache_file.exists():
        print("❌ Error: device_state_cache.json not found!")
        print("   Make sure the Zigbee service has been running.")
        sys.exit(1)
    
    with open(cache_file, 'r') as f:
        return json.load(f)


def analyze_endpoint(ieee, model, ep_id, ep_data):
    """Analyze a single endpoint."""
    print(f"\n  Endpoint {ep_id}:")
    print(f"  {'-'*66}")
    
    # Get device_type if available
    device_type = ep_data.get('device_type')
    device_type_name = DEVICE_TYPE_NAMES.get(device_type, f"Unknown (0x{device_type:04X})") if device_type else "Not Set"
    
    if device_type:
        print(f"  Zigbee Device Type: {device_type_name}")
    else:
        print(f"  Zigbee Device Type: Not available in cache")
    
    # Determine what ZHA would classify this as
    zha_would_be = "unknown"
    if device_type in LIGHT_DEVICE_TYPES:
        zha_would_be = "light"
    elif device_type in SWITCH_DEVICE_TYPES:
        zha_would_be = "switch"
    elif device_type == 0x0002:  # Router
        zha_would_be = "N/A (router)"
    
    if zha_would_be != "N/A (router)" and device_type:
        print(f"  ZHA would classify as: {zha_would_be.upper()}")
    
    # Check clusters
    in_clusters = ep_data.get('in_clusters', [])
    has_onoff = 6 in in_clusters or 0x0006 in in_clusters
    has_level = 8 in in_clusters or 0x0008 in in_clusters
    has_color = 768 in in_clusters or 0x0300 in in_clusters
    
    print(f"  Clusters:")
    print(f"    • OnOff (0x0006):  {'✓' if has_onoff else '✗'}")
    print(f"    • Level (0x0008):  {'✓' if has_level else '✗'}")
    print(f"    • Color (0x0300):  {'✓' if has_color else '✗'}")
    
    if not has_onoff:
        print(f"  → Not a controllable on/off device")
        return None
    
    # Determine what YOUR code would classify this as
    should_be_by_type = device_type in LIGHT_DEVICE_TYPES if device_type else False
    should_be_by_cluster = has_level or has_color
    your_code_would_be = "light" if (should_be_by_type or should_be_by_cluster) else "switch"
    
    print(f"\n  Detection Logic:")
    print(f"    • By device_type: {should_be_by_type} → {'LIGHT' if should_be_by_type else 'SWITCH'}")
    print(f"    • By clusters: {should_be_by_cluster} → {'LIGHT' if should_be_by_cluster else 'SWITCH'}")
    print(f"    • Final result: {your_code_would_be.upper()}")
    
    # Compare with ZHA
    if zha_would_be != "N/A (router)" and zha_would_be != your_code_would_be and device_type:
        print(f"  ⚠️  NOTE: ZHA would classify as {zha_would_be}, your code as {your_code_would_be}")
    else:
        print(f"  ✅ Correctly detected as {your_code_would_be}")
    
    return {
        'ieee': ieee,
        'model': model,
        'endpoint': ep_id,
        'device_type': device_type,
        'device_type_name': device_type_name,
        'has_level': has_level,
        'has_color': has_color,
        'should_be': your_code_would_be,
        'zha_would_be': zha_would_be
    }


def main():
    print("="*70)
    print("ZIGBEE LIGHT VS SWITCH DIAGNOSTIC TOOL")
    print("="*70)
    print()
    
    # Load device cache
    print("Loading device cache...")
    devices = load_device_cache()
    print(f"✅ Loaded {len(devices)} devices\n")
    
    print("="*70)
    print("DEVICE ANALYSIS")
    print("="*70)
    
    results = []
    
    for ieee, device in devices.items():
        # Skip coordinator
        if ieee == '00:00:00:00:00:00:00:00':
            continue
        
        manufacturer = device.get('manufacturer', 'Unknown')
        model = device.get('model', 'Unknown')
        
        print(f"\n{'='*70}")
        print(f"📱 {manufacturer} {model}")
        print(f"   IEEE: {ieee}")
        print(f"{'='*70}")
        
        endpoints = device.get('endpoints', {})
        if not endpoints:
            print("  ⚠️  No endpoint data available")
            continue
        
        for ep_id, ep_data in endpoints.items():
            if ep_id == '0':  # Skip ZDO
                continue
            
            result = analyze_endpoint(ieee, model, ep_id, ep_data)
            if result:
                results.append(result)
    
    # Summary
    print(f"\n\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}\n")
    
    lights = [r for r in results if r['should_be'] == 'light']
    switches = [r for r in results if r['should_be'] == 'switch']
    
    print(f"Found {len(results)} controllable endpoints:")
    print(f"  • {len(lights)} lights")
    print(f"  • {len(switches)} switches")
    
    # Check for potential issues
    no_device_type = [r for r in results if not r['device_type']]
    if no_device_type:
        print(f"\n⚠️  {len(no_device_type)} devices missing device_type information")
        print("   (Detection will rely only on cluster presence)")
    
    print("\nNEXT STEPS:")
    print("1. Deploy the updated switches.py with device_type detection")
    print("2. Restart your Zigbee service")
    print("3. Check Home Assistant - lights should now have proper controls")
    print("\nIf entities still appear wrong in Home Assistant:")
    print("  • Restart Home Assistant to clear cached MQTT configs")
    print("  • Delete wrong entities in HA UI and let them rediscover")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
