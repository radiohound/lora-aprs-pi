"""LACF 3.0.0-dev.1 (type 03) frames against the vectors of the proposal repository."""

import json
import os
import tempfile
import time
import unittest

from lora_aprs_pi import capability, config, frames, lacf3, mqtt, neighbours
from lora_aprs_pi import station
from lora_aprs_pi.station import Station
from lora_aprs_pi.lacf import LacfError

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_CALL = b"N0RLY-1"


def vectors(name):
    with open(os.path.join(HERE, "vectors", name)) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                yield line.split()


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestType03(unittest.TestCase):
    def test_decode_matches_vectors(self):
        n = 0
        for frame, tnc2 in vectors("vectors_hb3_v1.txt"):
            if frame == "-":
                continue
            got, _ = lacf3.decode(bytes.fromhex(frame))
            self.assertEqual(got, bytes.fromhex(tnc2))
            n += 1
        self.assertGreater(n, 100)

    def test_encode_matches_vectors(self):
        n = 0
        for frame, tnc2 in vectors("vectors_hb3_v1.txt"):
            if frame == "-":
                self.assertIsNone(lacf3.encode(bytes.fromhex(tnc2)))
                continue
            self.assertEqual(lacf3.encode(bytes.fromhex(tnc2)), bytes.fromhex(frame))
            n += 1
        self.assertGreater(n, 100)

    def test_rejection_vectors_are_dropped(self):
        n = 0
        for line in vectors("vectors_hb3_reject_v1.txt"):
            with self.assertRaises(LacfError, msg=line[0]):
                lacf3.decode(bytes.fromhex(line[0]))
            n += 1
        self.assertGreater(n, 10)

    def test_capability_vectors(self):
        n = 0
        for frame, tnc2, capb in vectors("vectors_hb3_cap_v1.txt"):
            got, details = lacf3.decode(bytes.fromhex(frame))
            self.assertEqual(got, bytes.fromhex(tnc2))
            self.assertEqual(details["capability_hex"], capb)
            self.assertEqual(lacf3.encode(bytes.fromhex(tnc2), bytes.fromhex(capb)), bytes.fromhex(frame))
            n += 1
        self.assertGreater(n, 3)

    def test_capability_pack_parse(self):
        b = lacf3.cap_pack(5, 1, 7, 0, 0, 22, 2)
        self.assertEqual(lacf3.cap_parse(b), {"role": 5, "model": 1, "sf": 7, "bw": 0, "cr": 0, "power": 22, "preset": 2})
        with self.assertRaises(ValueError):
            lacf3.cap_pack(8, 0, 0, 0, 0, 0, 0)

    def _relay_vectors(self, name):
        n = 0
        for k, line in enumerate(vectors(name)):
            received, relayed, relayed_pkt = line
            frame = bytes.fromhex(received)
            if relayed == "-":
                continue
            hops = lacf3.decode(frame)[1]["relay_tuples"]
            call = b"N0RLY-%d" % (hops + 1)
            tup = bytes([50 + hops, 20 + hops, 45 + hops, 10 + hops])
            got = lacf3.relay_to(frame, bytes.fromhex(relayed_pkt), call, tup)
            self.assertEqual(got, bytes.fromhex(relayed))
            n += 1
        self.assertGreater(n, 5)

    def test_relay_vectors(self):
        self._relay_vectors("vectors_hb3_relay_v1.txt")
        self._relay_vectors("vectors_hb3_relay_chain_v1.txt")

    def test_passthrough_vectors(self):
        n = 0
        for received, pass_relayed, decoder_relayed in vectors("vectors_hb3_passthrough_v1.txt"):
            if received == "-":
                continue
            frame = bytes.fromhex(received)
            if pass_relayed == "-":
                with self.assertRaises(LacfError):
                    lacf3.front(frame)
                continue
            text, _ = lacf3.front(frame)
            self.assertTrue(text.endswith(b":"))
            # the digipeater's own path rule decides the new path; here it is the one of the expected frame
            new_text, _ = lacf3.front(bytes.fromhex(pass_relayed))
            got = lacf3.relay_to_passthrough(frame, new_text, RELAY_CALL, bytes([50, 20, 45, 10]))
            self.assertEqual(got, bytes.fromhex(pass_relayed))
            n += 1
        self.assertGreater(n, 5)


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestReceive(unittest.TestCase):
    def test_frames_decode_picks_type_03(self):
        frame, tnc2 = next(vectors("vectors_hb3_v1.txt"))
        got = frames.decode(bytes.fromhex(frame))
        self.assertEqual(got["format"], "lacf3")
        self.assertEqual(got["text"], bytes.fromhex(tnc2))
        self.assertIsNone(got["error"])

    def test_type_02_still_goes_to_the_old_decoder(self):
        with open(os.path.join(HERE, "vectors", "vectors_hb_v1.txt")) as f:
            line = next(l for l in f if l.strip() and not l.startswith("#") and not l.startswith("-"))
        frame = bytes.fromhex(line.split()[0])
        self.assertEqual(frames.decode(frame)["format"], "lacf")

    def test_damaged_type_03_is_dropped_not_shown(self):
        frame, _ = next(vectors("vectors_hb3_v1.txt"))
        bad = bytearray(bytes.fromhex(frame))
        bad[-1] ^= 0xFF
        got = frames.decode(bytes(bad))
        self.assertIsNone(got["text"])
        self.assertIn("dropped", got["error"])

    def test_capability_frame_reaches_the_details(self):
        frame, _, capb = next(vectors("vectors_hb3_cap_v1.txt"))
        got = frames.decode(bytes.fromhex(frame))
        self.assertEqual(got["lacf"]["capability_hex"], capb)
        self.assertIn("role", got["lacf"]["capabilities"])


    def test_model_not_held_reports_the_front_for_a_relay(self):
        frame = next(l[0] for l in vectors("vectors_hb3_passthrough_v1.txt") if l[0] != "-")
        got = frames.decode(bytes.fromhex(frame))
        self.assertIsNone(got["text"])            # never shown, gated or used
        self.assertIn("can be relayed without the model", got["error"])
        self.assertEqual(got["front"], b"K0TEST-7>APWL2K,WIDE1-1,WIDE2-2:")

    def test_mqtt_class_of_type_03_is_hybrid(self):
        frame, _ = next(vectors("vectors_hb3_v1.txt"))
        self.assertEqual(mqtt.classify(bytes.fromhex(frame)), "hybrid")


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestStationNeighbours(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.st = Station(os.path.join(self.dir.name, "config.json"), simulate=True)
        self.st.start()
        self.st.change_settings({"station": {"callsign": "N0ME-1"}})
        end = time.time() + 5
        while self.st.status()["radio"]["state"] != "ok" and time.time() < end:
            time.sleep(0.05)
        self.st.radio.next_rx = float("inf")

    def tearDown(self):
        self.st.stop()
        self.dir.cleanup()

    def test_capability_frame_lands_in_the_neighbour_table(self):
        frame, tnc2, capb = next(vectors("vectors_hb3_cap_v1.txt"))
        self.st._received(bytes.fromhex(frame), -90.0, 3.0, 0)
        (n,) = self.st.status()["neighbours"]
        self.assertEqual(n["call"], bytes.fromhex(tnc2).split(b">")[0].decode())
        self.assertTrue(n["direct"])
        self.assertIsNotNone(n["capabilities"])
        heard = self.st.heard_since(0)
        self.assertEqual(heard[-1]["format"], "lacf3")
        json.dumps(heard)                          # the Heard list goes to the web page as JSON
        json.dumps(self.st.status())


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestStationRepeatsType03(unittest.TestCase):
    CALL = "N0RLY-1"

    def setUp(self):
        self._delay = station.DIGI_DELAY_S
        station.DIGI_DELAY_S = (0, 0)
        self.dir = tempfile.TemporaryDirectory()
        self.st = Station(os.path.join(self.dir.name, "config.json"), simulate=True)
        self.st.start()
        self.assertEqual(self.st.change_settings({"station": {"callsign": self.CALL}, "digi": {"mode": "fill-in"}}), {})
        end = time.time() + 5
        while self.st.status()["radio"]["state"] != "ok" and time.time() < end:
            time.sleep(0.05)
        self.st.radio.next_rx = float("inf")

    def tearDown(self):
        station.DIGI_DELAY_S = self._delay
        self.st.stop()
        self.dir.cleanup()

    def wait_sent(self, n=1):
        end = time.time() + 5
        while time.time() < end:
            if self.st.radio and len(self.st.radio.sent) >= n:
                return self.st.radio.sent
            time.sleep(0.05)
        self.fail("nothing transmitted")

    def test_decodable_frame_is_repeated_as_type_03_with_our_tuple(self):
        received, relayed, relayed_pkt = next(vectors("vectors_hb3_relay_v1.txt"))
        self.st._received(bytes.fromhex(received), -90.0, 3.0, 0)
        sent = self.wait_sent()[0]
        self.assertTrue(sent.startswith(lacf3.PREFIX))
        text, details = lacf3.decode(sent)
        self.assertEqual(text, bytes.fromhex(relayed_pkt))
        self.assertEqual(details["relay_tuples"], 1)
        tx = [e for e in self.st.heard_since(0) if e["dir"] == "tx"]
        self.assertEqual((tx[0]["kind"], tx[0]["format"], tx[0]["without_model"]), ("repeat", "lacf3", False))

    def test_frame_without_our_model_is_repeated_without_it(self):
        n = 0
        for received, pass_relayed, _ in vectors("vectors_hb3_passthrough_v1.txt"):
            if received == "-" or pass_relayed == "-":
                continue
            front, _ = lacf3.front(bytes.fromhex(received))
            if b"WIDE1-1" not in front.split(b",", 1)[1].split(b",")[0:1]:
                continue
            before = len(self.st.radio.sent)
            self.st.digi_seen = station.igate.Dedupe()
            self.st._received(bytes.fromhex(received), -90.0, 3.0, 0)
            sent = self.wait_sent(before + 1)[before]
            # same front as the expected pass-through frame (our call in the path), content copied byte for byte
            self.assertEqual(lacf3.front(sent)[0], lacf3.front(bytes.fromhex(pass_relayed))[0])
            want = bytes.fromhex(pass_relayed)         # all but the RXT block (4 tuple bytes + mask) is identical
            self.assertEqual(len(sent), len(want))
            self.assertEqual(sent[:-5], want[:-5])
            self.assertEqual(lacf3.front(sent)[1]["relay_tuples"], 1)
            tx = [e for e in self.st.heard_since(0) if e["dir"] == "tx"][-1]
            self.assertTrue(tx["without_model"])
            n += 1
            if n == 3:
                break
        self.assertGreater(n, 0)

    def test_frame_without_our_model_is_not_shown_gated_or_used(self):
        received = next(l[0] for l in vectors("vectors_hb3_passthrough_v1.txt") if l[0] != "-")
        self.st._received(bytes.fromhex(received), -90.0, 3.0, 0)
        e = [x for x in self.st.heard_since(0) if x["dir"] == "rx"][-1]
        self.assertIsNone(e["text"])
        self.assertEqual(self.st.status()["neighbours"], [])

    def test_damaged_frame_is_not_repeated(self):
        received, _, _ = next(vectors("vectors_hb3_relay_v1.txt"))
        bad = bytearray(bytes.fromhex(received))
        bad[-1] ^= 0xFF
        self.st._received(bytes(bad), -90.0, 3.0, 0)
        time.sleep(0.5)
        self.assertEqual(self.st.radio.sent, [])


class TestCapabilitySchedule(unittest.TestCase):
    CAP = b"\x01\x02\x03\x04"

    def test_first_frame_then_every_8th_but_not_within_an_hour(self):
        s = capability.Schedule()
        self.assertTrue(s.due(self.CAP, 0))
        s.sent(True, self.CAP, 0)
        for i in range(1, 12):                       # frames 1..11 within the hour: never
            self.assertFalse(s.due(self.CAP, i * 60), i)
            s.sent(False, self.CAP, i * 60)
        self.assertTrue(s.due(self.CAP, 3700))       # the count is past 8 and the hour is over

    def test_every_8th_exactly(self):
        s = capability.Schedule()
        s.sent(True, self.CAP, 0)
        t = 4000
        for i in range(1, 8):
            self.assertFalse(s.due(self.CAP, t), i)
            s.sent(False, self.CAP, t)
        self.assertTrue(s.due(self.CAP, t))          # the 8th frame

    def test_at_least_every_6_hours_and_on_a_change(self):
        s = capability.Schedule()
        s.sent(True, self.CAP, 0)
        self.assertFalse(s.due(self.CAP, 6 * 3600 - 1))
        self.assertTrue(s.due(self.CAP, 6 * 3600))
        self.assertTrue(s.due(b"\x01\x02\x03\x05", 10))

    def test_fields_from_the_settings(self):
        cfg = config.load("/nonexistent/config.json")
        cfg["digi"]["mode"] = "fill-in"
        cfg["igate"]["enabled"] = True
        cfg["radio"].update(freq_mhz=433.775, sf=12, bw_khz=125, cr=5, power_dbm=22)
        self.assertEqual(capability.fields(cfg), (5, 1, 7, 1, 0, 22, 2))
        cfg["igate"]["enabled"] = False
        cfg["digi"]["mode"] = "off"
        cfg["radio"]["freq_mhz"] = 868.1
        self.assertEqual(capability.fields(cfg)[0], 1)
        self.assertEqual(capability.fields(cfg)[6], 0)


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestStationBeaconsType03(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.st = Station(os.path.join(self.dir.name, "config.json"), simulate=True)
        self.st.start()
        errs = self.st.change_settings({"station": {"callsign": "K6ATV-10"}, "digi": {"mode": "fill-in"},
                                        "beacon": {"enabled": True, "latitude": 37.02, "longitude": -121.6,
                                                   "format": "lacf3", "comment": "Hill digi", "comment_every": 1}})
        self.assertEqual(errs, {})
        self.st.radio_dirty = False
        end = time.time() + 5
        while self.st.radio is None and time.time() < end:
            time.sleep(0.05)

    def tearDown(self):
        self.st.stop()
        self.dir.cleanup()

    def caps(self):
        return [lacf3.decode(f)[1]["capabilities"] for f in self.st.radio.sent]

    def test_beacons_are_type_03_and_announce_on_schedule(self):
        for _ in range(9):
            self.st._beacon(manual=True)
        sent = self.st.radio.sent
        self.assertTrue(all(f.startswith(lacf3.PREFIX) for f in sent))
        carried = [lacf3.decode(f)[1]["capabilities"] is not None for f in sent]
        self.assertEqual(carried[0], True)                 # the first frame after start-up
        self.assertEqual(carried[1:], [False] * 8)         # then not within the hour, whatever the count
        cap = lacf3.decode(sent[0])[1]["capabilities"]
        self.assertEqual((cap["role"], cap["sf"], cap["bw"], cap["preset"]), (2, 7, 1, 2))

    def test_a_settings_change_announces_at_once(self):
        self.st._beacon(manual=True)
        self.st._beacon(manual=True)
        self.st.change_settings({"digi": {"mode": "off"}})
        self.st.radio_dirty = False
        self.st._beacon(manual=True)
        caps = [lacf3.decode(f)[1]["capabilities"] for f in self.st.radio.sent]
        self.assertIsNotNone(caps[0])
        self.assertIsNone(caps[1])
        self.assertEqual(caps[2]["role"], 1)


class TestNeighbours(unittest.TestCase):
    CAP = {"role": 5, "model": 1, "sf": 7, "bw": 1, "cr": 0, "power": 22, "preset": 2}

    def test_direct_then_via_relay_keeps_capabilities(self):
        n = neighbours.Neighbours()
        n.heard("k6atv-14", True, -50, 10.0, self.CAP, now=1000)
        n.heard("K6ATV-14", False, -80, 2.0, now=1100)
        (e,) = n.table(now=1200)
        self.assertEqual(e["call"], "K6ATV-14")
        self.assertFalse(e["direct"])
        self.assertEqual(e["capabilities"]["role"], "iGate and digipeater")
        self.assertEqual(e["capabilities"]["sf"], 12)
        self.assertEqual(e["capabilities"]["bw_khz"], 125)
        self.assertEqual(e["capabilities"]["newest_model"], 0)
        self.assertEqual(e["capabilities"]["power_dbm"], 22)

    def test_expires_after_24_hours(self):
        n = neighbours.Neighbours()
        n.heard("A", True, -50, 10.0, now=0)
        self.assertEqual(n.table(now=24 * 3600 - 1)[0]["call"], "A")
        self.assertEqual(n.table(now=24 * 3600 + 1), [])

    def test_holds_at_most_24_and_drops_the_oldest(self):
        n = neighbours.Neighbours()
        for i in range(30):
            n.heard("S%d" % i, True, -50, 1.0, now=100 + i)
        t = n.table(now=200)
        self.assertEqual(len(t), neighbours.MAX)
        self.assertNotIn("S0", [e["call"] for e in t])
        self.assertIn("S29", [e["call"] for e in t])


if __name__ == "__main__":
    unittest.main()
