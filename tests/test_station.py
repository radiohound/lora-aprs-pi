"""Packets, frames, settings and the station loop, using the simulated radio."""

import json
import os
import tempfile
import time
import unittest

from lora_aprs_pi import aprs, config, frames, gps, lacf
from lora_aprs_pi.station import Station

HERE = os.path.dirname(os.path.abspath(__file__))


def vectors(name):
    with open(os.path.join(HERE, "vectors", name)) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                yield line.split()


class TestLacfVectors(unittest.TestCase):
    """The vendored codec must give exactly the spec 11 test vectors."""

    def test_codec_built(self):
        self.assertTrue(lacf.available(), lacf.load_error())

    def test_encode_and_decode_all_vectors(self):
        n = 0
        for frame_hex, pkt_hex in vectors("vectors_hb_v1.txt"):
            pkt = bytes.fromhex(pkt_hex)
            if frame_hex == "-":
                self.assertIsNone(lacf.encode(pkt))
                continue
            frame = bytes.fromhex(frame_hex)
            self.assertEqual(lacf.encode(pkt), frame)
            self.assertEqual(lacf.decode(frame)[0], pkt)
            n += 1
        self.assertGreater(n, 500)

    def test_bad_frames_are_dropped(self):
        for row in vectors("vectors_hb_reject_v1.txt"):
            with self.assertRaises(lacf.LacfError):
                lacf.decode(bytes.fromhex(row[0]))

    def test_relay_vectors(self):
        for received, relayed, _ in vectors("vectors_hb_relay_v1.txt"):
            self.assertEqual(lacf.relay(bytes.fromhex(received), b"N0RLY-1", bytes([50, 20, 45, 10])),
                             bytes.fromhex(relayed))


class TestFrames(unittest.TestCase):
    PKT = b"N0CALL-7>APZLPI,WIDE1-1:!3704.20N/12133.80W>trail test"

    def test_standard_round_trip(self):
        frame, used = frames.encode(self.PKT, "standard")
        self.assertEqual((frame[:3], used), (b"<\xff\x01", "standard"))
        self.assertEqual(frames.decode(frame)["text"], self.PKT)

    def test_lacf_round_trip_and_smaller(self):
        frame, used = frames.encode(self.PKT, "lacf")
        self.assertEqual((frame[:3], used), (b"<\xff\x02", "lacf"))
        self.assertLess(len(frame), len(self.PKT) + 3)
        got = frames.decode(frame)
        self.assertEqual((got["format"], got["text"]), ("lacf", self.PKT))

    def test_broken_wide_path_not_sent(self):
        with self.assertRaises(ValueError):
            frames.encode(b"N0CALL>APZLPI,WIDE2-3:>status", "lacf")

    def test_unknown_frame_never_shown_as_text(self):
        got = frames.decode(b"<\xff\x81abcdef")
        self.assertEqual((got["format"], got["text"]), ("unknown", None))

    def test_damaged_lacf_dropped(self):
        frame, _ = frames.encode(self.PKT, "lacf")
        bad = frame[:-3] + bytes([frame[-3] ^ 0x10]) + frame[-2:]
        self.assertIsNone(frames.decode(bad)["text"])


class TestAprs(unittest.TestCase):
    def test_position_format(self):
        info = aprs.position_info(37.07, -121.563, "/[", "Hiker", 1820)
        self.assertEqual(info, "!3704.20N/12133.78W[Hiker /A=001820")

    def test_minutes_never_round_to_60(self):
        self.assertTrue(aprs.position_info(36.99999999, -121.0, "/-").startswith("!3700.00N"))

    def test_southern_and_eastern(self):
        self.assertEqual(aprs.position_info(-33.5, 151.25, "/-")[:19], "!3330.00S/15115.00E")

    def test_parse_uncompressed(self):
        p = aprs.parse(b"N0CALL-9>APLRT1,WIDE1-1:!3711.55N/12129.43W[Hiker /A=001820")
        self.assertEqual((p["source"], p["path"], p["symbol"]), ("N0CALL-9", "WIDE1-1", "/["))
        self.assertAlmostEqual(p["lat"], 37.19250, places=4)
        self.assertAlmostEqual(p["lon"], -121.49050, places=4)
        self.assertEqual(p["altitude_ft"], 1820)

    def test_parse_compressed(self):
        p = aprs.parse(b"N0CALL>APZ:!/5L!!<*e7>7P[")      # spec example: 49.5N 72.75W
        self.assertAlmostEqual(p["lat"], 49.5, places=2)
        self.assertAlmostEqual(p["lon"], -72.75, places=2)

    def test_parse_with_timestamp(self):
        p = aprs.parse(b"N0CALL>APZ:@092345z3704.20N/12133.80W-home")
        self.assertAlmostEqual(p["lat"], 37.07, places=3)

    def test_not_a_packet(self):
        self.assertIsNone(aprs.parse(b"hello world"))

    def test_distance(self):
        self.assertAlmostEqual(aprs.distance_km(37.0, -121.5, 38.0, -121.5), 111.2, places=1)


