"""SX126x driver tests against a pretend chip (no hardware needed)."""

import unittest

from lora_aprs_pi import hardware
from lora_aprs_pi import sx126x as sx


class FakeGpio:
    def __init__(self):
        self.levels = {}

    def output(self, pin, level=0):
        if pin >= 0:
            self.levels[pin] = level

    def input(self, pin):
        if pin >= 0:
            self.levels.setdefault(pin, 0)

    def write(self, pin, level):
        if pin >= 0:
            self.levels[pin] = 1 if level else 0

    def read(self, pin):
        return self.levels.get(pin, 0)

    def close(self):
        pass


class FakeChip:
    """Answers SPI transfers the way an SX1262 would, closely enough for the driver."""

    def __init__(self, gpio, irq_pin, alive=True):
        self.gpio, self.irq_pin, self.alive = gpio, irq_pin, alive
        self.log = []
        self.regs = {}
        self.buffer = bytearray(256)
        self.irq = 0
        self.rx = None            # (payload, rssi byte, snr byte, irq bits) to deliver

    def _raise(self, bits):
        self.irq |= bits
        self.gpio.levels[self.irq_pin] = 1

    def deliver(self, payload, rssi_b=200, snr_b=0xF6, extra_irq=0):
        self.buffer[0x10:0x10 + len(payload)] = payload
        self.rx = (len(payload), 0x10)
        self.pkt = [rssi_b, snr_b, rssi_b]
        self._raise(sx.IRQ_RX_DONE | extra_irq)

    def xfer(self, data):
        data = list(data)
        op = data[0]
        self.log.append(data)
        out = [0] * len(data)
        if not self.alive:
            return out
        if op == sx.WRITE_REGISTER:
            addr = (data[1] << 8) | data[2]
            for i, b in enumerate(data[3:]):
                self.regs[addr + i] = b
        elif op == sx.READ_REGISTER:
            addr = (data[1] << 8) | data[2]
            for i in range(len(data) - 4):
                out[4 + i] = self.regs.get(addr + i, 0)
        elif op == sx.GET_IRQ_STATUS:
            out[2], out[3] = self.irq >> 8, self.irq & 0xFF
        elif op == sx.CLEAR_IRQ_STATUS:
            self.irq = 0
            self.gpio.levels[self.irq_pin] = 0
        elif op == sx.GET_RX_BUFFER_STATUS:
            out[2], out[3] = self.rx
        elif op == sx.GET_PACKET_STATUS:
            out[2:5] = self.pkt
        elif op == sx.READ_BUFFER:
            start = data[1]
            out[3:] = list(self.buffer[start:start + len(data) - 3])
        elif op == sx.WRITE_BUFFER:
            self.buffer[data[1]:data[1] + len(data) - 2] = bytes(data[2:])
        elif op == sx.SET_TX:
            self._raise(sx.IRQ_TX_DONE)
        return out

    def close(self):
        pass

    def sent(self, op):
        return [d for d in self.log if d[0] == op]


SETTINGS = {"freq_mhz": 433.775, "sf": 12, "bw_khz": 125, "cr": 5,
            "preamble": 8, "sync_word": 0x12, "chip_dbm": -5}


def make_radio(alive=True):
    prof = hardware.resolve({"profile": "meshadv_m33s"})
    gpio = FakeGpio()
    chip = FakeChip(gpio, prof["irq"], alive)
    radio = sx.SX126x(prof, spi=chip, gpio=gpio)
    return radio, chip, gpio, prof


class TestHelpers(unittest.TestCase):
    def test_airtime_matches_lora_formula(self):
        self.assertAlmostEqual(sx.airtime_s(58, 7, 125) * 1000, 112.9, places=1)
        self.assertAlmostEqual(sx.airtime_s(58, 9, 125) * 1000, 369.7, places=1)
        self.assertAlmostEqual(sx.airtime_s(58, 12, 125) * 1000, 2629.6, places=1)

    def test_sync_word_lora_aprs(self):
        self.assertEqual(sx.sync_word_bytes(0x12), [0x14, 0x24])

    def test_frequency_word(self):
        self.assertEqual(sx.freq_word(433_775_000), round(433_775_000 * 2 ** 25 / 32e6))

    def test_image_calibration_any_frequency(self):
        self.assertEqual(sx.image_cal_bytes(433.775), (0x6B, 0x6F))
        self.assertEqual(sx.image_cal_bytes(915.0), (0xE1, 0xE9))
        lo, hi = sx.image_cal_bytes(445.0)          # outside the datasheet's bands
        self.assertTrue(lo * 4 <= 445.0 <= hi * 4)

    def test_ldro_by_symbol_time(self):
        self.assertTrue(sx.needs_ldro(12, 125))
        self.assertTrue(sx.needs_ldro(11, 125))
        self.assertTrue(sx.needs_ldro(12, 250))       # 16.38 ms symbols
        self.assertTrue(sx.needs_ldro(10, 62.5))      # 16.38 ms symbols
        self.assertFalse(sx.needs_ldro(11, 250))
        self.assertFalse(sx.needs_ldro(9, 125))

    def test_bandwidth_codes(self):
        self.assertEqual(sx.bw_code(125), 0x04)
        self.assertEqual(sx.bw_code(62.5), 0x03)
        with self.assertRaises(sx.RadioError):
            sx.bw_code(100)


