"""The MQTT feed: event lines like the ESP32 iGate's, and the small MQTT client."""

import json
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from lora_aprs_pi import config, frames, lacf, mqtt

RADIO = {"freq_mhz": 433.775, "bw_khz": 125, "sf": 12, "cr": 5}
PKT = b"N0TEST-7>APLRT1,K6ATV-10*,WIDE2-1:!3704.20N/12133.80W>trail"


class FakeBroker:
    """Accepts one MQTT client, answers CONNECT with the given code, and keeps what is published."""

    def __init__(self, code=0):
        self.code = code
        self.connects = []
        self.published = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _read_packet(self, conn):
        head = conn.recv(1)
        if not head:
            return None, None
        mult, n = 1, 0
        while True:
            b = conn.recv(1)[0]
            n += (b & 127) * mult
            mult *= 128
            if not b & 128:
                break
        body = b""
        while len(body) < n:
            body += conn.recv(n - len(body))
        return head[0], body

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                while True:
                    kind, body = self._read_packet(conn)
                    if kind is None:
                        break
                    if kind == 0x10:
                        self.connects.append(body)
                        conn.sendall(bytes([0x20, 2, 0, self.code]))
                        if self.code:
                            break
                    elif kind & 0xF0 == 0x30:
                        tl = struct.unpack("!H", body[:2])[0]
                        self.published.append((body[2:2 + tl].decode(), json.loads(body[2 + tl:])))
                    elif kind == 0xC0:
                        conn.sendall(b"\xd0\x00")

    def close(self):
        self.srv.close()


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.ev = mqtt.Events("K6ATV-1", RADIO)

    def test_classify(self):
        self.assertEqual(mqtt.classify(b"<\xff\x01abc"), "plain")
        if lacf.available():
            # LACF with compressed content is "hybrid"; its plain-content form is "native" (the map counts both as LACF)
            self.assertIn(mqtt.classify(lacf.encode(PKT)), ("hybrid", "native"))
        self.assertEqual(mqtt.classify(b"<\xff\x81x"), "ppm_full")
        self.assertEqual(mqtt.classify(b"hello"), "unknown")

    def test_rx_line(self):
        raw = frames.PLAIN + PKT
        e = self.ev.rx(raw, -101.4, -7.25, 312, PKT)
        self.assertEqual((e["protocol"], e["event"], e["sequence"]), ("lora-aprs-events", "rx", 1))
        self.assertEqual(e["receiver"]["station"], "K6ATV-1")
        self.assertEqual(e["reception"]["local"], {"rssi_dbm": -101, "snr_db": -7.2, "freq_error_hz": 312})
        self.assertEqual(e["reception"]["radio"], {"frequency_hz": 433775000, "bandwidth_hz": 125000,
                                                   "spreading_factor": 12, "coding_rate": "4/5"})
        self.assertEqual(e["frame"], {"class": "plain", "air_bytes": len(raw), "plain_bytes": len(PKT) + 3, "outcome": "ok"})
        p = e["packet"]
        self.assertEqual((p["source"], p["destination"], p["path"]), ("N0TEST-7", "APLRT1", ["K6ATV-10*", "WIDE2-1"]))
        self.assertEqual((p["heard_from"], p["direct"], p["hops_used"], p["private_path"]), ("K6ATV-10", False, 1, False))
        self.assertEqual(p["tnc2"], PKT.decode())

    def test_heard_from_behind_an_alias(self):
        p = self.ev.rx(frames.PLAIN + b"A>B,K6ATV-10,WIDE1*:x", -90, 5, 0, b"A>B,K6ATV-10,WIDE1*:x")["packet"]
        self.assertEqual(p["heard_from"], "K6ATV-10")
        p = self.ev.rx(frames.PLAIN + b"A>B,WIDE1*:x", -90, 5, 0, b"A>B,WIDE1*:x")["packet"]
        self.assertIsNone(p["heard_from"])
        p = self.ev.rx(frames.PLAIN + b"A>B,WIDE1-1:x", -90, 5, 0, b"A>B,WIDE1-1:x")["packet"]
        self.assertEqual((p["heard_from"], p["direct"]), ("A", True))

    def test_private_packets_are_counted_but_never_sent(self):
        for pkt in (b"A>B,RFONLY:x", b"A>B,nogate:x", b"A>B,WIDE1-1,RFONLY-1:x"):
            self.assertIsNone(self.ev.rx(frames.PLAIN + pkt, -90, 5, 0, pkt))
        self.assertEqual(self.ev.received["plain"], 3)

    def test_crc_errors_carry_no_bytes_and_are_limited(self):
        e = self.ev.rx(None, -125, -15, 10, None)
        self.assertEqual((e["reception"]["crc_valid"], e["frame"]["outcome"], e["packet"]), (False, "crc_error", None))
        self.assertNotIn("raw_hex", e["frame"])
        sent = 1 + sum(1 for _ in range(50) if self.ev.rx(None, -125, -15, 10, None))
        self.assertEqual((sent, self.ev.crc_errors), (mqtt.CRC_EVENTS_PER_MIN, 51))

    def test_unreadable_frame(self):
        e = self.ev.rx(b"<\xff\x05junk", -100, 0, 0, None)
        self.assertEqual(e["frame"]["outcome"], "parse_failed")
        self.assertEqual(self.ev.parse_failed, 1)

    def test_no_events_before_the_clock_is_set(self):
        with mock.patch("time.time", return_value=1000.0):
            self.assertIsNone(self.ev.rx(frames.PLAIN + PKT, -90, 5, 0, PKT))
            self.assertIsNone(self.ev.tx(frames.PLAIN + PKT))

    def test_tx_and_status(self):
        t = self.ev.tx(frames.PLAIN + PKT)
        self.assertEqual(t["frame"], {"class": "plain", "air_bytes": len(PKT) + 3, "ok": True})
        self.ev.rx(frames.PLAIN + PKT, -90, 5, 0, PKT)
        self.ev.rx(None, -125, -15, 0, None)
        for dbm in (-110, -110, -100):
            self.ev.noise(dbm)
        self.ev.loop_pass(35)
        s = self.ev.status()
        self.assertEqual(s["received"]["plain"], 1)
        self.assertEqual((s["crc_errors"], s["loop_max_ms"], s["rx_gain"]), (1, 35, "boosted"))
        self.assertEqual((s["noise_floor"]["min_dbm"], s["noise_floor"]["max_dbm"], s["noise_floor"]["samples"]), (-110, -100, 3))
        self.assertAlmostEqual(s["noise_floor"]["avg_dbm"], -104.0, places=1)       # a power average, not -106.7
        self.assertIsNone(self.ev.status()["noise_floor"])                          # the samples start again each minute


