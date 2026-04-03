#!/bin/bash
# Clear Home Assistant MQTT Discovery - CORRECTED VERSION
# This properly clears all discovery configs by finding topics first

echo "Clearing Home Assistant MQTT discovery configs..."
echo ""

# Get broker from config
BROKER=$(grep broker_host ./config/config.yaml | awk '{print $2}' | tr -d '"' | tr -d "'")

if [ -z "$BROKER" ]; then
    read -p "Enter MQTT broker address: " BROKER
fi

echo "Broker: $BROKER"
echo ""
read -p "Clear all Zigbee discovery configs? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Finding discovery topics..."

# Method 1: Get your gateway's node IDs from MQTT
# Subscribe briefly to find topics, then clear them
TOPICS=$(timeout 2s mosquitto_sub -h "$BROKER" -t 'homeassistant/#' -v 2>/dev/null | \
    grep -o 'homeassistant/[^[:space:]]*' | sort -u)

if [ -z "$TOPICS" ]; then
    echo "⚠ No topics found via subscription. Trying alternative method..."
    
    # Method 2: Manual clearing based on common patterns
    # Clear topics for each component type
    COMPONENTS=("light" "switch" "sensor" "binary_sensor" "cover" "climate" "number")
    
    echo "Clearing by component type..."
    for component in "${COMPONENTS[@]}"; do
        # Try to get topics for this component
        COMP_TOPICS=$(timeout 1s mosquitto_sub -h "$BROKER" -t "homeassistant/${component}/+/+/config" -v 2>/dev/null | \
            grep -o "homeassistant/${component}/[^[:space:]]*" | sort -u)
        
        if [ ! -z "$COMP_TOPICS" ]; then
            echo "  Clearing ${component} entities..."
            echo "$COMP_TOPICS" | while read -r topic; do
                mosquitto_pub -h "$BROKER" -t "$topic" -n -r >/dev/null 2>&1
            done
        fi
    done
    
    echo ""
    echo "✓ Cleared discovery by component type"
else
    # Clear each found topic
    TOPIC_COUNT=$(echo "$TOPICS" | wc -l)
    echo "Found $TOPIC_COUNT discovery topics"
    echo ""
    
    CLEARED=0
    echo "$TOPICS" | while read -r topic; do
        if [[ "$topic" =~ config$ ]]; then
            mosquitto_pub -h "$BROKER" -t "$topic" -n -r >/dev/null 2>&1
            ((CLEARED++))
            if [ $((CLEARED % 10)) -eq 0 ]; then
                echo "  Cleared $CLEARED topics..."
            fi
        fi
    done
    
    echo ""
    echo "✓ Cleared $CLEARED discovery configs"
fi

echo ""
echo "=========================================="
echo "NEXT STEPS:"
echo "=========================================="
echo ""
echo "1. Restart Zigbee Manager:"
echo "   sudo systemctl restart zigbee_manager"
echo ""
echo "2. Wait 30 seconds for devices to re-announce"
echo ""
echo "3. Check gateway logs:"
echo "   sudo journalctl -u zigbee-gateway -f"
echo "   (Look for 'Published HA discovery' messages)"
echo ""
echo "4. Restart Home Assistant"
echo ""
echo "5. Verify in HA logs (should be NO template warnings):"
echo "   tail -f /config/home-assistant.log | grep -i template"
echo ""

sudo systemctl restart zigbee_manager.service
