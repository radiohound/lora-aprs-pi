"""Auto reduced power, and reports from digipeaters that repeated our beacon."""

import os
import tempfile
import time
import unittest

from lora_aprs_pi import digi, frames, power
from lora_aprs_pi.station import Station

CALL = "N0TST-3"


class TestMargin(unittest.TestCase):
    def test_weak_signal_judged_by_snr(self):
        self.assertEqual(power.margin_db(-125, -10, 12), 10)       # SF12 needs -20 dB

    def test_strong_signal_judged_by_rssi(self):
        self.assertEqual(power.margin_db(-100, 6, 12), 37)          # sensitivity -137 dBm

    def test_wider_bandwidth_less_sensitive(self):
        self.assertAlmostEqual(power.margin_db(-100, 6, 12, 250), 37 - 3.01, places=1)


class TestAutoPower(unittest.TestCase):
    def beacon(self, ap, *margins, fmt="lacf", path="WIDE1-1"):
        """Send a beacon, deliver reports with the given margins (via SNR at SF12), judge it."""
        ap.sent("info", fmt, path)
        for m in margins:
            ap.report("info", "N0DIGI", -137 + m, m - 20, 12, 125)   # both measures agree on m
        return ap.next_level(-1)

    def test_steps_down_holds_and_comes_back_up(self):
        ap = power.AutoPower(30)
        self.assertEqual(self.beacon(ap, 20), 28)         # 20 dB to spare: 2 dB lower
        self.assertEqual(self.beacon(ap, 11), 28)         # within the target band: hold
        self.assertEqual(self.beacon(ap, 7), 30)          # thin: up by 5, capped at the set maximum

    def test_best_report_counts(self):
        ap = power.AutoPower(30)
        self.assertEqual(self.beacon(ap, 3, 15), 28)

    def test_never_below_floor(self):
        ap = power.AutoPower(14)
        for _ in range(5):
            level = self.beacon(ap, 30)
        self.assertEqual(level, 10)

    def test_no_reports_goes_back_to_full_power(self):
        ap = power.AutoPower(30)
        self.beacon(ap, 30)
        self.beacon(ap, 30)
        self.assertEqual(ap.level, 26)
        self.assertEqual(self.beacon(ap), 26)             # one miss: hold
        self.assertEqual(self.beacon(ap), 30)             # two misses: full power

    def test_standard_beacons_not_judged(self):
        ap = power.AutoPower(30)
        self.beacon(ap, 30)
        self.assertEqual(self.beacon(ap, fmt="standard"), 28)
        self.assertEqual(self.beacon(ap, fmt="standard"), 28)   # no report expected, so no misses

    def test_report_for_another_packet_ignored(self):
        ap = power.AutoPower(30)
        ap.sent("info", "lacf", "WIDE1-1")
        ap.report("something else", "N0DIGI", -60, 8, 12, 125)
        self.assertEqual(ap.next_level(-1), 30)


class TestStation(unittest.TestCase):
    def test_digi_report_lowers_next_beacon(self):
        with tempfile.TemporaryDirectory() as d:
            st = Station(os.path.join(d, "config.json"), simulate=True)
            st.start()
            try:
                errs = st.change_settings({"station": {"callsign": CALL},
                                           "radio": {"profile": "meshadv_m30s", "power_dbm": 30},
                                           "beacon": {"latitude": 37.07, "longitude": -121.56,
                                                      "format": "lacf", "auto_reduce_power": True}})
                self.assertEqual(errs, {})
                end = time.time() + 5
                while st.status()["radio"]["state"] != "ok" and time.time() < end:
                    time.sleep(0.05)
                st.radio.next_rx = float("inf")

                st.beacon_now()
                end = time.time() + 5
                while not st.radio.sent and time.time() < end:
                    time.sleep(0.05)
                ours = st.radio.sent[0]
                self.assertEqual(st.radio.sent_power[0], 22)            # 30 dBm on the 30 dBm module

                # A digipeater repeats it and reports hearing us at -60 dBm, SNR +8 dB
                text = frames.decode(ours)["text"].decode()
                repeat_tnc2 = text.replace("WIDE1-1", "N0DIGI*")
                repeat = digi.build_frame(ours, "lacf", repeat_tnc2, "N0DIGI",
                                          digi.rxt_tuple(-60, 8, 0, 3000, 12, 125))
                st._received(repeat, -70.0, 5.0, 0)
                heard = st.heard_since(0)[0]
                self.assertTrue(heard["own_repeat"])
                self.assertEqual(heard["repeated_by"], "N0DIGI")
                self.assertEqual(heard["digi_report"], {"digi": "N0DIGI", "rssi": -60, "snr": 8.0})

                st.beacon_now()
                end = time.time() + 5
                while len(st.radio.sent) < 2 and time.time() < end:
                    time.sleep(0.05)
                self.assertEqual(st.radio.sent_power[1], 20)            # 2 dB lower
                self.assertEqual(st.status()["beacon"]["auto_power"]["level_dbm"], 28)
            finally:
                st.stop()


if __name__ == "__main__":
    unittest.main()
