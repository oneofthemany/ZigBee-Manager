#!/usr/bin/env python3
"""
MQTT Discovery Monitor

This script listens to MQTT and shows what discovery configs are currently
published for your devices. This tells us what Home Assistant sees.
"""
import asyncio
import yaml
import json
from pathlib import Path
import sys
import signal

sys.path.insert(0, '/opt/zigbee_manager')

try:
    from aiomqtt import Client
except ImportError:
    print("❌ Error: aiomqtt not installed")
    print("   Install with: pip install aiomqtt")
    sys.exit(1)


def load_config():
    """Load configuration."""
    config_file = Path('/opt/zigbee_manager/config.yaml')
    if not config_file.exists():
        print("❌ Error: config.yaml not found!")
        sys.exit(1)
    
    with open(config_file, 'r') as f:
        return yaml.safe_load(f) or {}


class MQTTMonitor:
    def __init__(self):
        self.discoveries = {}
        self.running = True
        
    async def monitor(self):
        """Monitor MQTT for discovery messages."""
        config = load_config()
        mqtt_config = config.get('mqtt', {})
        
        broker = mqtt_config.get('broker_host', 'localhost')
        port = mqtt_config.get('broker_port', 1883)
        username = mqtt_config.get('username')
        password = mqtt_config.get('password')
        
        print("="*70)
        print("MQTT DISCOVERY MONITOR")
        print("="*70)
        print()
        print(f"Connecting to MQTT: {broker}:{port}")
        if username:
            print(f"Using authentication: {username}")
        print("Listening for discovery messages...")
        print("Press Ctrl+C to stop and see results")
        print()
        
        try:
            # Connect with authentication if provided
            client_args = {'hostname': broker, 'port': port}
            if username and password:
                client_args['username'] = username
                client_args['password'] = password
            
            async with Client(**client_args) as client:
                # Subscribe to all Home Assistant discovery messages
                await client.subscribe("homeassistant/#")
                
                async for message in client.messages:
                    if not self.running:
                        break
                        
                    topic = str(message.topic)
                    payload = message.payload.decode() if message.payload else ""
                    
                    # Only care about config topics
                    if not topic.endswith('/config'):
                        continue
                    
                    # Parse topic: homeassistant/{component}/{node_id}/{object_id}/config
                    parts = topic.split('/')
                    if len(parts) >= 4:
                        component = parts[1]
                        node_id = parts[2]
                        object_id = parts[3]
                        
                        # Only show lights and switches
                        if component not in ['light', 'switch']:
                            continue
                        
                        key = f"{node_id}:{object_id}"
                        
                        if payload:
                            try:
                                config_data = json.loads(payload)
                                device_name = config_data.get('device', {}).get('name', 'Unknown')
                                self.discoveries[key] = {
                                    'component': component,
                                    'node_id': node_id,
                                    'object_id': object_id,
                                    'device_name': device_name,
                                    'has_brightness': 'brightness_command_topic' in config_data,
                                    'has_color': 'color_mode' in config_data
                                }
                            except json.JSONDecodeError:
                                pass
                
        except Exception as e:
            print(f"❌ Error: {e}")
            sys.exit(1)
    
    def show_results(self):
        """Show discovered devices."""
        print("\n")
        print("="*70)
        print("DISCOVERED DEVICES")
        print("="*70)
        print()
        
        if not self.discoveries:
            print("❌ No light/switch discoveries found")
            print("\nPossible reasons:")
            print("  • Devices haven't been announced yet")
            print("  • Wrong MQTT broker/topic")
            print("  • Service not running")
            return
        
        # Group by device
        by_device = {}
        for key, info in self.discoveries.items():
            device = info['device_name']
            if device not in by_device:
                by_device[device] = []
            by_device[device].append(info)
        
        lights = []
        switches = []
        
        for device_name in sorted(by_device.keys()):
            print(f"📱 {device_name}")
            
            for info in by_device[device_name]:
                component = info['component']
                object_id = info['object_id']
                has_brightness = info['has_brightness']
                has_color = info['has_color']
                
                status_icon = "✅" if component == 'light' else "⚠️"
                
                print(f"  {status_icon} {component.upper()}: {object_id}")
                if component == 'light':
                    features = []
                    if has_brightness:
                        features.append("brightness")
                    if has_color:
                        features.append("color")
                    if features:
                        print(f"     Features: {', '.join(features)}")
                    lights.append(device_name)
                else:
                    switches.append(device_name)
            
            print()
        
        print("="*70)
        print("SUMMARY")
        print("="*70)
        print(f"Total devices: {len(by_device)}")
        print(f"  • {len(set(lights))} lights")
        print(f"  • {len(set(switches))} switches")
        
        # Check for bulbs that are switches
        bulb_keywords = ['bulb', 'light', 'tradfri', 'hue', 'philips', 'aqara']
        wrong_switches = []
        
        for device_name in set(switches):
            if any(kw.lower() in device_name.lower() for kw in bulb_keywords):
                wrong_switches.append(device_name)
        
        if wrong_switches:
            print()
            print("⚠️  POTENTIAL ISSUES:")
            print("   These devices look like lights but are configured as switches:")
            for name in wrong_switches:
                print(f"     • {name}")
            print()
            print("   ACTION: Deploy the updated switches.py and restart service")
        else:
            print()
            print("✅ All bulbs appear to be correctly configured as lights!")


async def main():
    monitor = MQTTMonitor()
    
    def signal_handler(sig, frame):
        print("\n\nStopping monitor...")
        monitor.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        await asyncio.wait_for(monitor.monitor(), timeout=10.0)
    except asyncio.TimeoutError:
        pass
    
    monitor.show_results()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
