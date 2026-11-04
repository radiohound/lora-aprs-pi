"""Station settings: defaults, checking, and saving to a JSON file."""

import copy
import json
import os
import re

from . import hardware

DEFAULT_PATH = "/etc/lora-aprs-pi/config.json"

DEFAULTS = {
    "station": {
        "callsign": "N0CALL",
    },
    "beacon": {
        "enabled": False,
        "interval_min": 30,
        "latitude": None,
        "longitude": None,
        "altitude_ft": None,
        "use_gps": False,
        "symbol": "/-",
        "comment": "LoRa APRS Pi",
        "path": "WIDE1-1",
        "format": "standard",         # standard | lacf | alternate
    },
    "radio": {
        "profile": hardware.DEFAULT_PROFILE,
        "freq_mhz": 433.775,
        "sf": 12,
        "bw_khz": 125,
        "cr": 5,
        "power_dbm": 20,              # at the antenna, approximate
        "custom": {k: hardware.PROFILES["custom"][k] for k in hardware.CUSTOM_KEYS},
    },
    "digi": {
        "mode": "off",                # off | fill-in | wide
    },
    "igate": {
        "enabled": False,
        "server": "rotate.aprs2.net",
        "port": 14580,
        "passcode": "",               # empty = worked out from the callsign
    },
    "gps": {
        "device": "",                 # e.g. /dev/ttyACM0 or /dev/serial0; empty = no GPS
        "baud": 9600,
    },
    "web": {
        "password": "",               # empty = anyone on the network can change settings
    },
    "wifi": {
        "hotspot_ssid": "LoRa-APRS-Pi",
        "hotspot_password": "loraaprs",
    },
}

# Fixed for LoRa APRS; not offered in the web page.
SYNC_WORD = 0x12
PREAMBLE = 8

SYMBOLS = {
    "/-": "House", "/>": "Car", "/[": "Person / hiker", "/O": "Balloon",
    "/#": "Digipeater", "L&": "iGate (LoRa)", "/y": "Yagi antenna at house",
    "/b": "Bicycle", "/k": "Truck", "/;": "Campground", "/r": "Antenna", "/_": "Weather station",
}

CALL_RE = re.compile(r"^[A-Z0-9]{3,6}(-[1-9][0-9]?)?$")
PATH_RE = re.compile(r"^$|^[A-Z0-9]{1,6}(-[0-9]{1,2})?(,[A-Z0-9]{1,6}(-[0-9]{1,2})?){0,7}$")


def _wide_ok(path):
    """WIDEn-N must have 1 <= N <= n <= 7 (the APRS rule spec 11 enforces)."""
    for hop in filter(None, path.split(",")):
        m = re.match(r"^WIDE(\d)-(\d+)$", hop)
        if m and not 1 <= int(m.group(2)) <= int(m.group(1)) <= 7:
            return False
    return True


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        elif key in base:
            base[key] = value
    return base


def load(path):
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path) as f:
            _merge(cfg, json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def save(path, cfg):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)          # it holds the WiFi and settings passwords
    os.replace(tmp, path)


def update(cfg, changes):
    """A copy of cfg with the web page's changes merged in, and a dict of problems."""
    new = _merge(copy.deepcopy(cfg), changes)
    return new, check(new)


