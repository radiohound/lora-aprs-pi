"""Listen before talk: every transmission waits for a free channel (the iGate firmware's scheme), against the pretend radio."""

import os
import tempfile
import time
import unittest

from lora_aprs_pi import station
from lora_aprs_pi.station import Station

CALL = "N0RLY-1"


class LbtCase(unittest.TestCase):
    def setUp(self):
        self._saved = (station.DIGI_DELAY_S, station.CAD_BACKOFF_MAX, station.CAD_MAX_WAIT_S)
        station.DIGI_DELAY_S = (0, 0)
        station.CAD_BACKOFF_MAX = 1                    # one slot (about 0.13 s at SF12) per busy check, to keep the tests short
        self.dir = tempfile.TemporaryDirectory()
        self.st = Station(os.path.join(self.dir.name, "config.json"), simulate=True)
        self.st.start()
        self.assertEqual(self.st.change_settings({"station": {"callsign": CALL}, "digi": {"mode": "fill-in"},
                                                  "beacon": {"enabled": True, "latitude": 37.02, "longitude": -121.6}}), {})
        end = time.time() + 5
        while self.st.status()["radio"]["state"] != "ok" and time.time() < end:
            time.sleep(0.05)
        self.st.radio.next_rx = float("inf")           # the pretend radio stays quiet unless a test says otherwise
        self.st.radio_dirty = False

    def tearDown(self):
        station.DIGI_DELAY_S, station.CAD_BACKOFF_MAX, station.CAD_MAX_WAIT_S = self._saved
        self.st.stop()
        self.dir.cleanup()

    def wait_sent(self, n=1, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.st.radio.sent) >= n:
                return self.st.radio.sent
            time.sleep(0.05)
        return self.st.radio.sent

    def hear(self):
        self.st._received(b"<\xff\x01N0TEST-7>APLRT1,WIDE1-1:>trail", -100.0, 2.0, 0)


class TestListenBeforeTalk(LbtCase):
    def test_a_free_channel_needs_two_free_checks_in_a_row(self):
        self.hear()
        self.assertEqual(len(self.wait_sent()), 1)
        self.assertEqual(self.st.radio.cad_checks, 2)

    def test_a_busy_channel_waits_and_then_sends(self):
        self.st.radio.busy_for = 2
        self.hear()
        self.assertEqual(len(self.wait_sent(timeout=8)), 1)
        self.assertEqual(self.st.radio.cad_checks, 4)              # busy, busy, free, free
        self.assertEqual(self.st.status()["counts"]["channel_busy"], 2)

    def test_a_busy_check_resets_the_free_count(self):
        self.st.radio.busy_for = 1
        self.hear()
        self.wait_sent(timeout=8)
        self.assertEqual(self.st.radio.cad_checks, 3)

    def test_a_repeat_is_given_up_when_the_channel_stays_busy(self):
        station.CAD_MAX_WAIT_S = 0.6
        self.st.radio.busy_for = 10_000
        self.hear()
        time.sleep(1.5)
        self.assertEqual(self.st.radio.sent, [])
        self.assertGreaterEqual(self.st.status()["counts"]["gave_up"], 1)

    def test_a_beacon_waits_and_is_tried_again_later(self):
        station.CAD_MAX_WAIT_S = 0.6
        self.st.radio.busy_for = 10_000
        before = time.time()
        self.st._beacon(manual=True)
        self.assertEqual(self.st.radio.sent, [])
        self.assertIn("Channel busy", self.st.beacon_note)
        self.assertGreaterEqual(self.st.beacon_due, before + 29)   # tried again in 30 s
        self.st.radio.busy_for = 0
        self.st._beacon(manual=True)
        self.assertEqual(len(self.st.radio.sent), 1)

    def test_the_station_keeps_listening_while_it_waits(self):
        station.CAD_MAX_WAIT_S = 1.0
        self.st.radio.busy_for = 10_000
        self.st.radio.next_rx = 0                                  # a packet is there for the next poll
        before = sum(self.st.counts[k] for k in ("rx_ok", "rx_dropped", "crc"))
        self.assertFalse(self.st._channel_free())
        self.assertGreater(sum(self.st.counts[k] for k in ("rx_ok", "rx_dropped", "crc")), before)

    def test_the_time_to_hop_includes_the_wait(self):
        """The RXT tuple's time to hop runs to the end of the transmission, so it must be worked out after the wait."""
        import lora_aprs_pi.lacf as lacf
        if not lacf.available():
            self.skipTest("run 'make' first")

        def hop_byte(text, busy):
            n = len(self.st.radio.sent)
            self.st.radio.busy_for = busy
            self.st._received(lacf.encode(text), -100.0, 2.0, 0)
            sent = self.wait_sent(n + 1, timeout=8)
            self.assertEqual(len(sent), n + 1)
            return lacf.decode(sent[-1])[1]["rxt"][0]["bytes"][3]

        free = hop_byte(b"N0TEST-7>APLRT1,WIDE1-1:>first", 0)
        waited = hop_byte(b"N0TEST-8>APLRT1,WIDE1-1:>second", 3)
        self.assertGreater(waited, free)                           # three busy checks of about 0.13 s each are in the time to hop


