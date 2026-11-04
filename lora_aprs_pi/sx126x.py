"""SX1262 / SX1268 LoRa radio driver for the Raspberry Pi.

Talks to the chip through the Linux SPI device (python3-spidev) and drives
the reset, busy, interrupt and RF-switch pins through lgpio (python3-lgpio),
which works on every Pi from the Zero 2 W to the Pi 5.

Commands and register addresses are from the Semtech SX1261/2 datasheet;
the SX1268 (433 MHz E22 modules) uses the same command set.
"""

import math
import time


class RadioError(Exception):
    """The radio is missing, miswired, or did not answer."""


# Commands
SET_STANDBY = 0x80
SET_RX = 0x82
SET_TX = 0x83
SET_RF_FREQUENCY = 0x86
SET_PACKET_TYPE = 0x8A
SET_MODULATION_PARAMS = 0x8B
SET_PACKET_PARAMS = 0x8C
SET_TX_PARAMS = 0x8E
SET_BUFFER_BASE_ADDRESS = 0x8F
SET_PA_CONFIG = 0x95
SET_DIO2_AS_RF_SWITCH = 0x9D
SET_DIO3_AS_TCXO = 0x97
CALIBRATE = 0x89
CALIBRATE_IMAGE = 0x98
SET_DIO_IRQ_PARAMS = 0x08
CLEAR_IRQ_STATUS = 0x02
GET_IRQ_STATUS = 0x12
GET_RX_BUFFER_STATUS = 0x13
GET_PACKET_STATUS = 0x14
WRITE_REGISTER = 0x0D
READ_REGISTER = 0x1D
WRITE_BUFFER = 0x0E
READ_BUFFER = 0x1E

# Registers
REG_SYNC_WORD = 0x0740
REG_FREQ_ERROR = 0x076B          # not in the datasheet; RadioLib reads it the same way
REG_RX_GAIN = 0x08AC
REG_OCP = 0x08E7

# Interrupt bits
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002
IRQ_HEADER_ERR = 0x0020
IRQ_CRC_ERR = 0x0040
IRQ_TIMEOUT = 0x0200
IRQ_USED = IRQ_TX_DONE | IRQ_RX_DONE | IRQ_HEADER_ERR | IRQ_CRC_ERR | IRQ_TIMEOUT

BW_CODES = {7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02,
            41.7: 0x0A, 62.5: 0x03, 125: 0x04, 250: 0x05, 500: 0x06}

# Image calibration bands (MHz low, MHz high, byte 1, byte 2), datasheet 9.2.1
IMAGE_BANDS = [(430, 440, 0x6B, 0x6F), (470, 510, 0x75, 0x81),
               (779, 787, 0xC1, 0xC5), (863, 870, 0xD7, 0xDB), (902, 928, 0xE1, 0xE9)]


def image_cal_bytes(freq_mhz):
    """CalibrateImage bytes: the datasheet's band if the frequency is in one,
    otherwise a window around it computed the way RadioLib does."""
    for lo, hi, b1, b2 in IMAGE_BANDS:
        if lo <= freq_mhz <= hi:
            return b1, b2
    lo, hi = freq_mhz - 5, freq_mhz + 5
    return int(math.floor((lo - 1) / 4)), int(math.ceil((hi + 1) / 4))


def needs_ldro(sf, bw_khz):
    """Low data rate optimisation: on when a symbol lasts 16.38 ms or more."""
    return (2 ** sf) / (bw_khz * 1000.0) >= 16.38e-3

TCXO_CODES = [(1.6, 0), (1.7, 1), (1.8, 2), (2.2, 3), (2.4, 4), (2.7, 5), (3.0, 6), (3.3, 7)]


def airtime_s(nbytes, sf, bw_khz, cr=5, preamble=8, crc=True):
    """Time on air of one LoRa packet (explicit header), in seconds."""
    tsym = (2 ** sf) / (bw_khz * 1000.0)
    ldro = 1 if needs_ldro(sf, bw_khz) else 0
    num = 8 * nbytes - 4 * sf + 28 + (16 if crc else 0)
    nsym = 8 + max(math.ceil(num / (4.0 * (sf - 2 * ldro))) * cr, 0)
    return (preamble + 4.25 + nsym) * tsym


def sync_word_bytes(sw):
    """One-byte LoRa sync word (0x12 for LoRa APRS) as the SX126x two bytes."""
    return [((sw >> 4) & 0x0F) << 4 | 0x04, (sw & 0x0F) << 4 | 0x04]


