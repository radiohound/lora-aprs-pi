"""Auto reduced power: lower the beacon power while a digipeater reports a strong signal.

When this station's LACF beacon is repeated by an LACF digipeater, the repeat
carries the digipeater's RXT report of how well it heard us. As soon as a
report arrives, the power for the next beacon is worked out from the best
report so far for that beacon, measured as the margin over what the receiver
needs at this spreading factor:

  margin >= 13 dB      2 dB lower than that beacon
  margin 10 to 13 dB   unchanged
  margin < 10 dB       up, by the shortfall plus 2 dB (at most 6 dB at a time)
  no report for 2 LACF beacons in a row: straight back to full power

The power never goes above the power set on the Settings page, nor below
10 dBm at the antenna. Only beacons sent as LACF with a path can be judged,
since only LACF repeats carry a report; standard beacons leave it unchanged.
The level is saved, so a restart carries on where it left off.
"""

import json
import math
import os
import time

TARGET_MARGIN_DB = 10
STEP_DOWN_DB = 2
MAX_STEP_UP_DB = 6
MISSES_TO_RESET = 2
FLOOR_DBM = 10
KEEP_SAVED_S = 6 * 3600          # a saved level older than this is not trusted after a restart

# What the receiver needs at BW 125 kHz (SX126x): demodulation SNR limit and sensitivity.
REQUIRED_SNR = {7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20}
SENSITIVITY_125 = {7: -124, 8: -127, 9: -130, 10: -133, 11: -135.5, 12: -137}


def margin_db(rssi, snr, sf, bw_khz=125):
    """How many dB above the receiver's limit a report is.

    SNR is the better measure for weak signals; above about +5 dB it stops
    growing, so a strong signal is judged by its RSSI over the sensitivity."""
    if snr >= 5:
        sens = SENSITIVITY_125[sf] + 10 * math.log10(bw_khz / 125.0)
        return rssi - sens
    return snr - REQUIRED_SNR[sf]


class AutoPower:
    def __init__(self, max_dbm, state_path=None, profile_min_dbm=-1):
        self.max_dbm = max_dbm
        self.level = max_dbm
        self.base = max_dbm            # the power the beacon awaiting reports went out at
        self.reports = []              # (margin, digi) for that beacon
        self.awaiting = None           # its information field
        self.misses = 0
        self.profile_min = profile_min_dbm
        self.state_path = state_path
        self.note = "Full power until a digipeater reports back"
        self._load()

    # ---- keeping the level across restarts ----

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if st.get("max_dbm") == self.max_dbm and time.time() - st.get("time", 0) < KEEP_SAVED_S:
                self.level = int(st["level"])
                self.note = st.get("note") or "Carried on from before the restart"
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self):
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"level": self.level, "max_dbm": self.max_dbm, "note": self.note,
                           "time": time.time()}, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- the rules ----

    def set_max(self, max_dbm):
        if max_dbm != self.max_dbm:
            self.max_dbm = max_dbm
            self.level = self.base = max_dbm
            self.misses = 0
            self.note = "Full power until a digipeater reports back"
            self._save()

    def floor(self):
        return min(self.max_dbm, max(FLOOR_DBM, self.profile_min))

    def report(self, info, digi, rssi, snr, sf, bw_khz):
        """A digipeater's report on one of our beacons. Returns the new level when it changed it."""
        if self.awaiting is None or info != self.awaiting:
            return None
        self.reports.append((margin_db(rssi, snr, sf, bw_khz), digi))
        margin, best = max(self.reports)
        if margin >= TARGET_MARGIN_DB + 3:
            level = max(self.floor(), self.base - STEP_DOWN_DB)
        elif margin < TARGET_MARGIN_DB:
            level = min(self.max_dbm, self.base + min(MAX_STEP_UP_DB, TARGET_MARGIN_DB - margin + 2))
        else:
            level = self.base
        self.level = int(round(level))
        self.misses = 0
        if self.level < self.base:
            how = f"next beacon {self.base - self.level} dB lower"
        elif self.level > self.base:
            how = f"next beacon {self.level - self.base} dB higher"
        else:
            how = "next beacon at the same power"
        self.note = f"{best} heard the last beacon with {margin:.0f} dB to spare: {how}"
        self._save()
        return self.level

    def next_level(self, profile_min_dbm=None):
        """The power for the beacon about to go out (dBm at the antenna)."""
        if profile_min_dbm is not None:
            self.profile_min = profile_min_dbm
        if self.awaiting is not None and not self.reports:
            self.misses += 1
            if self.misses >= MISSES_TO_RESET:
                self.level = self.max_dbm
                self.note = f"No digipeater report for {self.misses} beacons: back to full power"
            else:
                self.note = "No digipeater report for the last beacon"
            self._save()
        self.level = max(min(self.level, self.max_dbm), min(self.floor(), self.max_dbm))
        self.awaiting, self.reports = None, []
        return self.level

    def sent(self, info, fmt, path):
        """Note a beacon just sent at the current level. Only LACF beacons with a path can get a report."""
        self.base = self.level
        self.awaiting = info if (fmt == "lacf" and path) else None
        self.reports = []
