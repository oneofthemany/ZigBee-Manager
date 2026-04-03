#!/usr/bin/env python3
"""
Hive Device Pairing Helper (Full Commissioning)
Automates Binding AND Configuration for Hive Thermostats (SLT6).

Fixes the "Searching..." issue by:
1. Binding Thermostat -> Receiver (Control)
2. Binding Thermostat -> Coordinator (Reporting)
3. Forcing System Mode -> HEAT (Wake up)
4. Syncing Target Temperature
"""
import sys
import requests
import time
import json

BASE_URL = "http://localhost:8000"

def get_devices():
    try:
        r = requests.get(f"{BASE_URL}/api/devices")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Error fetching devices: {e}")
        return []

def bind_devices(source_ieee, target_ieee, cluster_id):
    print(f"   > Binding {source_ieee} -> {target_ieee} (Cluster 0x{cluster_id:04x})...")
    try:
        payload = {
            "source_ieee": source_ieee,
            "target_ieee": target_ieee,
            "cluster_id": cluster_id
        }
        r = requests.post(f"{BASE_URL}/api/device/bind", json=payload)
        res = r.json()
        if res.get("success"):
            print("     [SUCCESS] Binding created!")
            return True
        else:
            print(f"     [FAILED] {res.get('error')}")
            return False
    except Exception as e:
        print(f"     [ERROR] {e}")
        return False

def send_command(ieee, command, value):
    print(f"   > Sending Command '{command}' = {value} to {ieee}...")
    try:
        payload = {
            "ieee": ieee,
            "command": command,
            "value": value
        }
        r = requests.post(f"{BASE_URL}/api/device/command", json=payload)
        res = r.json()
        if res.get("success"):
            print("     [SUCCESS] Command sent!")
            return True
        else:
            print(f"     [FAILED] {res.get('error')}")
            return False
    except Exception as e:
        print(f"     [ERROR] {e}")
        return False

def main():
    print("=" * 60)
    print("HIVE COMMISSIONING WIZARD")
    print("=" * 60)
    print("Steps:")
    print("1. Bind Thermostat -> Receiver (Relay Control)")
    print("2. Bind Thermostat -> Coordinator (Home Assistant Updates)")
    print("3. Force 'HEAT' mode (Stops 'Searching...')")
    print()

    devices = get_devices()
    
    thermostats = []
    receivers = []
    coordinator = None

    # Identify devices
    for d in devices:
        model = (d.get('model') or "").upper()
        manuf = (d.get('manufacturer') or "").upper()
        
        if d.get('type') == 'Coordinator':
            coordinator = d
        
        if "SLT6" in model or "SLT6" in manuf:
            thermostats.append(d)
        elif "SLR" in model or "RECEIVER" in model or "HEATLINK" in model:
            receivers.append(d)

    if not thermostats:
        print("❌ No Hive Thermostats (SLT6) found.")
        return
    if not receivers:
        print("❌ No Hive Receivers (SLR1c/b) found.")
        return
    if not coordinator:
        print("❌ Coordinator not found (Critical for reporting).")
        return

    # Select Devices
    sl_thermo = thermostats[0]
    sl_receiver = receivers[0]

    print(f"Targeting:")
    print(f"   Thermostat:  {sl_thermo['friendly_name']} ({sl_thermo['ieee']})")
    print(f"   Receiver:    {sl_receiver['friendly_name']} ({sl_receiver['ieee']})")
    print(f"   Coordinator: {coordinator['friendly_name']} ({coordinator['ieee']})")
    print("-" * 60)

    # --- STEP 1: BIND THERMOSTAT -> RECEIVER ---
    print("\n[1/3] Binding Thermostat to Receiver...")
    # Thermostat (0x0201)
    bind_devices(sl_thermo['ieee'], sl_receiver['ieee'], 0x0201)
    
    # --- STEP 2: BIND THERMOSTAT -> COORDINATOR ---
    print("\n[2/3] Binding Thermostat to Coordinator (for Reporting)...")
    # This ensures HA gets temperature updates immediately
    bind_devices(sl_thermo['ieee'], coordinator['ieee'], 0x0201)

    # --- STEP 3: WAKE UP SEQUENCE ---
    print("\n[3/3] Sending Wake-Up Configuration...")
    print("Wait for the thermostat screen to light up...")
    time.sleep(2)

    # Force System Mode to HEAT (This usually stops 'Searching')
    # Use 'heat' string or 4 (int)
    send_command(sl_thermo['ieee'], "system_mode", "heat")
    
    time.sleep(1)
    
    # Set a target temperature to force a sync
    # 20.0 C
    send_command(sl_thermo['ieee'], "temperature", 20.0)

    print("-" * 60)
    print("✅ COMMISSIONING COMPLETE")
    print("Check the Thermostat display. It should now show the target temp.")
    print("If it still says 'Searching', press the 'Back' or 'Menu' button on the thermostat to wake it up, then run this script again.")

if __name__ == "__main__":
    main()