def freq_word(freq_hz):
    """RF frequency register value (32 MHz crystal, 2^25 steps)."""
    return int(round(freq_hz * (1 << 25) / 32_000_000))


def bw_code(bw_khz):
    for bw, code in BW_CODES.items():
        if abs(bw - bw_khz) < 0.5:
            return code
    raise RadioError(f"unsupported bandwidth {bw_khz} kHz")


class Gpio:
    """Raspberry Pi GPIO lines through lgpio."""

    LABELS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835")

    def __init__(self):
        try:
            import lgpio
        except ImportError as exc:
            raise RadioError("python3-lgpio is not installed (sudo apt install python3-lgpio)") from exc
        self.lg = lgpio
        self.h = self._open_chip()
        self.claimed = set()

    def _open_chip(self):
        lg = self.lg
        first = None
        for n in range(6):
            try:
                h = lg.gpiochip_open(n)
            except lg.error:
                continue
            try:
                label = lg.gpio_get_chip_info(h)[3]
            except Exception:
                label = ""
            if any(name in label for name in self.LABELS):
                if first is not None:
                    lg.gpiochip_close(first)
                return h
            if first is None:
                first = h
            else:
                lg.gpiochip_close(h)
        if first is None:
            raise RadioError("no GPIO chip found; is this a Raspberry Pi?")
        return first

    def _claim(self, pin, output, level=0):
        lg = self.lg
        for attempt in range(2):
            try:
                if output:
                    lg.gpio_claim_output(self.h, pin, level)
                else:
                    lg.gpio_claim_input(self.h, pin)
                self.claimed.add(pin)
                return
            except lg.error:
                if attempt:
                    raise RadioError(f"GPIO {pin} is in use by another program")
                try:
                    lg.gpio_free(self.h, pin)      # left claimed by a crashed run
                except lg.error:
                    pass

    def output(self, pin, level=0):
        if pin >= 0:
            self._claim(pin, True, level)

    def input(self, pin):
        if pin >= 0:
            self._claim(pin, False)

    def write(self, pin, level):
        if pin >= 0:
            self.lg.gpio_write(self.h, pin, 1 if level else 0)

    def read(self, pin):
        return self.lg.gpio_read(self.h, pin)

    def close(self):
        for pin in list(self.claimed):
            try:
                self.lg.gpio_free(self.h, pin)
            except Exception:
                pass
        self.claimed.clear()
        try:
            self.lg.gpiochip_close(self.h)
        except Exception:
            pass


class Spi:
    """SPI through spidev, with a GPIO chip-select when the HAT doesn't use CE0/CE1."""

    NATIVE_CS = {0: 8, 1: 7}

    def __init__(self, bus, dev, hz, cs_pin, gpio):
        try:
            import spidev
        except ImportError as exc:
            raise RadioError("python3-spidev is not installed (sudo apt install python3-spidev)") from exc
        self.spi = spidev.SpiDev()
        try:
            self.spi.open(bus, dev)
        except OSError as exc:
            raise RadioError(f"cannot open /dev/spidev{bus}.{dev}; is SPI turned on? ({exc})") from exc
        self.spi.mode = 0
        self.spi.max_speed_hz = int(hz)
        self.gpio = gpio
        self.cs = cs_pin if cs_pin >= 0 and cs_pin != self.NATIVE_CS.get(dev) else -1
        if self.cs >= 0:
            gpio.output(self.cs, 1)

    def xfer(self, data):
        if self.cs >= 0:
            self.gpio.write(self.cs, 0)
        try:
            return self.spi.xfer2(list(data))
        finally:
            if self.cs >= 0:
                self.gpio.write(self.cs, 1)

    def close(self):
        self.spi.close()


