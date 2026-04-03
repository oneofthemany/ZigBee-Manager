#!/usr/bin/env python3
"""
Hive Thermostat Communication Diagnostic
Tests if SLT6 and SLR1c are communicating properly
"""
import asyncio
import requests
import time
from datetime import datetime

BASE_URL = "http://localhost:8000"

SLT6_IEEE = "00:1e:5e:09:02:a3:e7:27"  # Thermostat
SLR1C_IEEE = "00:1e:5e:09:02:a4:40:5a"  # Receiver

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def poll_device(ieee):
    """Poll a device and return its state"""
    try:
        r = requests.post(f"{BASE_URL}/api/device/poll", 
                         json={"ieee": ieee}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"❌ Poll failed: {e}")
        return None

def get_device_state(ieee):
    """Get current device state from device list"""
    try:
        r = requests.get(f"{BASE_URL}/api/devices", timeout=5)
        devices = r.json()
        for d in devices:
            if d['ieee'] == ieee:
                return d.get('state', {})
        return {}
    except Exception as e:
        log(f"❌ Get state failed: {e}")
        return {}

def check_thermostat():
    """Check SLT6 thermostat status"""
    log("\n📊 Checking Thermostat (SLT6)...")
    poll_device(SLT6_IEEE)
    time.sleep(1)
    
    state = get_device_state(SLT6_IEEE)
    
    # Check critical values
    system_mode = state.get('system_mode', 0)
    setpoint = state.get('occupied_heating_setpoint') or state.get('heating_setpoint', 0)
    local_temp = state.get('local_temperature') or state.get('current_temperature', 0)
    pi_demand = state.get('pi_heating_demand', 0)
    battery = state.get('battery', 0)
    
    log(f"  Battery: {battery}%")
    log(f"  System Mode: {system_mode} ({'OFF' if system_mode == 0 else 'HEAT' if system_mode == 4 else 'UNKNOWN'})")
    log(f"  Current Temp: {local_temp / 100:.1f}°C")
    log(f"  Target Temp: {setpoint / 100:.1f}°C")
    log(f"  Heating Demand: {pi_demand}%")
    
    # Diagnostics
    issues = []
    if battery < 20:
        issues.append("⚠️  LOW BATTERY - Replace batteries!")
    if system_mode == 0:
        issues.append("⚠️  THERMOSTAT IS OFF - Turn it on to HEAT mode!")
    if local_temp == 0:
        issues.append("⚠️  No temperature reading - Check battery/sensor")
    if setpoint <= local_temp and system_mode == 4:
        issues.append("⚠️  Target temp not above current - Increase setpoint to test")
    
    if issues:
        for issue in issues:
            log(issue)
        return False
    else:
        log("✅ Thermostat looks good!")
        return True

def check_receiver():
    """Check SLR1c receiver status"""
    log("\n📊 Checking Receiver (SLR1c)...")
    poll_device(SLR1C_IEEE)
    time.sleep(1)
    
    state = get_device_state(SLR1C_IEEE)
    
    # Check values
    system_mode = state.get('system_mode', 0)
    setpoint = state.get('occupied_heating_setpoint') or state.get('heating_setpoint', 0)
    local_temp = state.get('local_temperature') or state.get('current_temperature', 0)
    running_state = state.get('running_state', 0)
    
    log(f"  System Mode: {system_mode}")
    log(f"  Current Temp: {local_temp / 100:.1f}°C")
    log(f"  Target Temp: {setpoint / 100:.1f}°C")
    log(f"  Running State: {running_state} ({'IDLE' if running_state == 0 else 'HEATING'})")
    
    return True

def test_communication():
    """Test if thermostat and receiver are communicating"""
    log("\n🔗 Testing Communication...")
    
    # Get initial states
    thermo_state_1 = get_device_state(SLT6_IEEE)
    receiver_state_1 = get_device_state(SLR1C_IEEE)
    
    thermo_setpoint_1 = thermo_state_1.get('occupied_heating_setpoint', 0)
    receiver_setpoint_1 = receiver_state_1.get('occupied_heating_setpoint', 0)
    
    log(f"  Thermostat setpoint: {thermo_setpoint_1 / 100:.1f}°C")
    log(f"  Receiver setpoint: {receiver_setpoint_1 / 100:.1f}°C")
    
    if abs(thermo_setpoint_1 - receiver_setpoint_1) < 50:  # Within 0.5°C
        log("✅ Setpoints match - devices are synchronized!")
        return True
    else:
        log("⚠️  Setpoints don't match - communication may be broken")
        log("   Try adjusting the thermostat setpoint and check if receiver updates")
        return False

def main():
    print("=" * 60)
    print("Hive Thermostat Communication Diagnostic")
    print("=" * 60)
    
    # Check thermostat
    thermo_ok = check_thermostat()
    
    # Check receiver
    receiver_ok = check_receiver()
    
    # Test communication
    if thermo_ok:
        comm_ok = test_communication()
    else:
        log("\n⚠️  Fix thermostat issues before testing communication")
        comm_ok = False
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if thermo_ok and receiver_ok and comm_ok:
        print("✅ All checks passed - system is working!")
        print("\nNext steps:")
        print("  1. Adjust thermostat setpoint above current temperature")
        print("  2. Wait 10-30 seconds")
        print("  3. Listen for SLR1c relay click")
        print("  4. Check SLR1c LED turns red/orange")
    else:
        print("⚠️  Issues found - see warnings above")
        print("\nCommon fixes:")
        print("  1. Replace thermostat batteries if low")
        print("  2. Turn thermostat to HEAT mode (not OFF)")
        print("  3. Set target temp ABOVE current temp")
        print("  4. Re-bind devices if setpoints don't match")
    print("=" * 60)

if __name__ == "__main__":
    main()
