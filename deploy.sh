#!/bin/bash
# Zigbee Manager Deployment Script
# This script sets up the Zigbee Manager for production use

set -e

echo "=========================================="
echo "Zigbee Manager Deployment Script"
echo "=========================================="
echo

# Configuration
INSTALL_DIR="/opt/zigbee-manager"
SERVICE_USER="zigbee"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="$INSTALL_DIR/logs"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root"
    echo "Usage: sudo bash deploy.sh"
    exit 1
fi

echo "Step 1: Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false -d "$INSTALL_DIR" "$SERVICE_USER"
    echo "✓ User '$SERVICE_USER' created"
else
    echo "✓ User '$SERVICE_USER' already exists"
fi

echo
echo "Step 2: Adding user to dialout group for USB access..."
usermod -a -G dialout "$SERVICE_USER"
echo "✓ User added to dialout group"

echo
echo "Step 3: Creating directory structure..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$LOG_DIR"
echo "✓ Directories created"

echo
echo "Step 4: Installing system dependencies..."
apt-get update
apt-get install -y python3 python3-venv python3-pip logrotate
echo "✓ System dependencies installed"

echo
echo "Step 5: Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi

echo
echo "Step 6: Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r requirements.txt
echo "✓ Python dependencies installed"

echo
echo "Step 7: Copying application files..."
# Assumes script is run from the project directory
cp -r *.py "$INSTALL_DIR/"
cp -r handlers "$INSTALL_DIR/"
cp -r static "$INSTALL_DIR/"
cp config.yaml "$INSTALL_DIR/"
echo "✓ Application files copied"

echo
echo "Step 8: Setting permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 644 "$INSTALL_DIR/config.yaml"
chmod 755 "$LOG_DIR"
echo "✓ Permissions set"

echo
echo "Step 9: Installing systemd service..."
if [ -f "zigbee-manager.service" ]; then
    # Update paths in service file
    sed -i "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|g" zigbee-manager.service
    sed -i "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python main.py|g" zigbee-manager.service
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=$LOG_DIR $INSTALL_DIR|g" zigbee-manager.service

    cp zigbee-manager.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable zigbee-manager
    echo "✓ Systemd service installed and enabled"
else
    echo "⚠ Warning: zigbee-manager.service file not found"
fi

echo
echo "Step 10: Installing logrotate configuration..."
if [ -f "zigbee-logrotate.conf" ]; then
    # Update paths in logrotate config
    sed -i "s|/path/to/your/project/logs|$LOG_DIR|g" zigbee-logrotate.conf

    cp zigbee-logrotate.conf /etc/logrotate.d/zigbee-manager
    chmod 644 /etc/logrotate.d/zigbee-manager
    echo "✓ Logrotate configuration installed"

    # Test logrotate config
    echo "  Testing logrotate configuration..."
    if logrotate -d /etc/logrotate.d/zigbee-manager >/dev/null 2>&1; then
        echo "  ✓ Logrotate configuration is valid"
    else
        echo "  ⚠ Warning: Logrotate configuration test failed"
    fi
else
    echo "⚠ Warning: zigbee-logrotate.conf file not found"
fi

echo
echo "Step 11: Configuring firewall (if UFW is active)..."
if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 8000/tcp comment "Zigbee Manager Web Interface"
    echo "✓ Firewall rule added for port 8000"
else
    echo "  UFW not active, skipping firewall configuration"
fi

echo
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo
echo "Next steps:"
echo "1. Edit configuration: sudo nano $INSTALL_DIR/config/config.yaml"
echo "2. Update MQTT settings, Zigbee USB port, etc."
echo "3. Start the service: sudo systemctl start zigbee-manager"
echo "4. Check status: sudo systemctl status zigbee-manager"
echo "5. View logs: sudo journalctl -u zigbee-manager -f"
echo "6. Access web interface: http://YOUR_IP:8000"
echo
echo "Useful commands:"
echo "- Restart service: sudo systemctl restart zigbee-manager"
echo "- Stop service: sudo systemctl stop zigbee-manager"
echo "- View application logs: sudo tail -f $LOG_DIR/zigbee.log"
echo "- View debug logs: sudo tail -f $LOG_DIR/zigbee_debug.log"
echo "- Test logrotate: sudo logrotate -f /etc/logrotate.d/zigbee-manager"
echo
echo "For debugging guide, see: DEBUGGING_GUIDE.md"
echo
