"""Auto reduced power: lower the beacon power while a digipeater reports a strong signal.

When this station's LACF beacon is repeated by an LACF digipeater, the repeat
carries the digipeater's RXT report of how well it heard us. From the best
report for each beacon, work out the margin over what the receiver needs at
this spreading factor, and step the next beacon's power:

  margin >= 13 dB      2 dB lower
  margin 10 to 13 dB   unchanged
  margin < 10 dB       up, by the shortfall plus 2 dB (at most 6 dB at a time)
  no report for 2 LACF beacons in a row: straight back to full power

The power never goes above the power set on the Settings page, nor below
10 dBm at the antenna. Only beacons sent as LACF with a path can be judged,
since only LACF repeats carry a report; standard beacons leave it unchanged.
"""

import math

TARGET_MARGIN_DB = 10
STEP_DOWN_DB = 2
MAX_STEP_UP_DB = 6
MISSES_TO_RESET = 2
FLOOR_DBM = 10

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
    def __init__(self, max_dbm):
        self.max_dbm = max_dbm
        self.level = max_dbm
        self.reports = []              # margins reported for the beacon awaiting judgement
        self.awaiting = None           # the info field of that beacon
        self.misses = 0
        self.note = "Full power until a digipeater reports back"

    def set_max(self, max_dbm):
        if max_dbm != self.max_dbm:
            self.max_dbm = max_dbm
            self.level = max_dbm
            self.misses = 0
            self.note = "Full power until a digipeater reports back"

    def floor(self, profile_min_dbm):
        return min(self.max_dbm, max(FLOOR_DBM, profile_min_dbm))

    def report(self, info, digi, rssi, snr, sf, bw_khz):
        """A digipeater's report on one of our beacons."""
        if self.awaiting is not None and info == self.awaiting:
            self.reports.append((margin_db(rssi, snr, sf, bw_khz), digi))

    def next_level(self, profile_min_dbm):
        """Judge the last LACF beacon and return the power for the next one (dBm at the antenna)."""
        if self.awaiting is None:
            return self.level
        floor = self.floor(profile_min_dbm)
        if self.reports:
            margin, digi = max(self.reports)
            self.misses = 0
            if margin >= TARGET_MARGIN_DB + 3:
                self.level = max(floor, self.level - STEP_DOWN_DB)
            elif margin < TARGET_MARGIN_DB:
                self.level = min(self.max_dbm, self.level + min(MAX_STEP_UP_DB, TARGET_MARGIN_DB - margin + 2))
            self.note = f"{digi} heard the last beacon with {margin:.0f} dB to spare"
        else:
            self.misses += 1
            if self.misses >= MISSES_TO_RESET:
                self.level = self.max_dbm
                self.note = f"No digipeater report for {self.misses} beacons: back to full power"
            else:
                self.note = "No digipeater report for the last beacon"
        self.level = int(round(self.level))
        self.awaiting, self.reports = None, []
        return self.level

    def sent(self, info, fmt, path):
        """Note a beacon just sent. Only LACF beacons with a path can get a report."""
        self.awaiting = info if (fmt == "lacf" and path) else None
        self.reports = []