def check(cfg):
    """Problems with the settings, as {field: message}. Empty when all is well."""
    errs = {}
    st, bc, rd, gp, wf = cfg["station"], cfg["beacon"], cfg["radio"], cfg["gps"], cfg["wifi"]

    st["callsign"] = str(st["callsign"]).strip().upper()
    if not CALL_RE.match(st["callsign"]):
        errs["station.callsign"] = "Callsign like K6ATV or K6ATV-12"

    try:
        bc["interval_min"] = int(bc["interval_min"])
        if not 1 <= bc["interval_min"] <= 1440:
            raise ValueError
    except (TypeError, ValueError):
        errs["beacon.interval_min"] = "Minutes between beacons, 1 to 1440"

    for key, limit in (("latitude", 90), ("longitude", 180)):
        val = bc[key]
        if val in ("", None):
            bc[key] = None
            continue
        try:
            bc[key] = round(float(val), 6)
            if not -limit <= bc[key] <= limit:
                raise ValueError
        except (TypeError, ValueError):
            errs["beacon." + key] = f"Decimal degrees, -{limit} to {limit}"

    if bc["altitude_ft"] in ("", None):
        bc["altitude_ft"] = None
    else:
        try:
            bc["altitude_ft"] = int(float(bc["altitude_ft"]))
        except (TypeError, ValueError):
            errs["beacon.altitude_ft"] = "Feet, or leave empty"

    bc["path"] = str(bc["path"]).strip().upper().replace(" ", "")
    if not PATH_RE.match(bc["path"]) or not _wide_ok(bc["path"]):
        errs["beacon.path"] = "Like WIDE1-1 or WIDE1-1,WIDE2-1, or empty"
    if bc["format"] not in ("standard", "lacf", "alternate"):
        errs["beacon.format"] = "Choose a format"
    if len(str(bc["symbol"])) != 2:
        errs["beacon.symbol"] = "Two characters: table and symbol"
    bc["comment"] = str(bc["comment"])[:80]
    if bc["enabled"] and not bc["use_gps"] and (bc["latitude"] is None or bc["longitude"] is None):
        errs["beacon.latitude"] = "Enter a position, or use GPS"

    if rd["profile"] not in hardware.PROFILES:
        errs["radio.profile"] = "Choose your board"
    try:
        rd["freq_mhz"] = round(float(rd["freq_mhz"]), 4)
        if not (410 <= rd["freq_mhz"] <= 510 or 850 <= rd["freq_mhz"] <= 930):
            raise ValueError
    except (TypeError, ValueError):
        errs["radio.freq_mhz"] = "MHz, 410 to 510 or 850 to 930"
    for key, allowed in (("sf", range(7, 13)), ("cr", range(5, 9))):
        try:
            rd[key] = int(rd[key])
            if rd[key] not in allowed:
                raise ValueError
        except (TypeError, ValueError):
            errs["radio." + key] = "Choose a value"
    try:
        rd["bw_khz"] = float(rd["bw_khz"])
        if rd["bw_khz"] not in (62.5, 125.0, 250.0, 500.0):
            raise ValueError
        if rd["bw_khz"].is_integer():
            rd["bw_khz"] = int(rd["bw_khz"])
    except (TypeError, ValueError):
        errs["radio.bw_khz"] = "Choose a bandwidth"
    try:
        rd["power_dbm"] = int(float(rd["power_dbm"]))
    except (TypeError, ValueError):
        errs["radio.power_dbm"] = "dBm at the antenna"
    limits = {"pa_gain_db": (0, 40, "Amplifier gain, 0 to 40 dB"),
              "max_chip_dbm": (-9, 22, "Chip limit, -9 to 22 dBm"),
              "tcxo_volts": (0, 3.3, "TCXO volts, 0 (none) to 3.3"),
              "spi_bus": (0, 6, "SPI bus number"), "spi_dev": (0, 2, "SPI device number")}
    for key, val in rd["custom"].items():
        lo, hi, msg = limits.get(key, (-1, 27, "BCM GPIO 0 to 27, or -1 if not connected"))
        try:
            num = float(val) if key in ("pa_gain_db", "tcxo_volts") else int(float(val))
            if not lo <= num <= hi:
                raise ValueError
            rd["custom"][key] = num
        except (TypeError, ValueError):
            errs["radio.custom." + key] = msg

    if cfg["digi"]["mode"] not in ("off", "fill-in", "wide"):
        errs["digi.mode"] = "Choose a digipeater mode"
    if cfg["digi"]["mode"] != "off" and st["callsign"] == "N0CALL":
        errs["station.callsign"] = "Enter your callsign to use the digipeater"

    ig = cfg["igate"]
    ig["enabled"] = bool(ig["enabled"])
    ig["server"] = str(ig["server"]).strip()
    if not re.match(r"^[A-Za-z0-9.-]{1,253}$", ig["server"]):
        errs["igate.server"] = "A server name such as rotate.aprs2.net"
    try:
        ig["port"] = int(ig["port"])
        if not 1 <= ig["port"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errs["igate.port"] = "A port number such as 14580"
    if str(ig["passcode"]).strip() == "":
        ig["passcode"] = ""
    else:
        try:
            ig["passcode"] = int(ig["passcode"])
            if not -1 <= ig["passcode"] <= 32767:
                raise ValueError
        except (TypeError, ValueError):
            errs["igate.passcode"] = "Leave empty, or a number from -1 to 32767"
    if ig["enabled"] and st["callsign"] == "N0CALL":
        errs["station.callsign"] = "Enter your callsign to use the iGate"

    gp["device"] = str(gp["device"]).strip()
    if gp["device"] and not gp["device"].startswith("/dev/"):
        errs["gps.device"] = "A device such as /dev/ttyACM0, or empty"
    try:
        gp["baud"] = int(gp["baud"])
    except (TypeError, ValueError):
        errs["gps.baud"] = "Baud rate such as 9600"

    pw = str(cfg["web"]["password"])
    if pw and not (4 <= len(pw) <= 64 and pw.isascii() and pw.isprintable()):
        errs["web.password"] = "4 to 64 plain characters (letters, digits, punctuation)"
    if not 1 <= len(str(wf["hotspot_ssid"])) <= 32:
        errs["wifi.hotspot_ssid"] = "1 to 32 characters"
    if not 8 <= len(str(wf["hotspot_password"])) <= 63:
        errs["wifi.hotspot_password"] = "8 to 63 characters"
    return errs


def radio_settings(cfg):
    """What the radio driver needs, with the chip power worked out for the board."""
    rd = cfg["radio"]
    prof = hardware.resolve(rd)
    return prof, {
        "freq_mhz": rd["freq_mhz"], "sf": rd["sf"], "bw_khz": rd["bw_khz"], "cr": rd["cr"],
        "preamble": PREAMBLE, "sync_word": SYNC_WORD,
        "chip_dbm": hardware.chip_power(rd["power_dbm"], prof),
    }


def public(cfg):
    """Settings as shown to the web page: passwords are never sent back."""
    out = copy.deepcopy(cfg)
    out["web"]["password"] = ""
    out["web"]["password_set"] = bool(cfg["web"]["password"])
    out["wifi"]["hotspot_password"] = ""
    return out
