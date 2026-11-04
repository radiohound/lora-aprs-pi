"""iGate: passcode, gating rules, dedupe, and the APRS-IS connection against a pretend server."""

import os
import socket
import tempfile
import threading
import time
import unittest

from lora_aprs_pi import frames, igate
from lora_aprs_pi.station import Station

CALL = "N0TST-3"


class FakeServer:
    """A tiny APRS-IS server: answers the login and keeps every line it receives."""

    def __init__(self, verified=True):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.verified = verified
        self.lines = []
        self.login = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        f = conn.makefile("rb")
        self.login = f.readline().decode().strip()
        call = self.login.split()[1]
        word = "verified" if self.verified else "unverified"
        conn.sendall(f"# logresp {call} {word}, server T2TEST\r\n".encode())
        self.conn = conn
        while True:
            line = f.readline()
            if not line:
                break
            self.lines.append(line.decode().rstrip("\r\n"))
        conn.close()

    def close(self):
        if getattr(self, "conn", None):
            self.conn.close()
        self.sock.close()


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestPasscode(unittest.TestCase):
    def test_known_value(self):
        self.assertEqual(igate.passcode("N0CALL"), 13023)

    def test_ssid_and_case_ignored(self):
        self.assertEqual(igate.passcode("k6atv-12"), igate.passcode("K6ATV"))


class TestRules(unittest.TestCase):
    def gate(self, pkt):
        return igate.check(pkt, CALL)

    def test_ordinary_packet_gated(self):
        pkt = "N0TEST-7>APLRT1,WIDE1-1:!3704.20N/12133.80W>trail"
        self.assertEqual(self.gate(pkt), (pkt, None))

    def test_blocked_hops(self):
        for hop in ("NOGATE", "RFONLY", "TCPIP*", "TCPXX", "nogate"):
            text, reason = self.gate(f"N0TEST-7>APLRT1,WIDE1-1,{hop}:>status")
            self.assertIsNone(text, hop)
            self.assertIn("in path", reason)

    def test_blocked_word_only_as_a_whole_hop(self):
        # a digipeater whose name merely contains the word is not a block
        self.assertIsNotNone(self.gate("N0TEST-7>APLRT1,NOGATEX*:>status")[0])

    def test_third_party_unwrapped(self):
        inner = "N0TEST-9>APLRT1,WIDE1-1:>hello"
        text, _ = self.gate(f"N0DIGI>APLRG1,WIDE1-1:}}{inner}")
        self.assertEqual(text, inner)

    def test_third_party_from_internet_not_regated(self):
        text, reason = self.gate("N0DIGI>APLRG1:}N0TEST-9>APRS,TCPIP,N0DIGI*:>hello")
        self.assertIsNone(text)
        self.assertEqual(reason, "TCPIP in path")

    def test_query_own_and_injection(self):
        self.assertEqual(self.gate("N0TEST-7>APLRT1:?APRS?")[1], "general query")
        self.assertEqual(self.gate(f"{CALL}>APZLPI,WIDE1-1:>me")[1], "our own packet")
        self.assertEqual(self.gate("N0TEST-7>APLRT1:>hi\r\nN0EVIL>APRS:>injected")[1], "has a line break")

    def test_upload_line(self):
        self.assertEqual(igate.upload_line("N0TEST-7>APLRT1,WIDE1-1:>hi", CALL),
                         f"N0TEST-7>APLRT1,WIDE1-1,qAO,{CALL}:>hi")


class TestDedupe(unittest.TestCase):
    def test_same_packet_by_two_paths(self):
        d = igate.Dedupe(30)
        self.assertTrue(d.is_new("N0TEST-7>APLRT1,WIDE1-1:>hi", now=0))
        self.assertFalse(d.is_new("N0TEST-7>APLRT1,N0DIGI*:>hi", now=10))
        self.assertTrue(d.is_new("N0TEST-7>APLRT1,WIDE1-1:>hi", now=41))
        self.assertTrue(d.is_new("N0TEST-7>APLRT1,WIDE1-1:>different", now=41))


class TestConnection(unittest.TestCase):
    def test_login_and_send(self):
        srv = FakeServer()
        conn = igate.AprsIs("127.0.0.1", srv.port, CALL, igate.passcode(CALL))
        try:
            self.assertTrue(wait_for(lambda: conn.status()["state"] == "connected"))
            self.assertTrue(srv.login.startswith(f"user {CALL} pass {igate.passcode(CALL)} vers LoRaAPRSPi"))
            self.assertTrue(conn.status()["verified"])
            self.assertEqual(conn.status()["server"], "T2TEST")
            time.sleep(2.5)                     # stay quiet past the read timeout, then send
            conn.send("N0TEST-7>APLRT1,qAO,N0TST-3:>hi")
            self.assertTrue(wait_for(lambda: srv.lines))
            self.assertEqual(srv.lines[0], "N0TEST-7>APLRT1,qAO,N0TST-3:>hi")
        finally:
            conn.stop()
            srv.close()

    def test_unverified_reported(self):
        srv = FakeServer(verified=False)
        conn = igate.AprsIs("127.0.0.1", srv.port, CALL, 1)
        try:
            self.assertTrue(wait_for(lambda: conn.status()["state"] == "connected"))
            self.assertFalse(conn.status()["verified"])
        finally:
            conn.stop()
            srv.close()


class TestStationGates(unittest.TestCase):
    def test_heard_packets_reach_aprs_is(self):
        srv = FakeServer()
        with tempfile.TemporaryDirectory() as d:
            st = Station(os.path.join(d, "config.json"), simulate=True)
            st.start()
            try:
                errs = st.change_settings({"station": {"callsign": CALL},
                                           "igate": {"enabled": True, "server": "127.0.0.1", "port": srv.port}})
                self.assertEqual(errs, {})
                self.assertTrue(wait_for(lambda: st.status()["igate"].get("state") == "connected"))

                lacf_frame, _ = frames.encode(b"N0TEST-7>APLRT1,WIDE1-1:!3704.20N/12133.80W>trail", "lacf")
                st._received(lacf_frame, -100.0, 2.0)
                st._received(b"<\xff\x01N0TEST-9>APLRT1,RFONLY:>keep me off the internet", -100.0, 2.0)

                self.assertTrue(wait_for(lambda: srv.lines))
                self.assertEqual(srv.lines[0],
                                 f"N0TEST-7>APLRT1,WIDE1-1,qAO,{CALL}:!3704.20N/12133.80W>trail")
                heard = st.heard_since(0)
                self.assertEqual(heard[0]["not_gated"], "RFONLY in path")
                self.assertTrue(heard[1]["gated"])
                self.assertEqual(st.status()["counts"]["gated"], 1)
            finally:
                st.stop()
                srv.close()

    def test_igate_needs_a_callsign(self):
        with tempfile.TemporaryDirectory() as d:
            st = Station(os.path.join(d, "config.json"), simulate=True)
            errs = st.change_settings({"igate": {"enabled": True}})
            self.assertIn("station.callsign", errs)


if __name__ == "__main__":
    unittest.main()
