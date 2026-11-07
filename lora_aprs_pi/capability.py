"""The capability block of LACF 3 (spec 12): what this station is and what it can decode, sent in four bytes
after V on some of its own frames.

Schedule (spec 12, SHOULD): the first frame after start-up, then every 8th frame but at most once an hour,
at least once every 6 hours, and at once when the settings it describes change.
"""

EVERY_FRAMES = 8
MIN_GAP_S = 3600
MAX_GAP_S = 6 * 3600
NEWEST_MODEL = 0                   # the newest PPM model this build decodes; the field carries it plus one
ROLE_LISTENS, ROLE_DIGI, ROLE_IGATE, ROLE_IGATE_DIGI = 1, 2, 4, 5
PRESETS = {433.775: 2, 439.9125: 3, 434.855: 4}
BW_CODE = {62.5: 0, 125.0: 1, 250.0: 2, 500.0: 3}


def role(cfg):
    digi = cfg["digi"]["mode"] != "off"
    igate = bool(cfg["igate"]["enabled"])
    if igate and digi:
        return ROLE_IGATE_DIGI
    if igate:
        return ROLE_IGATE
    return ROLE_DIGI if digi else ROLE_LISTENS


def fields(cfg):
    """The seven field codes for lacf3.cap_pack, from the station's settings."""
    rd = cfg["radio"]
    return (role(cfg), NEWEST_MODEL + 1, rd["sf"] - 5, BW_CODE.get(float(rd["bw_khz"]), 1), rd["cr"] - 5,
            max(1, min(63, int(rd["power_dbm"]))), PRESETS.get(round(float(rd["freq_mhz"]), 4), 0))


class Schedule:
    """Decides which of our own type 03 frames carry the capability bytes."""

    def __init__(self):
        self.count = 0              # frames sent since the last announcement
        self.last_time = None
        self.last_bytes = None

    def due(self, cap_bytes, now):
        """True when this frame should carry cap_bytes (call once per frame about to be sent)."""
        if self.last_time is None or cap_bytes != self.last_bytes:
            return True
        age = now - self.last_time
        return age >= MAX_GAP_S or (self.count + 1 >= EVERY_FRAMES and age >= MIN_GAP_S)

    def sent(self, with_caps, cap_bytes, now):
        """Note a type 03 frame sent."""
        if with_caps:
            self.count, self.last_time, self.last_bytes = 0, now, cap_bytes
        else:
            self.count += 1
