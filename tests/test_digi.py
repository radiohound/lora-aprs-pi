"""Digipeater: path rules, RXT scales, LACF relaying, and repeats from the station."""

import os
import tempfile
import time
import unittest

from lora_aprs_pi import digi, frames, lacf, station
from lora_aprs_pi.station import Station

CALL = "N0TST-3"
HERE = os.path.dirname(os.path.abspath(__file__))


class TestPathRules(unittest.TestCase):
    def path(self, p, mode="wide"):
        return digi.new_path(p, CALL, mode)

    def test_fill_in(self):
        self.assertEqual(self.path("WIDE1-1", "fill-in"), (f"{CALL}*", None))
        self.assertEqual(self.path("WIDE1-1,WIDE2-1", "fill-in"), (f"{CALL}*,WIDE2-1", None))
        self.assertIsNone(self.path("WIDE2-1", "fill-in")[0])        # fill-in leaves WIDE2 to wide digis

    def test_wide(self):
        self.assertEqual(self.path("WIDE2-1"), (f"{CALL}*", None))
        self.assertEqual(self.path("WIDE2-2"), (f"{CALL}*,WIDE2-1", None))
        self.assertEqual(self.path("N0DIGI*,WIDE2-1"), (f"N0DIGI*,{CALL}*", None))
        self.assertIsNone(self.path("WIDE7-7")[0])                    # big floods are not served

    def test_own_callsign_hop_any_mode(self):
        self.assertEqual(self.path(CALL, "fill-in"), (f"{CALL}*", None))
        self.assertEqual(self.path(f"N0DIGI*,{CALL}", "fill-in"), (f"N0DIGI*,{CALL}*", None))

    def test_never_twice(self):
        self.assertEqual(self.path(f"{CALL}*,WIDE2-1")[1], "already repeated by this station")

    def test_refusals(self):
        self.assertEqual(self.path("WIDE3-5")[1], "path breaks the WIDEn-N rule")
        self.assertEqual(self.path("")[1], "no hops left")
        self.assertEqual(self.path("WIDE1-1", "off")[1], "digipeater off")
        self.assertEqual(self.path("A,B,C,D,E,F,G,WIDE2-2")[1], "not a hop this digipeater serves (A)")
        self.assertEqual(self.path("A*,B*,C*,D*,E*,F*,G*,WIDE2-2")[1], "path too long")

    def test_nogate_and_rfonly_still_repeated(self):
        self.assertEqual(self.path("WIDE1-1,NOGATE", "fill-in"), (f"{CALL}*,NOGATE", None))
        self.assertEqual(self.path("WIDE1-1,RFONLY", "fill-in"), (f"{CALL}*,RFONLY", None))

    def test_decide(self):
        self.assertEqual(digi.decide("N0TEST-7>APLRT1,WIDE1-1:>hi", CALL, "fill-in"),
                         (f"N0TEST-7>APLRT1,{CALL}*:>hi", None))
        self.assertEqual(digi.decide(f"{CALL}>APZLPI,WIDE1-1:>me", CALL, "fill-in")[1], "our own packet")


class TestRxtScales(unittest.TestCase):
    """The same numbers as the iGate's rxt_scale.h."""

    def test_rssi_snr(self):
        self.assertEqual(digi.rxt_rssi(-80), 50)
        self.assertEqual(digi.rxt_rssi(-140), 0)
        self.assertEqual(digi.rxt_rssi(-20), 89)
        self.assertEqual(digi.rxt_snr(-14), 20)
        self.assertEqual(digi.rxt_snr(30), 88)

    def test_frequency_offset(self):
        self.assertEqual(digi.rxt_freq_offset(0), 45)
        self.assertEqual(digi.rxt_freq_offset(-2500), 0)
        self.assertEqual(digi.rxt_freq_offset(5000), 89)
        self.assertEqual(digi.rxt_freq_offset(625), 68)     # sqrt(0.25) * 45 + 45 = 67.5, rounds to 68

    def test_time_to_hop(self):
        unit = 4096 / 125 * 10                               # SF12/125: 327.68 ms
        self.assertEqual(digi.rxt_time_to_hop(0, 12, 125), 0)
        self.assertEqual(digi.rxt_time_to_hop(unit, 12, 125), 9)   # log(2)/log(1.08) = 9.0


