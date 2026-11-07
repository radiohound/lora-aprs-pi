"""Smart Beaconing for a moving tracker.

This uses the methods of SmartBeaconing, the algorithm devised by Tony Arnerich, KD7TA, and
Steve Bragg, KA9MVA, for the HamHUD, and used by most APRS trackers since. The rules and the
standard settings are theirs; this code is written fresh from the published description.

From the GPS speed and heading:
  - stopped or slow (below the low speed): a beacon every "slow rate"
  - fast (above the high speed): a beacon every "fast rate"
  - in between: fast rate x high speed / speed, so the beacons come about
    every same distance travelled
  - a turn sends one at once ("corner pegging") when the heading has changed
    by more than  turn angle + turn slope / speed  (a tighter angle at speed,
    a looser one when slow, so stop-and-go traffic doesn't set it off),
    but never sooner than the minimum turn time after the last beacon.
"""

MPH_PER_KNOT = 1.15078


def rate_s(bc, speed_mph):
    """Seconds between beacons at this speed."""
    slow, fast = bc["sb_slow_min"] * 60, bc["sb_fast_s"]
    low, high = bc["sb_low_mph"], bc["sb_high_mph"]
    if speed_mph is None or speed_mph < low:
        return slow
    if speed_mph >= high:
        return fast
    return min(slow, fast * high / speed_mph)


def turn_threshold_deg(bc, speed_mph):
    return bc["sb_turn_min_deg"] + bc["sb_turn_slope"] / max(speed_mph, 1.0)


def heading_change(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


class SmartBeacon:
    def __init__(self):
        self.last_time = None
        self.last_course = None
        self.reason = None

    def due(self, bc, now, speed_mph, course):
        """True when a beacon should go now. speed_mph/course are None without a moving fix."""
        if self.last_time is None:
            self.reason = "first beacon"
            return True
        since = now - self.last_time
        if since >= rate_s(bc, speed_mph):
            self.reason = "time"
            return True
        if (speed_mph is not None and speed_mph >= bc["sb_low_mph"] and course is not None
                and self.last_course is not None and since >= bc["sb_turn_time_s"]
                and heading_change(course, self.last_course) > turn_threshold_deg(bc, speed_mph)):
            self.reason = "turn"
            return True
        return False

    def sent(self, now, course):
        self.last_time = now
        if course is not None:
            self.last_course = course

    def next_in_s(self, bc, now, speed_mph):
        if self.last_time is None:
            return 0
        return max(0, round(self.last_time + rate_s(bc, speed_mph) - now))
