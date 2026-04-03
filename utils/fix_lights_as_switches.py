#!/usr/bin/env python3
"""
Clear Old MQTT Switch Configs and Force Republish

This script:
1. Deletes all old switch configs from MQTT (retained messages)
2. Triggers your service to republish with new detection logic
"""
import asyncio
import yaml
from pathlib import Path
import sys
import json

sys.path.insert(0, '/opt/zigbee_manager')

try:
    from aiomqtt import Client
except ImportError:
    print("❌ Error: aiomqtt not installed")
    sys.exit(1)


def load_config():
    """Load configuration."""
    config_file = Path('/opt/zigbee_manager/config.yaml')
    if not config_file.exists():
        print("❌ Error: config.yaml not found!")
        sys.exit(1)
    
    with open(config_file, 'r') as f:
        return yaml.safe_load(f) or {}


# List of devices that should be lights (based on your monitor output)
LIGHTS_TO_FIX = [
    'LED - Bathroom Door',
    'LED - Bathroom Sink', 
    'LED - Bathroom Toilet',
    'LED - Ensuite - Shower',
    'LED - Ensuite - Sink',
    'LED - Ensuite - Toilet',
    'Lamp - Living',
    'Pendant - Elena',
    'Pendant - Hallway 1',
    'Pendant - Hallway 2',
    'Pendant - Kitchen 1',
    'Pendant - Kitchen 2',
    'Pendant - Living',
    'Pendant - Master',
]


async def clear_and_republish():
    """Clear old configs and republish."""
    config = load_config()
    mqtt_config = config.get('mqtt', {})
    
    broker = mqtt_config.get('broker_host', 'localhost')
    port = mqtt_config.get('broker_port', 1883)
    username = mqtt_config.get('username')
    password = mqtt_config.get('password')
    
    print("="*70)
    print("CLEAR OLD CONFIGS AND REPUBLISH")
    print("="*70)
    print()
    print(f"Connecting to MQTT: {broker}:{port}")
    if username:
        print(f"Using authentication: {username}")
    print()
    
    try:
        client_args = {'hostname': broker, 'port': port}
        if username and password:
            client_args['username'] = username
            client_args['password'] = password
        
        async with Client(**client_args) as client:
            print("✅ Connected to MQTT broker")
            print()
            
            # Step 1: Subscribe and collect all current configs
            print("Step 1: Discovering current switch configs...")
            await client.subscribe("homeassistant/switch/+/+/config")
            
            configs_to_clear = []
            
            # Collect messages for 3 seconds
            try:
                async with asyncio.timeout(3):
                    async for message in client.messages:
                        topic = str(message.topic)
                        payload = message.payload.decode() if message.payload else ""
                        
                        if payload:
                            try:
                                config_data = json.loads(payload)
                                device_name = config_data.get('device', {}).get('name', '')
                                
                                # Check if this is a light that's misconfigured as switch
                                if any(light_name in device_name for light_name in LIGHTS_TO_FIX):
                                    configs_to_clear.append((topic, device_name))
                                    print(f"   Found: {device_name} ({topic})")
                            except json.JSONDecodeError:
                                pass
            except asyncio.TimeoutError:
                pass
            
            print(f"\n   Total switch configs to clear: {len(configs_to_clear)}")
            print()
            
            # Step 2: Clear them
            if configs_to_clear:
                print("Step 2: Clearing old switch configs...")
                for topic, name in configs_to_clear:
                    print(f"   Clearing: {name}")
                    await client.publish(topic, "", qos=1, retain=True)
                    await asyncio.sleep(0.1)
                print(f"   ✅ Cleared {len(configs_to_clear)} configs")
            else:
                print("Step 2: No configs to clear")
            print()
            
            # Step 3: Trigger republish
            print("Step 3: Triggering service to republish all devices...")
            await client.publish(
                "homeassistant/status",
                payload="online",
                qos=1,
                retain=False
            )
            print("   ✅ Republish triggered")
            print()
            
            print("="*70)
            print("COMPLETE!")
            print("="*70)
            print()
            print("NEXT STEPS:")
            print("1. Wait 10 seconds for service to republish")
            print("2. Restart Home Assistant (Settings → System → Restart)")
            print("3. Check your lights - they should now have dimming controls!")
            print()
            print("To verify:")
            print("  /home/sean/venv/bin/python3 light_discovery.py")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    print()
    print("This will:")
    print("  • Clear old switch configs for lights")
    print("  • Trigger republish with new detection logic")
    print()
    
    response = input("Continue? (y/n): ")
    if response.lower() != 'y':
        print("Cancelled.")
        sys.exit(0)
    
    print()
    asyncio.run(clear_and_republish())


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
