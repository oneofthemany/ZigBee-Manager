#!/usr/bin/env python3
"""
NCP Configuration Inspector
Connects to the Silicon Labs (EZSP) adapter and reads active configuration values.
Useful for verifying if buffer settings (CONFIG_PACKET_BUFFER_COUNT) are actually applied.
"""
import asyncio
import logging
import sys
import os
import yaml
from bellows.ezsp import EZSP
from bellows.zigbee.application import ControllerApplication
import zigpy.config

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("check_ncp")

# Load settings from config.yaml to ensure we match the main app
def load_config():
    if os.path.exists("config.yaml"):
        with open("config.yaml", 'r') as f:
            return yaml.safe_load(f)
    return {}

async def check_config():
    user_conf = load_config().get('zigbee', {})
    
    # Default settings matching main.py
    device_config = {
        "path": user_conf.get('port', '/dev/ttyACM0'),
        "baudrate": user_conf.get('baudrate', 460800),
        "flow_control": user_conf.get('flow_control', 'hardware')
    }

    print(f"Connecting to {device_config['path']} @ {device_config['baudrate']}...")

    # Initialize EZSP
    ezsp = EZSP(device_config)
    
    try:
        await ezsp.connect()
        print("✓ Connected to NCP")
        
        # Get Version Info
        ver = await ezsp.version()
        print(f"  - Protocol Version: {ver}")
        
        # Get Stack Version
        # EZSP_VALUE_VERSION_INFO = 0x11
        # Returns: [build, major, minor, patch, special, type]
        ver_info = await ezsp.getValue(0x11) 
        # Handle difference in return types between bellows versions
        if isinstance(ver_info, tuple):
            # Often (status, value_data)
            if len(ver_info) > 1:
                v = ver_info[1]
                print(f"  - Firmware Version: {v[1]}.{v[2]}.{v[3]} build {v[0]}")
        
        print("-" * 60)
        print("CURRENT CONFIGURATION (Active on Dongle)")
        print("-" * 60)

        # Dictionary of EZSP Config IDs (EZSP Protocol Reference)
        # These allow us to see what is actually allocated in the stick's RAM
        configs = {
            0x01: "packet_buffer_count",
            0x02: "neighbor_table_size",
            0x03: "aps_unicast_message_count",
            0x04: "binding_table_size",
            0x05: "address_table_size",
            0x06: "multicast_table_size",
            0x07: "route_table_size",
            0x09: "stack_profile",
            0x12: "security_level",
            0x26: "source_route_table_size",
            0x29: "indirect_transmission_timeout",
        }

        for conf_id, name in configs.items():
            try:
                # getConfigurationValue returns (status, value)
                status, value = await ezsp.getConfigurationValue(conf_id)
                if status == 0: # EZSP_SUCCESS
                    print(f"{name:<30} : {value}")
                else:
                    print(f"{name:<30} : ERROR (0x{status:02X})")
            except Exception as e:
                print(f"{name:<30} : Failed ({e})")

        print("-" * 60)
        print("ANALYSIS")
        print("-" * 60)
        
        # Verify buffers
        status, buffers = await ezsp.getConfigurationValue(0x01)
        if status == 0:
            if buffers < 255:
                print(f"⚠️  WARNING: Buffer count is LOW ({buffers}).")
                print("   Recommendation: Increase 'packet_buffer_count' in core.py to 512.")
            elif buffers >= 512:
                 print(f"✅ Buffer count is excellent ({buffers}).")
                 print("   This adapter is configured for high traffic.")
            else:
                 print(f"ℹ️  Buffer count is standard ({buffers}).")
        
        # Check Source Routing (Critical for 50+ devices)
        status, src_routes = await ezsp.getConfigurationValue(0x26)
        if status == 0 and src_routes < 16:
             print(f"⚠️  WARNING: Source Route Table is SMALL ({src_routes}).")
             print("   For 57 devices, you need source routing.")
             print("   Recommendation: Increase 'source_route_table_size' in core.py to 32 or 64.")
        elif status == 0:
             print(f"✅ Source Route Table is good ({src_routes}).")

    except Exception as e:
        print(f"\n❌ Connection Failed: {e}")
        print("Ensure the main application is STOPPED before running this tool.")
        print("Zigbee adapters can only accept one connection at a time.")
    finally:
        try:
            ezsp.close()
        except:
            pass

if __name__ == "__main__":
    # Suppress noisy libs
    logging.getLogger("bellows").setLevel(logging.CRITICAL)
    logging.getLogger("zigpy").setLevel(logging.CRITICAL)
    asyncio.run(check_config())
