#!/bin/bash
set -e

podman rm -f zigbee-matter-manager 2>/dev/null || true

image=$(podman images | grep "localhost/zigbee-matter-manager" | awk '{print $3}')

if [[ -n "$image" ]]; then
    podman rmi -f "$image"
else
    echo "No existing image found, skipping rmi."
fi

rm -rf zigbee-matter-manager/ .zigbee-matter-manager/

rm -f ~/.config/systemd/user/container-zigbee-matter-manager.service

systemctl --user daemon-reload

curl -fsSL https://raw.githubusercontent.com/oneofthemany/ZigBee-Matter-Manager/main/build.sh | bash -s -- --usb /dev/ttyUSB0