#!/bin/bash
# Installs (or updates) LoRa APRS Pi on Raspberry Pi OS Bookworm or newer.
#   sudo ./install.sh
# Your settings in /etc/lora-aprs-pi are kept when you run it again.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/lora-aprs-pi
CONF=/etc/lora-aprs-pi
REBOOT=no

step() { echo; echo "==> $*"; }

step "Installing the packages it needs"
apt-get update -qq
apt-get install -y -qq python3 python3-spidev python3-lgpio gcc make iw >/dev/null

step "Turning on SPI for the LoRa HAT"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_spi 0
else
  BOOTCFG=/boot/firmware/config.txt
  [ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
  grep -q '^dtparam=spi=on' "$BOOTCFG" || echo 'dtparam=spi=on' >> "$BOOTCFG"
fi
[ -e /dev/spidev0.0 ] || REBOOT=yes

step "Copying the program to $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/lora_aprs_pi"
cp -r "$SRC/lora_aprs_pi" "$SRC/lacf" "$SRC/Makefile" "$SRC/README.md" "$SRC/LICENSE" "$DEST/"
find "$DEST" -name '__pycache__' -prune -exec rm -rf {} +

step "Building the LACF codec"
make -s -C "$DEST" clean all
(cd "$DEST" && python3 -c "from lora_aprs_pi import lacf; assert lacf.available(), lacf.load_error()")

step "Settings folder"
mkdir -p "$CONF"
chmod 700 "$CONF"
[ -f "$CONF/config.json" ] && echo "Keeping your settings in $CONF/config.json"

if command -v nmcli >/dev/null; then
  # Raspberry Pi OS keeps WiFi switched off until a WiFi country is set.
  if command -v raspi-config >/dev/null && [ -z "$(raspi-config nonint get_wifi_country 2>/dev/null)" ]; then
    COUNTRY=${WIFI_COUNTRY:-US}
    step "Setting the WiFi country to $COUNTRY (run with WIFI_COUNTRY=xx to choose another)"
    raspi-config nonint do_wifi_country "$COUNTRY"
  fi
  nmcli radio wifi on || true

  step "Hotspot sign-in page"
  # While the Pi runs its hotspot, every web address leads to the setup page,
  # so a phone that joins the hotspot pops the page up by itself.
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  echo 'address=/#/10.42.0.1' > /etc/NetworkManager/dnsmasq-shared.d/lora-aprs-pi.conf
else
  echo
  echo "NetworkManager isn't running, so the WiFi page and hotspot fallback are off."
  echo "Raspberry Pi OS Bookworm and newer use NetworkManager by default."
fi

step "Starting the service"
cp "$SRC/systemd/lora-aprs-pi.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable lora-aprs-pi >/dev/null
systemctl restart lora-aprs-pi

HOST=$(hostname)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Done."
[ -n "$IP" ] || IP="<this Pi's address>"
echo "Open  http://$HOST.local  or  http://$IP  on a phone or computer"
echo "on the same network, set your callsign, and save."
echo "If the Pi can't join WiFi it starts the hotspot \"LoRa-APRS-Pi\" (password loraaprs);"
echo "join it and the setup page opens."
if [ "$REBOOT" = yes ]; then
  echo
  echo "SPI was just turned on: reboot once (sudo reboot) before the radio will work."
fi
