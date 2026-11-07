"""SmartBeaconing: rate from speed, corner pegging, and course/speed in the beacon."""

import os
import tempfile
import time
import unittest

from lora_aprs_pi import aprs, config, frames, gps, smartbeacon
from lora_aprs_pi.station import Station

BC = dict(config.DEFAULTS["beacon"], smart_beaconing=True, use_gps=True)


class TestRules(unittest.TestCase):
    def test_rate(self):
        r = smartbeacon.rate_s
        self.assertEqual(r(BC, None), 30 * 60)          # no moving fix
        self.assertEqual(r(BC, 2), 30 * 60)              # below the low speed
        self.assertEqual(r(BC, 60), 60)                  # at the high speed
        self.assertEqual(r(BC, 80), 60)
        self.assertAlmostEqual(r(BC, 30), 120)           # half the high speed: twice the gap

    def test_turns(self):
        sb = smartbeacon.SmartBeacon()
        self.assertTrue(sb.due(BC, 0, 30, 90))           # first beacon
        sb.sent(0, 90)
        self.assertFalse(sb.due(BC, 10, 30, 180))        # a turn, but sooner than the minimum gap
        self.assertFalse(sb.due(BC, 20, 30, 110))        # 20 degrees: under 25 + 255/30 = 33.5
        self.assertTrue(sb.due(BC, 20, 30, 130))         # 40 degrees: a turn
        self.assertEqual(sb.reason, "turn")
        sb.sent(20, 130)
        self.assertFalse(sb.due(BC, 40, 2, 300))         # stopped: turns don't count
        self.assertTrue(sb.due(BC, 20 + 121, 30, 130))   # time, at 30 mph (120 s)
        self.assertEqual(sb.reason, "time")
        self.assertEqual(smartbeacon.heading_change(350, 10), 20)

    def test_course_speed_in_packet(self):
        info = aprs.position_info(37.0193, -121.618, "/>", "hi", None, 0.4, 30.4)
        self.assertEqual(info, "!3701.16N/12137.08W>360/030hi")
        self.assertEqual(aprs.position_info(37, -121, "/>", "", None, 270, 12), "!3700.00N/12100.00W>270/012")
        self.assertNotIn("/0", aprs.position_info(37, -121, "/>", "", None, None, None)[19:])

    def test_settings(self):
        cfg = config.load("/nonexistent")
        cfg["beacon"].update(smart_beaconing=True, use_gps=False)
        self.assertIn("beacon.smart_beaconing", config.check(cfg))
        cfg["beacon"].update(use_gps=True, sb_low_mph=20, sb_high_mph=15)
        self.assertIn("beacon.sb_high_mph", config.check(cfg))

    def test_gps_reads_speed_and_course(self):
        fix = gps.new_fix()
        gps.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A", fix)
        self.assertEqual((fix["speed_kn"], fix["course"]), (22.4, 84.4))


class FakeGps:
    def __init__(self):
        self.speed_kn, self.course = 26.0, 90.0
        self.device, self.error = "/dev/fake", None

    def fix(self):
        return {"valid": True, "fresh": True, "lat": 37.02, "lon": -121.62, "alt_m": 60.0, "sats": 9,
                "speed_kn": self.speed_kn, "course": self.course, "hdop": 0.9, "fix_type": 3, "in_view": 12,
                "state": "good fix"}

    def stop(self):
        pass


class TestDrive(unittest.TestCase):
    def test_simulated_drive(self):
        d = tempfile.TemporaryDirectory()
        path = os.path.join(d.name, "config.json")
        cfg = config.load(path)
        cfg["station"]["callsign"] = "K6ATV-7"
        cfg["beacon"].update(enabled=True, use_gps=True, smart_beaconing=True, symbol="/>", format="standard",
                             sb_turn_time_s=5)
        cfg["gps"]["device"] = "/dev/fake"
        config.save(path, cfg)
        st = Station(path, simulate=True)
        st._start_gps = lambda: None
        st.gps = FakeGps()
        st.beacon_due = 0
        st.start()
        try:
            end = time.time() + 5
            while time.time() < end and not st.radio:
                time.sleep(0.05)
            self.assertTrue(self._wait(lambda: st.smart.last_time is not None))
            first = frames.decode(st.radio.sent[-1])["text"]
            self.assertIn(b">090/026", first)                    # course and speed in the beacon
            n = len(st.radio.sent)
            st.beacon_due = 0                                     # skip the minimum gap for the test
            st.smart.last_time -= 10
            st.gps.course = 180.0                                 # a sharp turn at 30 mph
            self.assertTrue(self._wait(lambda: len(st.radio.sent) > n))
            self.assertEqual(st.smart.reason, "turn")
            s = st.status()["beacon"]["smart"]
            self.assertTrue(s["on"])
            self.assertAlmostEqual(s["speed_mph"], 29.9, delta=0.2)
        finally:
            st.stop()
            d.cleanup()

    def _wait(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False


if __name__ == "__main__":
    unittest.main()
