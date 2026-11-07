"""The stations heard lately and what they said they can do (the capability bytes of LACF 3).

One entry per callsign: when it was last heard, whether directly or through a relay, the signal of
the last frame, and the newest capability announcement. An entry that has not been heard for 24 hours
goes; the table holds at most MAX entries and drops the one heard longest ago.
"""

import threading
import time

MAX = 24
EXPIRY_S = 24 * 3600
DIRECT_S = 2 * 3600                  # a station heard directly this recently is "direct"
ROLES = ["transmit only", "listens", "digipeater", "digipeater late", "iGate",
         "iGate and digipeater", "iGate and digipeater late", "reserved"]
BW_KHZ = [62.5, 125, 250, 500]      # capability BW code, spec 12
CR = ["4/5", "4/6", "4/7", "4/8"]


def describe(cap):
    """The capability fields as readable values."""
    return {
        "role": ROLES[cap["role"]],
        # the field is the newest model the station decodes plus one; 0 = plain content only
        "newest_model": cap["model"] - 1 if cap["model"] else None,
        "sf": 5 + cap["sf"],
        "bw_khz": BW_KHZ[cap["bw"]],
        "cr": CR[cap["cr"]],
        "power_dbm": cap["power"] or None,
        "preset": cap["preset"],
    }


class Neighbours:
    """direct means a frame came straight from the station within DIRECT_S. A frame heard through a relay keeps the
    time it was heard but never replaces that, and carries the relay's signal, not the station's, so it is not stored
    as the station's signal."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_call = {}

    def heard(self, call, direct, rssi, snr, capabilities=None, now=None):
        """Note a frame from call. capabilities is the parsed field dict of a capability block, or None."""
        now = time.time() if now is None else now
        call = call.upper()
        with self._lock:
            self._expire(now)
            e = self._by_call.get(call)
            if e is None:
                if len(self._by_call) >= MAX:
                    oldest = min(self._by_call, key=lambda c: self._by_call[c]["last"])
                    del self._by_call[oldest]
                e = self._by_call[call] = {"call": call, "capabilities": None, "cap_time": None, "cap_direct": None,
                                           "direct_time": None, "rssi": None, "snr": None}
            e["last"] = now
            if direct:
                e.update(direct_time=now, rssi=rssi, snr=snr)
            if capabilities is not None:
                e.update(capabilities=describe(capabilities), cap_time=now, cap_direct=bool(direct))

    def _expire(self, now):
        for c in [c for c, e in self._by_call.items() if now - e["last"] > EXPIRY_S]:
            del self._by_call[c]

    def table(self, now=None):
        """Newest first. direct is True when the station was heard directly within DIRECT_S; rssi and snr are those of
        its latest direct frame (None if it has only been heard through relays)."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            rows = []
            for e in self._by_call.values():
                row = dict(e)
                row["direct"] = e["direct_time"] is not None and now - e["direct_time"] <= DIRECT_S
                rows.append(row)
            return sorted(rows, key=lambda r: -r["last"])