class TestCancelOnHear(LbtCase):
    """A repeat we are about to send is dropped when another digipeater is heard repeating the same packet first."""

    ORIGINAL = b"<\xff\x01N0TEST-7>APLRT1,WIDE1-1:>trail"
    REPEATED = b"<\xff\x01N0TEST-7>APLRT1,K6ATV-14*:>trail"

    def test_a_pending_repeat_is_cancelled(self):
        station.DIGI_DELAY_S = (1.5, 1.5)
        self.st._received(self.ORIGINAL, -100.0, 2.0, 0)
        self.assertTrue(self.st.digi_jobs)
        time.sleep(0.3)
        self.st._received(self.REPEATED, -90.0, 3.0, 0)
        time.sleep(2.2)
        self.assertEqual(self.st.radio.sent, [])
        self.assertEqual(self.st.status()["counts"]["cancelled"], 1)

    def test_without_the_other_repeat_it_goes_out(self):
        station.DIGI_DELAY_S = (0.5, 0.5)
        self.st._received(self.ORIGINAL, -100.0, 2.0, 0)
        self.assertEqual(len(self.wait_sent()), 1)
        self.assertEqual(self.st.status()["counts"]["cancelled"], 0)

    def test_the_same_packet_heard_again_unrepeated_does_not_cancel(self):
        station.DIGI_DELAY_S = (1.0, 1.0)
        self.st._received(self.ORIGINAL, -100.0, 2.0, 0)
        self.st._received(self.ORIGINAL, -95.0, 2.5, 0)            # a retransmission or an echo: no digipeater has used the path
        self.assertEqual(len(self.wait_sent(timeout=3)), 1)

    def test_another_packet_does_not_cancel(self):
        station.DIGI_DELAY_S = (1.0, 1.0)
        self.st._received(self.ORIGINAL, -100.0, 2.0, 0)
        self.st._received(b"<\xff\x01N0TEST-7>APLRT1,K6ATV-14*:>other text", -90.0, 3.0, 0)
        self.assertEqual(len(self.wait_sent(timeout=3)), 1)

    def test_a_repeat_waiting_for_the_channel_is_cancelled_and_stops_waiting(self):
        """The case seen on the air: the other digipeater is on air, so our channel check keeps finding it busy; when its repeat is
        heard to the end, ours is dropped at once and not after the whole wait."""
        station.DIGI_DELAY_S = (0, 0)
        station.CAD_MAX_WAIT_S = 6
        self.st.radio.busy_for = 10_000
        self.st._received(self.ORIGINAL, -100.0, 2.0, 0)
        time.sleep(0.5)                                            # the job is now waiting for the channel
        t = time.monotonic()
        self.st._received(self.REPEATED, -90.0, 3.0, 0)            # heard from the loop's thread in real life; here the test thread
        end = time.time() + 3
        while self.st.status()["counts"]["cancelled"] == 0 and time.time() < end:
            time.sleep(0.05)
        self.st.radio.busy_for = 0                                 # the channel is free from now on: a repeat that was not cancelled would go out
        time.sleep(0.6)
        self.assertEqual(self.st.radio.sent, [])
        self.assertEqual(self.st.status()["counts"]["cancelled"], 1)
        self.assertEqual(self.st.status()["counts"]["gave_up"], 0)
        self.assertLess(time.monotonic() - t, 3)

    def test_a_packet_we_cannot_read_is_cancelled_the_same_way(self):
        from lora_aprs_pi import lacf3
        if not lacf3.available():
            self.skipTest("run 'make' first")
        vecs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors", "vectors_hb3_passthrough_v1.txt")
        with open(vecs) as f:
            received = next(l.split()[0] for l in f if l.strip() and not l.startswith("#") and l.split()[0] != "-")
        base = bytes.fromhex(received)
        text, _ = lacf3.front(base)
        again = lacf3.relay_to_passthrough(base, text.replace(b"WIDE1-1", b"K6ATV-14*", 1), b"K6ATV-14", bytes([40, 40, 45, 20]))
        self.assertIsNotNone(again)
        station.DIGI_DELAY_S = (1.5, 1.5)
        self.st._received(base, -100.0, 2.0, 0)
        self.assertTrue(self.st.digi_jobs)
        time.sleep(0.3)
        self.st._received(again, -90.0, 3.0, 0)
        time.sleep(2.2)
        self.assertEqual(self.st.radio.sent, [])
        self.assertEqual(self.st.status()["counts"]["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