class SX126x:
    """One SX1262/SX1268. Not thread safe: one thread owns the radio."""

    def __init__(self, prof, spi=None, gpio=None):
        self.p = prof
        self.settings = None
        self.spi = None
        self.gpio = gpio or Gpio()
        try:
            self.spi = spi or Spi(prof["spi_bus"], prof["spi_dev"], prof["spi_hz"], prof["cs"], self.gpio)
            self.gpio.output(prof["reset"], 1)
            self.gpio.output(prof["tx_en"], 0)
            self.gpio.output(prof["rx_en"], 0)
            if prof["busy"] >= 0:
                self.gpio.input(prof["busy"])
            if prof["irq"] >= 0:
                self.gpio.input(prof["irq"])
        except Exception:
            # give back the pins and the SPI device, or the next retry finds them "in use"
            if self.spi:
                self.spi.close()
            self.gpio.close()
            raise

    # ---- low level ----

    def _wait_busy(self, timeout=1.0):
        pin = self.p["busy"]
        if pin < 0:
            time.sleep(0.002)
            return
        end = time.monotonic() + timeout
        while self.gpio.read(pin):
            if time.monotonic() > end:
                raise RadioError("radio stays busy; check the HAT is seated and the BUSY pin")
            time.sleep(0.0002)

    def _cmd(self, opcode, *params):
        self._wait_busy()
        self.spi.xfer([opcode, *params])

    def _read(self, opcode, n):
        """Get-type command: opcode, then the status byte, then n data bytes."""
        self._wait_busy()
        return self.spi.xfer([opcode, 0] + [0] * n)[2:]

    def write_reg(self, addr, data):
        self._wait_busy()
        self.spi.xfer([WRITE_REGISTER, addr >> 8, addr & 0xFF, *data])

    def read_reg(self, addr, n):
        self._wait_busy()
        return self.spi.xfer([READ_REGISTER, addr >> 8, addr & 0xFF, 0] + [0] * n)[4:]

    def _rf_switch(self, mode):
        self.gpio.write(self.p["tx_en"], mode == "tx")
        self.gpio.write(self.p["rx_en"], mode == "rx")

    def irq_status(self):
        d = self._read(GET_IRQ_STATUS, 2)
        return (d[0] << 8) | d[1]

    def clear_irq(self, bits=0xFFFF):
        self._cmd(CLEAR_IRQ_STATUS, (bits >> 8) & 0xFF, bits & 0xFF)

    def _irq_pending(self):
        pin = self.p["irq"]
        if pin >= 0:
            return bool(self.gpio.read(pin))
        return bool(self.irq_status() & IRQ_USED)

    # ---- setup ----

    def reset(self):
        self.gpio.write(self.p["reset"], 0)
        time.sleep(0.002)
        self.gpio.write(self.p["reset"], 1)
        time.sleep(0.010)
        self._wait_busy()

    def begin(self, settings):
        """Reset the chip and set it up for LoRa APRS with these settings."""
        self.reset()
        self._cmd(SET_STANDBY, 0x00)                       # STDBY_RC
        if self.p.get("tcxo_volts"):
            code = min(TCXO_CODES, key=lambda vc: abs(vc[0] - self.p["tcxo_volts"]))[1]
            delay = 320                                    # x 15.625 us = 5 ms
            self._cmd(SET_DIO3_AS_TCXO, code, delay >> 16, (delay >> 8) & 0xFF, delay & 0xFF)
            self._cmd(CALIBRATE, 0x7F)                     # recalibrate on the TCXO clock
            time.sleep(0.005)
        # DIO2 drives the RF switch only on boards where TXEN is wired to it. When the
        # Pi drives TXEN/RXEN itself, DIO2 is left alone so the two never fight.
        dio2 = self.p.get("dio2_rf_switch", self.p["tx_en"] < 0)
        self._cmd(SET_DIO2_AS_RF_SWITCH, 0x01 if dio2 else 0x00)
        self._cmd(SET_PACKET_TYPE, 0x01)                   # LoRa
        self.write_reg(REG_SYNC_WORD, sync_word_bytes(0x12))
        if self.read_reg(REG_SYNC_WORD, 2) != sync_word_bytes(0x12):
            raise RadioError("the radio did not answer over SPI; check SPI is on and the HAT profile")
        self.configure(settings)

    def configure(self, s):
        """Apply frequency, SF, BW, CR, sync word and power (chip dBm) without a reset."""
        self.settings = dict(s)
        self._cmd(SET_STANDBY, 0x00)
        freq_hz = int(round(float(s["freq_mhz"]) * 1e6))
        self._cmd(CALIBRATE_IMAGE, *image_cal_bytes(freq_hz / 1e6))
        f = freq_word(freq_hz)
        self._cmd(SET_RF_FREQUENCY, f >> 24, (f >> 16) & 0xFF, (f >> 8) & 0xFF, f & 0xFF)

        # High-power PA, +22 dBm table. Below 600 MHz the chip is an SX1268 (E22-400
        # modules), whose table uses hpMax 0x06; the SX1262 (E22-900) uses 0x07.
        hp_max = 0x06 if freq_hz < 600_000_000 else 0x07
        self._cmd(SET_PA_CONFIG, 0x04, hp_max, 0x00, 0x01)
        self.write_reg(REG_OCP, [0x38])                    # 140 mA current limit
        chip = int(max(-9, min(22, s["chip_dbm"])))
        self._cmd(SET_TX_PARAMS, chip & 0xFF, 0x04)        # 200 us ramp

        sf, bw, cr = int(s["sf"]), float(s["bw_khz"]), int(s["cr"])
        ldro = 1 if needs_ldro(sf, bw) else 0
        self._cmd(SET_MODULATION_PARAMS, sf, bw_code(bw), cr - 4, ldro)
        self._packet_params(255)
        self.write_reg(REG_SYNC_WORD, sync_word_bytes(int(s.get("sync_word", 0x12))))
        self._cmd(SET_BUFFER_BASE_ADDRESS, 0x00, 0x00)
        self._cmd(SET_DIO_IRQ_PARAMS, IRQ_USED >> 8, IRQ_USED & 0xFF,
                  IRQ_USED >> 8, IRQ_USED & 0xFF, 0, 0, 0, 0)
        self.write_reg(REG_RX_GAIN, [0x96])                # boosted RX gain

    def _packet_params(self, length):
        pre = int(self.settings["preamble"]) if self.settings else 8
        # preamble, explicit header, length, CRC on (as LoRa APRS uses), standard IQ
        self._cmd(SET_PACKET_PARAMS, pre >> 8, pre & 0xFF, 0x00, length & 0xFF, 0x01, 0x00)

    # ---- receive and transmit ----

    def start_rx(self):
        self._cmd(SET_STANDBY, 0x00)
        self._packet_params(255)
        self.clear_irq()
        self._rf_switch("rx")
        self._cmd(SET_RX, 0xFF, 0xFF, 0xFF)                # continuous receive

    def poll(self):
        """None, or a received packet as (payload or None on CRC error, RSSI, SNR, frequency offset Hz)."""
        if not self._irq_pending():
            return None
        irq = self.irq_status()
        if not irq:
            return None
        self.clear_irq(irq)                                # only what we read, so nothing newer is lost
        if not irq & IRQ_RX_DONE:
            return None
        rssi, snr = self._packet_status()
        fo = self._freq_error_hz()
        if irq & (IRQ_CRC_ERR | IRQ_HEADER_ERR):
            return (None, rssi, snr, fo)
        length, start = self._read(GET_RX_BUFFER_STATUS, 2)
        self._wait_busy()
        data = self.spi.xfer([READ_BUFFER, start, 0] + [0] * length)[3:]
        return (bytes(data), rssi, snr, fo)

    def _freq_error_hz(self):
        """How far the sender was off frequency, as RadioLib's getFrequencyError() works it out."""
        b = self.read_reg(REG_FREQ_ERROR, 3)
        raw = ((b[0] << 16) | (b[1] << 8) | b[2]) & 0x0FFFFF
        if raw & 0x80000:
            raw -= 1 << 20
        return int(raw * float(self.settings["bw_khz"]) * 1000 * 155.0 / 160_000_000)

    def _packet_status(self):
        d = self._read(GET_PACKET_STATUS, 3)
        snr = d[1] - 256 if d[1] > 127 else d[1]
        return (-d[0] / 2.0, snr / 4.0)

    def transmit(self, payload):
        """Send one packet, wait until it's out, then go back to receive. Returns seconds on air."""
        if not 0 < len(payload) <= 255:
            raise RadioError("packet must be 1 to 255 bytes")
        s = self.settings
        expect = airtime_s(len(payload), int(s["sf"]), float(s["bw_khz"]), int(s["cr"]), int(s["preamble"]))
        self._cmd(SET_STANDBY, 0x00)
        self.clear_irq()
        self._wait_busy()
        self.spi.xfer([WRITE_BUFFER, 0x00, *payload])
        self._packet_params(len(payload))
        self._rf_switch("tx")
        start = time.monotonic()
        self._cmd(SET_TX, 0x00, 0x00, 0x00)
        deadline = start + expect * 1.5 + 2.0
        try:
            while True:
                if self._irq_pending():
                    irq = self.irq_status()
                    if irq & IRQ_TX_DONE:
                        break
                if time.monotonic() > deadline:
                    raise RadioError("transmit did not finish; check the antenna and power settings")
                time.sleep(0.005)
        finally:
            self.clear_irq()
            self.start_rx()
        return time.monotonic() - start

    def close(self):
        try:
            self._cmd(SET_STANDBY, 0x00)
            self._rf_switch("off")
        except Exception:
            pass
        if self.spi:
            self.spi.close()
        self.gpio.close()