class TestSettings(unittest.TestCase):
    def test_defaults_filled_in_but_off(self):
        mq = config.DEFAULTS["mqtt"]
        self.assertFalse(mq["enabled"])
        self.assertEqual((mq["server"], mq["port"], mq["username"], mq["password"], mq["topic"]),
                         ("mqtt.lora-aprs.org", 1884, "igate", "lora-aprs", "lora-aprs"))

    def test_checks(self):
        cfg = config.load("/nonexistent")
        cfg["mqtt"].update(enabled=True, port="x", topic="bad topic")
        errs = config.check(cfg)
        self.assertIn("mqtt.port", errs)
        self.assertIn("mqtt.topic", errs)
        self.assertIn("station.callsign", errs)

    def test_public_map_login_is_shown(self):
        out = config.public(config.load("/nonexistent"))
        self.assertEqual((out["mqtt"]["password"], out["mqtt"]["password_public"]), ("lora-aprs", True))

    def test_own_password_is_not_shown(self):
        cfg = config.load("/nonexistent")
        cfg["mqtt"].update(username="walter", password="s3cret")
        out = config.public(cfg)
        self.assertEqual((out["mqtt"]["password"], out["mqtt"]["password_public"], out["mqtt"]["password_set"]),
                         ("", False, True))
        cfg["mqtt"].update(username="igate", password="s3cret")          # the shared user name with a private password
        self.assertEqual(config.public(cfg)["mqtt"]["password"], "")


class TestClient(unittest.TestCase):
    def test_connects_with_callsign_as_client_id_and_publishes(self):
        b = FakeBroker()
        c = mqtt.Mqtt("127.0.0.1", b.port, "K6ATV-1", "igate", "lora-aprs", "lora-aprs")
        try:
            self.assertTrue(wait_for(lambda: c.state == "connected"))
            body = b.connects[0]
            self.assertIn(b"\x00\x07K6ATV-1", body)
            self.assertIn(b"\x00\x05igate\x00\x09lora-aprs", body)
            for i in range(5):
                c.publish("rx", {"n": i})
            self.assertTrue(wait_for(lambda: len(b.published) == 5, 3))     # all five promptly, not one a second
            self.assertEqual(b.published[0], ("lora-aprs/K6ATV-1/rx", {"n": 0}))
            self.assertEqual(c.status()["sent"], 5)
        finally:
            c.stop()
            b.close()

    def test_refused_login_is_reported(self):
        b = FakeBroker(code=5)
        c = mqtt.Mqtt("127.0.0.1", b.port, "K6ATV-1", "igate", "wrong", "lora-aprs")
        try:
            self.assertTrue(wait_for(lambda: c.state == "error"))
            self.assertIn("not allowed", c.error)
        finally:
            c.stop()
            b.close()

    def test_long_message_length_encoding(self):
        pkt = mqtt.publish_packet("t", b"x" * 300)
        self.assertEqual(pkt[:3], bytes([0x30, (303 % 128) | 0x80, 303 // 128]))


class TestStation(unittest.TestCase):
    def test_simulated_station_feeds_the_broker(self):
        import os
        import tempfile
        from lora_aprs_pi.station import Station
        b = FakeBroker()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            cfg = config.load(path)
            cfg["station"]["callsign"] = "K6ATV-1"
            cfg["mqtt"].update(enabled=True, server="127.0.0.1", port=b.port)
            config.save(path, cfg)
            st = Station(path, simulate=True)
            st.start()
            try:
                st.radio_dirty = False
                self.assertTrue(wait_for(lambda: st.mqtt and st.mqtt.state == "connected"))
                st.radio.next_rx = 0
                self.assertTrue(wait_for(lambda: any(t.endswith("/rx") for t, _ in b.published)))
                self.assertEqual(st.status()["mqtt"]["state"], "connected")
                st.status_due = 0
                self.assertTrue(wait_for(lambda: any(t.endswith("/status") for t, _ in b.published)))
                status = [e for t, e in b.published if t.endswith("/status")][0]
                self.assertGreater(status["noise_floor"]["samples"], 0)
            finally:
                st.stop()
                b.close()


if __name__ == "__main__":
    unittest.main()
