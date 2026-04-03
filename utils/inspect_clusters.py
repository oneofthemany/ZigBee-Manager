#!/usr/bin/env python3
"""
Query Device Clusters from Running Service

This queries your Zigbee service API to see what clusters
devices actually have.
"""
import requests
import json

API_URL = "http://localhost:8000"

print("="*70)
print("DEVICE CLUSTER INSPECTOR")
print("="*70)
print()

# Get all devices
try:
    response = requests.get(f"{API_URL}/devices")
    devices_data = response.json()
    
    # Handle different response formats
    if isinstance(devices_data, dict):
        devices = list(devices_data.values())
        print(f"Found {len(devices)} devices\n")
    elif isinstance(devices_data, list):
        devices = devices_data
        print(f"Found {len(devices)} devices\n")
    else:
        print(f"Unexpected response format: {type(devices_data)}")
        print(f"Response: {devices_data}")
        exit(1)
    
    # Look for light devices
    light_keywords = ['led', 'pendant', 'lamp', 'bulb', 'light', 'tradfri', 'hue']
    
    found_lights = 0
    
    for device in devices:
        if isinstance(device, str):
            # Device is just an IEEE address, fetch details
            ieee = device
            try:
                detail_response = requests.get(f"{API_URL}/device/{ieee}")
                device = detail_response.json()
            except:
                continue
        else:
            ieee = device.get('ieee')
        
        friendly_name = device.get('friendly_name', device.get('name', 'Unknown'))
        manufacturer = device.get('manufacturer', 'Unknown')
        model = device.get('model', 'Unknown')
        
        # Check if this looks like a light
        name_lower = friendly_name.lower()
        is_potential_light = any(kw in name_lower for kw in light_keywords)
        
        if not is_potential_light:
            continue
        
        found_lights += 1
        friendly_name = device.get('friendly_name', 'Unknown')
        manufacturer = device.get('manufacturer', 'Unknown')
        model = device.get('model', 'Unknown')
        
        # Check if this looks like a light
        name_lower = friendly_name.lower()
        is_potential_light = any(kw in name_lower for kw in light_keywords)
        
        if not is_potential_light:
            continue
        
        print(f"{'='*70}")
        print(f"📱 {friendly_name}")
        print(f"   Manufacturer: {manufacturer}")
        print(f"   Model: {model}")
        print(f"   IEEE: {ieee}")
        print(f"{'='*70}")
        
        # Get device details
        try:
            detail_response = requests.get(f"{API_URL}/device/{ieee}")
            details = detail_response.json()
            
            endpoints = details.get('endpoints', {})
            
            for ep_id, ep_data in endpoints.items():
                if ep_id == '0':
                    continue
                
                print(f"\n  Endpoint {ep_id}:")
                
                in_clusters = ep_data.get('in_clusters', [])
                out_clusters = ep_data.get('out_clusters', [])
                
                # Check for the clusters we care about
                has_onoff = 0x0006 in in_clusters
                has_level = 0x0008 in in_clusters
                has_color = 0x0300 in in_clusters
                has_lightlink = 0x1000 in in_clusters
                has_opple = 0xFCC0 in in_clusters
                
                print(f"    In Clusters: {[f'0x{c:04X}' for c in in_clusters]}")
                
                print(f"\n    Key Clusters:")
                print(f"      • OnOff (0x0006):      {'✓' if has_onoff else '✗'}")
                print(f"      • Level (0x0008):      {'✓' if has_level else '✗'}")
                print(f"      • Color (0x0300):      {'✓' if has_color else '✗'}")
                print(f"      • LightLink (0x1000):  {'✓' if has_lightlink else '✗'}")
                print(f"      • Opple (0xFCC0):      {'✓' if has_opple else '✗'}")
                
                # Determine what it should be
                is_light = has_lightlink or has_opple or has_color
                
                print(f"\n    → Should be: {'LIGHT' if is_light else 'SWITCH'}")
                
                if not is_light and has_onoff:
                    print(f"    ⚠️  Has OnOff but no light-specific clusters!")
                    print(f"       This device will be detected as a SWITCH")
        
        except Exception as e:
            print(f"  ⚠️  Could not get details: {e}")
        
        print()
    
    if found_lights == 0:
        print("\n⚠️  No light devices found!")
        print("   Check that friendly names contain light-related keywords")

except Exception as e:
    print(f"❌ Error connecting to API: {e}")
    print(f"\nMake sure your Zigbee service is running on {API_URL}")
    import traceback
    traceback.print_exc()