class TestConfig(unittest.TestCase):
    def test_defaults_check_clean_except_callsign_stays_valid(self):
        self.assertEqual(config.check(config.load("/nonexistent")), {})

    def test_bad_values_reported(self):
        cfg = config.load("/nonexistent")
        new, errs = config.update(cfg, {"station": {"callsign": "not a call"},
                                        "beacon": {"path": "WIDE3-5", "latitude": "95"},
                                        "radio": {"sf": 13}})
        for key in ("station.callsign", "beacon.path", "beacon.latitude", "radio.sf"):
            self.assertIn(key, errs)

    def test_callsign_tidied(self):
        cfg, errs = config.update(config.load("/nonexistent"), {"station": {"callsign": " k6atv-12 "}})
        self.assertEqual((errs, cfg["station"]["callsign"]), ({}, "K6ATV-12"))

    def test_passwords_never_shown(self):
        cfg = config.load("/nonexistent")
        cfg["web"]["password"] = "secret"
        pub = config.public(cfg)
        self.assertEqual((pub["web"]["password"], pub["web"]["password_set"]), ("", True))
        self.assertEqual(pub["wifi"]["hotspot_password"], "")


class TestGps(unittest.TestCase):
    def test_rmc_and_gga(self):
        fix = {"valid": False, "lat": None, "lon": None, "alt_m": None, "sats": 0, "time": 0}
        self.assertTrue(gps.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A", fix))
        self.assertTrue(fix["valid"])
        self.assertAlmostEqual(fix["lat"], 48.1173, places=4)
        self.assertAlmostEqual(fix["lon"], 11.5167, places=4)
        self.assertTrue(gps.parse("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47", fix))
        self.assertEqual((fix["sats"], fix["alt_m"]), (8, 545.4))

    def test_bad_checksum_ignored(self):
        fix = {"valid": False}
        self.assertFalse(gps.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*00", fix))


class TestStation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")
        self.st = Station(self.path, simulate=True)
        self.st.start()

    def tearDown(self):
        self.st.stop()
        self.dir.cleanup()

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_radio_comes_up(self):
        self.assertTrue(self.wait_for(lambda: self.st.status()["radio"]["state"] == "ok"))

    def test_no_beacon_without_callsign(self):
        self.st.beacon_now()
        self.assertTrue(self.wait_for(lambda: self.st.beacon_note is not None))
        self.assertIn("callsign", self.st.beacon_note)
        self.assertEqual(self.st.radio.sent, [])

    def test_settings_saved_and_lacf_beacon_sent(self):
        errs = self.st.change_settings({"station": {"callsign": "N0TST-3"},
                                        "beacon": {"latitude": 37.07, "longitude": -121.56, "format": "lacf"}})
        self.assertEqual(errs, {})
        with open(self.path) as f:
            self.assertEqual(json.load(f)["station"]["callsign"], "N0TST-3")
        self.assertTrue(self.wait_for(lambda: self.st.status()["radio"]["state"] == "ok"))
        self.st.beacon_now()
        self.assertTrue(self.wait_for(lambda: self.st.radio and self.st.radio.sent))
        frame = self.st.radio.sent[0]
        got = frames.decode(frame)
        self.assertEqual(got["format"], "lacf")
        self.assertTrue(got["text"].startswith(b"N0TST-3>APZLPI,WIDE1-1:!3704.20N/12133.60W"))
        self.assertEqual(self.st.heard_since(0)[0]["dir"], "tx")

    def test_bad_settings_rejected_and_not_saved(self):
        errs = self.st.change_settings({"radio": {"freq_mhz": "abc"}})
        self.assertIn("radio.freq_mhz", errs)
        self.assertFalse(os.path.exists(self.path))

    def test_empty_password_keeps_old(self):
        self.st.change_settings({"web": {"password": "pw123"}})
        self.st.change_settings({"web": {"password": ""}})
        self.assertEqual(self.st.password(), "pw123")
        self.st.change_settings({"web": {"clear_password": True}})
        self.assertEqual(self.st.password(), "")

    def test_received_packets_listed(self):
        frame, _ = frames.encode(b"N0TEST-7>APLRT1,WIDE1-1:!3704.20N/12133.80W>trail", "lacf")
        self.st._received(frame, -101.0, -3.25)
        self.st._received(None, -125.0, -15.0)
        rx = [e for e in self.st.heard_since(0) if e["dir"] == "rx"]
        self.assertEqual(rx[1]["source"], "N0TEST-7")
        self.assertEqual(rx[1]["format"], "lacf")
        self.assertIn("CRC", rx[0]["error"])


if __name__ == "__main__":
    unittest.main()
