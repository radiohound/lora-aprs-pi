"""LoRa HAT wiring profiles. Pin numbers are BCM GPIO numbers.

Power: the web page asks for power at the antenna. Ebyte E22 modules put an
external amplifier after the SX126x chip, so the chip itself must be set
lower than that. Each profile gives the amplifier gain and the most the
chip may be driven, and `chip_power()` works out the chip setting.

The 33 dBm modules (E22-400M33S, E22-900M33S) have about 25 dB of gain and
must not have the chip driven above about 8 to 9 dBm. The 30 dBm modules
(E22-400M30S, E22-900M30S) have about 8 dB of gain and take the chip's full
22 dBm. The gains are approximate: measure your own output if it matters.
"""

PROFILES = {
    "meshadv_m33s": {
        "label": "MeshAdv-Pi HAT, E22 33 dBm module (E22-400M33S / 900M33S)",
        "spi_bus": 0, "spi_dev": 0, "spi_hz": 2_000_000,
        "cs": 21, "reset": 18, "irq": 16, "busy": 20, "tx_en": 13, "rx_en": 12,
        "tcxo_volts": 1.8,
        "pa_gain_db": 25, "max_chip_dbm": 8,
    },
    "meshadv_m30s": {
        "label": "MeshAdv-Pi HAT, E22 30 dBm module (E22-400M30S / 900M30S)",
        "spi_bus": 0, "spi_dev": 0, "spi_hz": 2_000_000,
        "cs": 21, "reset": 18, "irq": 16, "busy": 20, "tx_en": 13, "rx_en": 12,
        "tcxo_volts": 1.8,
        "pa_gain_db": 8, "max_chip_dbm": 22,
    },
    "custom": {
        "label": "Other SX1262 / SX1268 board (enter the pins)",
        "spi_bus": 0, "spi_dev": 0, "spi_hz": 2_000_000,
        "cs": 8, "reset": 25, "irq": 24, "busy": 23, "tx_en": -1, "rx_en": -1,
        "tcxo_volts": 1.8,
        # Starts low on purpose: an unknown module may have a big amplifier after the chip.
        "pa_gain_db": 0, "max_chip_dbm": 8,
    },
}

DEFAULT_PROFILE = "meshadv_m33s"   # the safe choice: a 30 dBm module on it just runs low
PIN_KEYS = ("cs", "reset", "irq", "busy", "tx_en", "rx_en")
CUSTOM_KEYS = PIN_KEYS + ("spi_bus", "spi_dev", "pa_gain_db", "max_chip_dbm", "tcxo_volts")
CHIP_MIN_DBM = -9


def resolve(radio_cfg):
    """The profile for these settings, with custom pins and limits applied."""
    name = radio_cfg.get("profile", DEFAULT_PROFILE)
    prof = dict(PROFILES.get(name, PROFILES[DEFAULT_PROFILE]))
    if name == "custom":
        custom = radio_cfg.get("custom", {})
        for key in PIN_KEYS + ("spi_bus", "spi_dev", "max_chip_dbm"):
            if key in custom:
                prof[key] = int(custom[key])
        for key in ("pa_gain_db", "tcxo_volts"):
            if key in custom:
                prof[key] = float(custom[key])
        prof["max_chip_dbm"] = max(CHIP_MIN_DBM, min(22, prof["max_chip_dbm"]))
    return prof


def chip_power(antenna_dbm, prof):
    """Chip setting (dBm) for the wanted antenna power, kept inside the safe range."""
    want = round(antenna_dbm - prof["pa_gain_db"])
    return int(max(CHIP_MIN_DBM, min(prof["max_chip_dbm"], want)))


def antenna_power(chip_dbm, prof):
    """Approximate power at the antenna for a chip setting."""
    return chip_dbm + prof["pa_gain_db"]