class TestLacfRelay(unittest.TestCase):
    def test_matches_spec_relay_vectors(self):
        n = 0
        with open(os.path.join(HERE, "vectors", "vectors_hb_relay_v1.txt")) as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                received, relayed, relayed_pkt = line.split()
                got = digi.build_frame(bytes.fromhex(received), "lacf",
                                       bytes.fromhex(relayed_pkt).decode(), "N0RLY-1", bytes([50, 20, 45, 10]))
                self.assertEqual(got, bytes.fromhex(relayed))
                n += 1
        self.assertGreater(n, 5)

    def test_standard_repeated_as_text(self):
        self.assertEqual(digi.build_frame(b"", "standard", "A>B,C*:>x", CALL, b"\0\0\0\0"), b"<\xff\x01A>B,C*:>x")


class TestStationRepeats(unittest.TestCase):
    def setUp(self):
        self._delay = station.DIGI_DELAY_S
        station.DIGI_DELAY_S = (0, 0)
        self.dir = tempfile.TemporaryDirectory()
        self.st = Station(os.path.join(self.dir.name, "config.json"), simulate=True)
        self.st.start()
        self.assertEqual(self.st.change_settings({"station": {"callsign": CALL}, "digi": {"mode": "fill-in"}}), {})
        end = time.time() + 5
        while self.st.status()["radio"]["state"] != "ok" and time.time() < end:
            time.sleep(0.05)
        self.st.radio.next_rx = float("inf")    # the pretend radio stays quiet, so only our packets count

    def tearDown(self):
        station.DIGI_DELAY_S = self._delay
        self.st.stop()
        self.dir.cleanup()

    def wait_sent(self, n):
        end = time.time() + 5
        while time.time() < end:
            if self.st.radio and len(self.st.radio.sent) >= n:
                return self.st.radio.sent
            time.sleep(0.05)
        self.fail("nothing transmitted")

    def test_standard_packet_repeated(self):
        self.st._received(b"<\xff\x01N0TEST-7>APLRT1,WIDE1-1:>trail", -100.0, 2.0, 0)
        sent = self.wait_sent(1)
        self.assertEqual(sent[0], f"<\xff\x01N0TEST-7>APLRT1,{CALL}*:>trail".encode("latin-1"))
        tx = [e for e in self.st.heard_since(0) if e["dir"] == "tx"]
        self.assertEqual((tx[0]["kind"], tx[0]["source"]), ("repeat", "N0TEST-7"))

    def test_lacf_packet_relayed_with_signal_report(self):
        frame, _ = frames.encode(b"N0TEST-7>APLRT1,WIDE1-1:!3704.20N/12133.80W>trail", "lacf")
        self.st._received(frame, -80.0, -14.0, 0)
        out = self.wait_sent(1)[0]
        text, details = lacf.decode(out)
        self.assertEqual(text, f"N0TEST-7>APLRT1,{CALL}*:!3704.20N/12133.80W>trail".encode())
        self.assertEqual(details["relay_tuples"], 1)
        self.assertEqual(out[-5:-2], bytes([50, 20, 45]))   # RSSI -80, SNR -14, offset 0; then time to hop, mask

    def test_same_packet_only_once(self):
        self.st._received(b"<\xff\x01N0TEST-7>APLRT1,WIDE1-1:>once", -100.0, 2.0, 0)
        self.wait_sent(1)
        self.st._received(b"<\xff\x01N0TEST-7>APLRT1,WIDE1-1:>once", -100.0, 2.0, 0)
        time.sleep(0.3)
        self.assertEqual(len(self.st.radio.sent), 1)
        rx = [e for e in self.st.heard_since(0) if e["dir"] == "rx"]
        self.assertEqual(rx[0]["not_repeated"], "already repeated in the last 30 s")

    def test_hearing_our_repeat_back_does_nothing(self):
        self.st._received(f"<\xff\x01N0TEST-7>APLRT1,{CALL}*:>back".encode("latin-1"), -100.0, 2.0, 0)
        time.sleep(0.3)
        self.assertEqual(self.st.radio.sent, [])


if __name__ == "__main__":
    unittest.main()
