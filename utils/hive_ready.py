#!/usr/bin/env python3
"""
Hive Device Diagnostic Script
Run this to check if your system is ready for Hive device pairing
"""

import asyncio
import sys


async def check_hive_readiness():
    """Check if system is ready for Hive device pairing"""

    print("=" * 60)
    print("HIVE DEVICE PAIRING DIAGNOSTIC")
    print("=" * 60)
    print()

    issues = []
    warnings = []

    # Check 1: Quirks Library
    print("[1/6] Checking zhaquirks installation...")
    try:
        import zhaquirks
        print("   ✓ zhaquirks installed")

        # Try to import Centralite quirks (Hive devices are rebranded Centralite)
        try:
            import zhaquirks.centralite
            print("   ✓ Centralite quirks available")
        except ImportError:
            warnings.append("Centralite quirks not found - Hive devices may not work correctly")
            print("   ⚠ Centralite quirks not available")
    except ImportError:
        issues.append("zhaquirks not installed - run: pip install zha-quirks")
        print("   ✗ zhaquirks NOT installed")

    print()

    # Check 2: Handler Registration
    print("[2/6] Checking handler registration...")
    try:
        from handlers import get_handler_for_cluster, HANDLER_REGISTRY

        # Check for Thermostat handler (critical for Hive)
        thermostat_handler = get_handler_for_cluster(0x0201)
        if thermostat_handler:
            print(f"   ✓ ThermostatHandler registered: {thermostat_handler.__name__}")
        else:
            issues.append("No ThermostatHandler registered for cluster 0x0201")
            print("   ✗ ThermostatHandler NOT registered")

        # Check for other important handlers
        onoff_handler = get_handler_for_cluster(0x0006)
        if onoff_handler:
            print(f"   ✓ OnOffHandler registered: {onoff_handler.__name__}")
        else:
            warnings.append("No OnOffHandler for cluster 0x0006")
            print("   ⚠ OnOffHandler NOT registered")

    except Exception as e:
        issues.append(f"Failed to check handlers: {e}")
        print(f"   ✗ Error checking handlers: {e}")

    print()

    # Check 3: Core Service
    print("[3/6] Checking core service...")
    try:
        from core import ZigbeeService
        print("   ✓ ZigbeeService importable")

        # Check if bind_devices method exists
        if hasattr(ZigbeeService, 'bind_devices'):
            print("   ✓ bind_devices method available")
        else:
            issues.append("ZigbeeService missing bind_devices method")
            print("   ✗ bind_devices method NOT available")

    except Exception as e:
        issues.append(f"Failed to import core service: {e}")
        print(f"   ✗ Error: {e}")

    print()

    # Check 4: MQTT Service
    print("[4/6] Checking MQTT service...")
    try:
        from mqtt import MQTTService
        print("   ✓ MQTTService importable")
    except Exception as e:
        warnings.append(f"MQTT service import failed: {e}")
        print(f"   ⚠ Error: {e}")

    print()

    # Check 5: Device Wrapper
    print("[5/6] Checking device wrapper...")
    try:
        from device import ZHADevice
        print("   ✓ ZHADevice importable")

        # Check availability calculation
        if hasattr(ZHADevice, 'is_available'):
            print("   ✓ is_available method present")
        else:
            issues.append("ZHADevice missing is_available method")
            print("   ✗ is_available method NOT present")

    except Exception as e:
        issues.append(f"Failed to import device wrapper: {e}")
        print(f"   ✗ Error: {e}")

    print()

    # Check 6: Configuration
    print("[6/6] Checking configuration...")
    try:
        import yaml
        with open('config.yaml', 'r') as f:
            config = yaml.safe_load(f)

        # Check network key
        if 'zigbee' in config and 'network_key' in config['zigbee']:
            print("   ✓ Network key configured")
        else:
            warnings.append("No network key in config - will auto-generate")
            print("   ⚠ Network key not configured")

        # Check channel
        if 'zigbee' in config and 'channel' in config['zigbee']:
            channel = config['zigbee']['channel']
            if channel in [15, 20, 25]:  # Recommended channels
                print(f"   ✓ Good channel selected: {channel}")
            else:
                warnings.append(f"Channel {channel} may have WiFi interference")
                print(f"   ⚠ Channel {channel} may conflict with WiFi")
        else:
            print("   ⚠ No channel configured - will use default")

        # Check coordinator settings
        if 'zigbee' in config:
            buffer_count = config['zigbee'].get('packet_buffer_count', 0)
            if buffer_count >= 255:
                print(f"   ✓ Packet buffers: {buffer_count}")
            else:
                warnings.append("Low packet buffer count may cause pairing issues")
                print(f"   ⚠ Packet buffers: {buffer_count} (recommended: 255)")

    except FileNotFoundError:
        issues.append("config.yaml not found")
        print("   ✗ config.yaml NOT found")
    except Exception as e:
        warnings.append(f"Config check failed: {e}")
        print(f"   ⚠ Error: {e}")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print()

    if not issues and not warnings:
        print("✓ ALL CHECKS PASSED")
        print()
        print("Your system is ready for Hive device pairing!")
        print()
        print("Next steps:")
        print("1. Start the gateway: python main.py")
        print("2. Enable pairing mode")
        print("3. Pair SLR1c (receiver) first")
        print("4. Pair SLT6 (thermostat) second")
        print("5. Bind SLT6 → SLR1c using the UI")
        return 0
    else:
        if issues:
            print(f"✗ {len(issues)} CRITICAL ISSUE(S) FOUND:")
            for issue in issues:
                print(f"  • {issue}")
            print()

        if warnings:
            print(f"⚠ {len(warnings)} WARNING(S):")
            for warning in warnings:
                print(f"  • {warning}")
            print()

        print("Fix the critical issues before attempting to pair Hive devices.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(check_hive_readiness()))
