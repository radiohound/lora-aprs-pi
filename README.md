# LoRa APRS Pi

A LoRa APRS station for a Raspberry Pi with a LoRa HAT, set up from your phone.

- Receives standard LoRa APRS packets and the newer, smaller
  [LACF](https://github.com/radiohound/lora-aprs-ideas) frames, and lists
  everything it hears with signal strength, distance and bearing.
- Sends a position beacon in standard format, LACF, or alternating between the two.
- Works as an iGate: passes what it hears to APRS-IS, LACF packets included, so
  LACF stations show up on aprs.fi like any other.
- Works as a digipeater: fill-in (WIDE1-1) or wide area (WIDE1-1 and WIDE2-N).
  LACF packets are repeated as LACF with this station's signal report added.
- Everything is set from a web page that works on a phone, with no internet needed.
- If the Pi can't join WiFi, it starts its own hotspot. Join it from your phone
  and the setup page opens by itself.

This version is a beacon station, receiver, receive-only iGate and digipeater.
Gating messages from APRS-IS back to RF is planned next.

## What you need

- A Raspberry Pi with WiFi: Zero 2 W, 3, 4 or 5
- Raspberry Pi OS Bookworm or newer (Lite is fine)
- A [MeshAdv-Pi HAT](https://github.com/chrismyers2000/MeshAdv-Pi-Hat) with an
  Ebyte E22 module, or another SX1262 / SX1268 board whose pins you can enter
- An antenna on the HAT **before** you transmit
- An amateur radio licence: the station won't transmit until you enter your callsign

## Install

On the Pi:

```bash
git clone https://github.com/radiohound/lora-aprs-pi.git
cd lora-aprs-pi
sudo ./install.sh
```

The installer adds the few packages it needs (all from Raspberry Pi OS, no pip),
turns on SPI, builds the LACF codec, and starts the station as a service. If it
says SPI was just turned on, reboot once.

To update later: `git pull` then `sudo ./install.sh` again. Your settings are kept.

## First setup from a phone

1. If the Pi is already on your WiFi, open `http://<pi-name>.local` on the phone
   (the installer prints the exact address).
2. If not, wait two minutes after power-up, then join the WiFi network
   **LoRa-APRS-Pi** (password `loraaprs`). The setup page should pop up; if it
   doesn't, open `http://10.42.0.1`.
3. On **Settings**: enter your callsign, choose your board, set your position,
   and save.
4. On **WiFi**: pick your home network to put the Pi on it, and change the
   hotspot password.
5. Set a **settings password** if others share your network.

## The page

| Tab | What it shows |
|---|---|
| Status | Whether the radio is listening, the radio settings, power, GPS, and the next beacon. **Send beacon now** sends one straight away. |
| Heard | Every packet heard, newest first: callsign, standard or LACF, signal, distance and bearing, and the raw packet. Your own beacons are marked Sent. |
| Settings | Callsign, beacon, radio, GPS and the settings password. Changes apply as soon as you save. |
| WiFi | Join a network, or change and switch to the hotspot. |

The bar under your callsign is the station's lamp: green while listening, amber
just after transmitting, red when the radio has a problem (with the reason).

## Beacon formats

- **Standard**: `3C FF 01` + text. Every LoRa APRS station can hear it.
- **LACF**: `3C FF 02` + the compact frame, typically about 40% fewer bytes,
  so less time on air. Only stations that understand LACF can hear it.
- **Alternate**: one of each in turn, so everyone hears you and LACF stations
  get the shorter frame.

Beacons use the plain (uncompressed) position format, which LACF carries in its
lossless position block.

## iGate (APRS-IS)

Turn on **Send what this station hears to APRS-IS** under Settings → iGate.
Gating to APRS-IS is for licensed amateurs.

- **Passcode:** leave it empty and the station works it out from its own
  callsign, the same way other APRS software does. Enter one only if you want a
  different one, or -1 for a listen-only login. The Status tab says whether the
  server accepted the login (verified) or not.
- **What gets gated:** every packet heard, except those with NOGATE, RFONLY,
  TCPIP or TCPXX in the path, general queries (`?APRS?`), the station's own
  packets, and repeats of the same packet within 30 seconds. Third-party packets
  are unwrapped and the same rules applied to the packet inside. The Heard tab
  marks each packet **Gated**, or says why it wasn't.
- **LACF:** LACF frames are decoded back to ordinary APRS text before upload.
- **Uploads are tagged `qAO`** (a receive-only iGate), since this version doesn't
  send APRS-IS traffic back over the radio.
- **Its own position** also goes to APRS-IS directly at the beacon interval
  (at least every 10 minutes apart), without using any air time, whether or not
  the radio beacon is on.

## Digipeater

Choose under Settings → Digipeater:

| Mode | Repeats |
|---|---|
| Off | nothing |
| Fill-in | `WIDE1-1`, and packets addressed to this station's callsign |
| Wide area | also `WIDE2-1` and `WIDE2-2` |

- The first hop not yet used decides, the same rule spec 11 uses for LACF:
  `WIDE1-1` and `WIDE2-1` become `MYCALL*`, `WIDE2-2` becomes `MYCALL*,WIDE2-1`.
- Never repeated: our own packets, a packet already repeated in the last 30
  seconds, one this station has already repeated (seen in its path), and any
  path that breaks the WIDEn-N rule. Bigger floods (`WIDE3`+) are not served.
- `NOGATE` and `RFONLY` packets **are** repeated: those words mean "keep it on
  RF", not "don't repeat".
- A repeat waits a random 0.5 to 2 seconds, so digipeaters that heard the same
  packet don't all transmit at once.
- **LACF packets** are repeated as LACF by copying their content and adding this
  station's RXT tuple: signal strength, SNR, frequency offset and time to hop.
  Standard packets are repeated as standard text.

## Hearing your beacon repeated

When a digipeater repeats this station's beacon and the station hears the
repeat, the Heard tab marks it **Your beacon, via** the digipeater. If the
repeat is LACF, it also shows the digipeater's report of how well it heard
you (RSSI and SNR), next to how well you heard its repeat.

## Auto reduced power

Settings → Beacon → **Auto reduced power** lowers the beacon's power while a
digipeater reports hearing you with plenty to spare, saving battery and
channel. It works from the digipeater's LACF report, so it needs LACF (or
alternate) beacons, a path such as `WIDE1-1`, and an LACF digipeater in range.

- The margin is the reported SNR above what SF12 (or the set SF) needs, or for
  strong signals the RSSI above the receiver's sensitivity.
- 13 dB or more to spare: the next beacon goes 2 dB lower. 10 to 13 dB: unchanged.
  Under 10 dB: back up by the shortfall plus 2 dB.
- No report for two LACF beacons in a row: straight back to full power.
- Never above the power set under Radio, never below 10 dBm at the antenna.
  Digipeated packets always go out at the set power.

## Transmit power

The page asks for the power you want **at the antenna**. E22 modules have an
amplifier after the radio chip, so the station sets the chip lower by the
amplifier's gain, and never drives the chip above what the module can take:

| Board | Amplifier | Chip limit | About at the antenna |
|---|---|---|---|
| MeshAdv-Pi, E22 33 dBm (E22-400M33S / 900M33S) | ~25 dB | 8 dBm | up to 33 dBm (2 W) |
| MeshAdv-Pi, E22 30 dBm (E22-400M30S / 900M30S) | ~8 dB | 22 dBm | up to 30 dBm (1 W) |

The default board is the 33 dBm one, because it is the safe guess: a 30 dBm
module set up as a 33 dBm one just runs at low power. The gains are
approximate; measure your output if it matters.

## Radio settings

The defaults are the LoRa APRS standard used in North America and Europe:
433.775 MHz, SF12, 125 kHz, coding rate 4/5, sync word 0x12, CRC on. Every
station you want to hear must use the same frequency, SF, bandwidth and coding
rate.

## Trying it without a Pi

On any computer with Python 3 and a C compiler:

```bash
make            # builds the LACF codec
make test       # runs all the tests
make run        # a pretend radio at http://localhost:8080
```

## Files

| Path | What it is |
|---|---|
| `lora_aprs_pi/sx126x.py` | SX1262 / SX1268 radio driver (spidev + lgpio) |
| `lora_aprs_pi/hardware.py` | Board pin profiles and the power limits |
| `lora_aprs_pi/frames.py` | Standard and LACF frames |
| `lora_aprs_pi/lacf.py` | Python wrapper for the LACF C codec |
| `lora_aprs_pi/aprs.py` | Building beacons, reading received packets |
| `lora_aprs_pi/station.py` | The station: radio loop, heard list, beacons |
| `lora_aprs_pi/igate.py` | APRS-IS connection, passcode, gating rules |
| `lora_aprs_pi/digi.py` | Digipeater path rules and RXT signal reports |
| `lora_aprs_pi/web.py`, `static/` | The web page and its API |
| `lora_aprs_pi/wifi.py` | WiFi and the hotspot, through NetworkManager |
| `lora_aprs_pi/gps.py` | Optional NMEA GPS on a serial port |
| `lacf/` | The LACF reference codec, copied from lora-aprs-ideas |
| `/etc/lora-aprs-pi/config.json` | Your settings, on the Pi |

Logs: `journalctl -u lora-aprs-pi -f`

## Troubleshooting

- **"The radio did not answer over SPI"**: SPI isn't on (reboot after
  installing), the HAT isn't seated, or the wrong board is selected.
- **"GPIO nn is in use by another program"**: something else is using the HAT,
  such as meshtasticd or Dire Wolf with LoRa. Stop it first.
- **No hotspot appears**: WiFi needs a country set
  (`sudo raspi-config` → Localisation → WLAN country), and NetworkManager, which
  Raspberry Pi OS Bookworm uses by default.
- **Nothing heard**: check the antenna, and that the frequency, SF, bandwidth and
  coding rate match the stations around you.

## Licence

MIT. The LACF codec in `lacf/` is from
[radiohound/lora-aprs-ideas](https://github.com/radiohound/lora-aprs-ideas), also MIT.
