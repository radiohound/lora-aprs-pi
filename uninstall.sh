#!/bin/bash
# Removes LoRa APRS Pi.  Your settings are kept unless you add --purge.
#   sudo ./uninstall.sh [--purge]
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./uninstall.sh"; exit 1; }

systemctl disable --now lora-aprs-pi 2>/dev/null || true
rm -f /etc/systemd/system/lora-aprs-pi.service
systemctl daemon-reload
rm -rf /opt/lora-aprs-pi
rm -f /etc/NetworkManager/dnsmasq-shared.d/lora-aprs-pi.conf
command -v nmcli >/dev/null && nmcli connection delete lora-aprs-pi-hotspot 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf /etc/lora-aprs-pi
  echo "Removed, including your settings."
else
  echo "Removed. Your settings are still in /etc/lora-aprs-pi (use --purge to delete them)."
fi
