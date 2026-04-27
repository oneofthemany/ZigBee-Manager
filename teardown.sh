#!/bin/bash
set -e

podman rm -f zigbee-matter-manager 2>/dev/null || true

export branch="main"
export device="/dev/ttyUSB0"

podman images -q "localhost/zigbee-matter-manager" | xargs -r podman rmi -f

rm -rf /opt/zigbee-matter-manager/ /opt/.zigbee-matter-manager/


curl -fsSL https://raw.githubusercontent.com/oneofthemany/ZigBee-Matter-Manager/${branch}/build.sh | bash -s -- --usb ${device}