class TestPower(unittest.TestCase):
    def test_33dbm_module_never_overdriven(self):
        p = hardware.PROFILES["meshadv_m33s"]
        self.assertEqual(hardware.chip_power(33, p), 8)
        self.assertEqual(hardware.chip_power(40, p), 8)
        self.assertEqual(hardware.chip_power(20, p), -5)
        self.assertEqual(hardware.chip_power(0, p), -9)

    def test_30dbm_module(self):
        p = hardware.PROFILES["meshadv_m30s"]
        self.assertEqual(hardware.chip_power(30, p), 22)
        self.assertEqual(hardware.chip_power(20, p), 12)

    def test_custom_pins(self):
        p = hardware.resolve({"profile": "custom", "custom": {"cs": 7, "busy": 5, "max_chip_dbm": 14}})
        self.assertEqual((p["cs"], p["busy"], p["max_chip_dbm"]), (7, 5, 14))

    def test_custom_board_starts_low_and_is_capped(self):
        self.assertEqual(hardware.resolve({"profile": "custom"})["max_chip_dbm"], 8)
        self.assertEqual(hardware.resolve({"profile": "custom", "custom": {"max_chip_dbm": 40}})["max_chip_dbm"], 22)


class TestDriver(unittest.TestCase):
    def test_begin_configures_lora_aprs(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        self.assertIn([sx.CALIBRATE_IMAGE, 0x6B, 0x6F], chip.log)
        self.assertIn([sx.SET_MODULATION_PARAMS, 12, 0x04, 1, 1], chip.log)   # SF12 BW125 CR4/5 LDRO
        pp = chip.sent(sx.SET_PACKET_PARAMS)[-1]
        self.assertEqual(pp[3], 0x00)   # explicit header
        self.assertEqual(pp[5], 0x01)   # CRC on
        self.assertIn([sx.SET_TX_PARAMS, (-5) & 0xFF, 0x04], chip.log)
        self.assertEqual([chip.regs[0x0740], chip.regs[0x0741]], [0x14, 0x24])

    def test_dead_radio_is_reported(self):
        radio, _, _, _ = make_radio(alive=False)
        with self.assertRaises(sx.RadioError):
            radio.begin(SETTINGS)

    def test_receive_reads_payload_and_signal(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        radio.start_rx()
        self.assertIsNone(radio.poll())
        chip.deliver(b"<\xff\x01N0CALL>APZ:>hi", rssi_b=200, snr_b=0xF6)
        payload, rssi, snr = radio.poll()
        self.assertEqual(payload, b"<\xff\x01N0CALL>APZ:>hi")
        self.assertEqual((rssi, snr), (-100.0, -2.5))

    def test_crc_error_gives_no_payload(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        radio.start_rx()
        chip.deliver(b"garbled", extra_irq=sx.IRQ_CRC_ERR)
        payload, rssi, snr = radio.poll()
        self.assertIsNone(payload)

    def test_transmit_then_back_to_receive(self):
        radio, chip, gpio, prof = make_radio()
        radio.begin(SETTINGS)
        radio.start_rx()
        frame = b"<\xff\x01N0CALL>APZ:>test"
        radio.transmit(frame)
        self.assertIn([sx.WRITE_BUFFER, 0x00, *frame], chip.log)
        self.assertEqual(chip.sent(sx.SET_PACKET_PARAMS)[-2][4], len(frame))
        self.assertEqual(chip.log[-1][0], sx.SET_RX)       # listening again
        self.assertEqual(gpio.levels[prof["rx_en"]], 1)
        self.assertEqual(gpio.levels[prof["tx_en"]], 0)

    def test_433_mhz_uses_sx1268_pa_table(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        self.assertIn([sx.SET_PA_CONFIG, 0x04, 0x06, 0x00, 0x01], chip.log)
        radio.configure(dict(SETTINGS, freq_mhz=915.0))
        self.assertEqual(chip.sent(sx.SET_PA_CONFIG)[-1], [sx.SET_PA_CONFIG, 0x04, 0x07, 0x00, 0x01])

    def test_dio2_left_alone_when_pi_drives_the_switch(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        self.assertIn([sx.SET_DIO2_AS_RF_SWITCH, 0x00], chip.log)

    def test_only_read_interrupts_are_cleared(self):
        radio, chip, _, _ = make_radio()
        radio.begin(SETTINGS)
        radio.start_rx()
        chip.deliver(b"<\xff\x01N0CALL>APZ:>hi")
        radio.poll()
        self.assertEqual(chip.sent(sx.CLEAR_IRQ_STATUS)[-1], [sx.CLEAR_IRQ_STATUS, 0x00, sx.IRQ_RX_DONE])

    def test_failed_setup_gives_back_pins(self):
        prof = dict(hardware.resolve({"profile": "meshadv_m33s"}))

        class BrokenGpio(FakeGpio):
            closed = False

            def input(self, pin):
                raise sx.RadioError("GPIO 16 is in use by another program")

            def close(self):
                BrokenGpio.closed = True

        gpio = BrokenGpio()
        with self.assertRaises(sx.RadioError):
            sx.SX126x(prof, spi=FakeChip(gpio, prof["irq"]), gpio=gpio)
        self.assertTrue(BrokenGpio.closed)

    def test_oversize_packet_refused(self):
        radio, _, _, _ = make_radio()
        radio.begin(SETTINGS)
        with self.assertRaises(sx.RadioError):
            radio.transmit(bytes(256))


if __name__ == "__main__":
    unittest.main()
