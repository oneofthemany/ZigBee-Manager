#!/usr/bin/env python3
"""
Clear Home Assistant MQTT Discovery - Python Version

This properly clears all discovery configs by connecting to MQTT
and removing retained messages for all your Zigbee devices.
"""

import sys
import time
import json
import re

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("❌ Error: paho-mqtt not installed")
    print("Install with: pip install paho-mqtt")
    sys.exit(1)

# Read broker from config.yaml
def get_broker_config():
    try:
        with open('config.yaml', 'r') as f:
            content = f.read()
        
        # Extract broker config
        broker_match = re.search(r'broker_host:\s*["\']?([^"\'\s]+)["\']?', content)
        port_match = re.search(r'broker_port:\s*(\d+)', content)
        username_match = re.search(r'username:\s*["\']?([^"\'\s]+)["\']?', content)
        password_match = re.search(r'password:\s*["\']?([^"\'\s]+)["\']?', content)
        
        return {
            'broker': broker_match.group(1) if broker_match else '192.168.1.1',
            'port': int(port_match.group(1)) if port_match else 1883,
            'username': username_match.group(1) if username_match else None,
            'password': password_match.group(1) if password_match else None,
        }
    except Exception as e:
        print(f"⚠ Could not read config.yaml: {e}")
        return {'broker': '192.168.1.1', 'port': 1883, 'username': None, 'password': None}


class DiscoveryCleaner:
    def __init__(self, broker, port=1883, username=None, password=None):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.topics = []
        self.connected = False
    
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            print(f"✓ Connected to {self.broker}:{self.port}")
            # Subscribe to all homeassistant discovery topics
            client.subscribe("homeassistant/#")
            print("⏳ Scanning for discovery topics...")
        else:
            print(f"❌ Connection failed with code {rc}")
    
    def on_message(self, client, userdata, msg):
        # Only collect config topics
        if msg.topic.endswith('/config'):
            self.topics.append(msg.topic)
    
    def connect(self):
        """Connect to MQTT broker"""
        self.client = mqtt.Client()
        
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(self.broker, self.port, 60)
            return True
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False
    
    def scan_topics(self, duration=3):
        """Scan for discovery topics"""
        self.client.loop_start()
        
        # Wait for connection and scan
        timeout = time.time() + duration
        while time.time() < timeout:
            time.sleep(0.1)
            if len(self.topics) > 0:
                # Continue scanning but show progress
                if len(self.topics) % 10 == 0:
                    print(f"  Found {len(self.topics)} topics so far...")
        
        self.client.loop_stop()
        
        # Remove duplicates
        self.topics = list(set(self.topics))
        return self.topics
    
    def clear_topics(self):
        """Clear all found topics"""
        if not self.topics:
            print("⚠ No topics found to clear")
            return 0
        
        print(f"\n📋 Found {len(self.topics)} discovery configs")
        print("\n🗑️  Clearing discovery topics...")
        
        cleared = 0
        self.client.loop_start()
        
        for topic in self.topics:
            try:
                # Publish empty retained message to remove
                self.client.publish(topic, payload="", qos=1, retain=True)
                cleared += 1
                
                if cleared % 10 == 0:
                    print(f"  Cleared {cleared}/{len(self.topics)}...")
            except Exception as e:
                print(f"  ❌ Error clearing {topic}: {e}")
        
        # Wait for messages to be sent
        time.sleep(1)
        self.client.loop_stop()
        
        return cleared
    
    def disconnect(self):
        """Disconnect from broker"""
        if self.client:
            self.client.disconnect()


def main():
    print("=" * 70)
    print("Clear Home Assistant MQTT Discovery")
    print("=" * 70)
    print()
    
    # Get config
    config = get_broker_config()
    print(f"Broker: {config['broker']}:{config['port']}")
    print()
    
    # Confirm
    confirm = input("Clear all Zigbee discovery configs? (yes/no): ")
    if confirm.lower() != "yes":
        print("Aborted.")
        return
    
    print()
    
    # Create cleaner
    cleaner = DiscoveryCleaner(
        config['broker'],
        config['port'],
        config['username'],
        config['password']
    )
    
    # Connect
    if not cleaner.connect():
        return
    
    # Scan for topics
    topics = cleaner.scan_topics(duration=3)
    
    if not topics:
        print()
        print("⚠ No discovery topics found!")
        print("This could mean:")
        print("  - No devices have been announced yet")
        print("  - Discovery topics were already cleared")
        print("  - MQTT credentials are incorrect")
        cleaner.disconnect()
        return
    
    # Clear topics
    cleared = cleaner.clear_topics()
    cleaner.disconnect()
    
    print()
    print("=" * 70)
    print(f"✓ CLEARED {cleared} DISCOVERY CONFIGS")
    print("=" * 70)
    print()
    print("NEXT STEPS:")
    print()
    print("1. Restart Zigbee Gateway:")
    print("   sudo systemctl restart zigbee-gateway")
    print()
    print("2. Wait 30 seconds for devices to re-announce")
    print()
    print("3. Monitor gateway logs:")
    print("   sudo journalctl -u zigbee-gateway -f | grep 'Published HA discovery'")
    print()
    print("4. Restart Home Assistant")
    print()
    print("5. Verify fix - check HA logs for template warnings:")
    print("   tail -f /config/home-assistant.log | grep -i template")
    print()
    print("   Expected: NO warnings! Devices stay 'available'")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
