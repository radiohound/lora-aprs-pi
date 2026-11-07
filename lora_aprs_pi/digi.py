"""Digipeater: decides whether to repeat a packet, and builds the repeated frame.

Modes (LoRa APRS conventions, as in Ricardo's firmware):
  off       never repeat
  fill-in   repeat WIDE1-1, plus any packet addressed to this station's callsign
  wide      also repeat WIDE2-1 and WIDE2-2 (wide-area digipeater)

The path rule is the one spec 11 uses for LACF relays, so standard and LACF
frames are treated alike: the first hop not yet used decides.
  WIDE1-1 -> MYCALL*        WIDE2-1 -> MYCALL*        WIDE2-2 -> MYCALL*,WIDE2-1
  MYCALL  -> MYCALL*
A packet whose path breaks the WIDEn-N rule (1 <= N <= n <= 7) is never repeated.
NOGATE and RFONLY are no reason not to repeat: they mean "keep it on RF".

LACF frames are relayed by copying their content byte for byte and adding this
station's RXT tuple (signal strength, SNR, frequency offset, time to hop);
standard frames are repeated as standard text.
"""

import math
import re

from . import lacf, lacf3

MODES = ("off", "fill-in", "wide")
MAX_HOPS = 8
_WIDE = re.compile(r"^WIDE(\d)-(\d+)$")

# RXT tuple scales (spec 11 / F4MLV's rxt branch, the same as the iGate's rxt_scale.h)
RXT_MAX = 89


def _clamp(v):
    return max(0, min(RXT_MAX, int(round(v))))


def rxt_rssi(dbm):
    return _clamp(dbm + 130)


def rxt_snr(db):
    return _clamp((max(-24.0, min(20.0, db)) + 24) / 0.5)


def rxt_freq_offset(hz):
    n = max(-2500.0, min(2500.0, float(hz))) / 2500.0
    return _clamp(math.copysign(math.sqrt(abs(n)), n) * 45 + 45)


def rxt_time_to_hop(ms, sf, bw_khz):
    unit = (2 ** sf) / bw_khz * 10.0                  # ten symbol times, in ms
    return _clamp(math.log(max(1.0, ms / unit + 1.0)) / math.log(1.08))


def rxt_signal(tuple4):
    """A relay's report back in dBm and dB: (RSSI, SNR), or None when the relay gave none: a value of 90 or more is
    "not measured" (93) or unknown (spec 12), not a signal."""
    if tuple4[0] >= 90 or tuple4[1] >= 90:
        return None
    return tuple4[0] - 130, tuple4[1] * 0.5 - 24


def rxt_tuple(rssi, snr, freq_offset_hz, tth_ms, sf, bw_khz):
    return bytes([rxt_rssi(rssi), rxt_snr(snr), rxt_freq_offset(freq_offset_hz or 0),
                  rxt_time_to_hop(tth_ms, sf, bw_khz)])


def _wide_ok(hops):
    for h in hops:
        m = _WIDE.match(h.rstrip("*"))
        if m and not 1 <= int(m.group(2)) <= int(m.group(1)) <= 7:
            return False
    return True


def new_path(path, mycall, mode):
    """The path to repeat with, or (None, reason) when the packet isn't ours to repeat."""
    if mode not in ("fill-in", "wide"):
        return None, "digipeater off"
    hops = [h for h in path.split(",") if h]
    if not _wide_ok(hops):
        return None, "path breaks the WIDEn-N rule"
    used = max((i for i, h in enumerate(hops) if h.endswith("*")), default=-1)
    if any(h.rstrip("*").upper() == mycall for h in hops[:used + 1]):
        return None, "already repeated by this station"
    k = used + 1
    if k >= len(hops):
        return None, "no hops left"
    hop = hops[k].upper()
    if hop == mycall:
        repl = [f"{mycall}*"]
    elif hop == "WIDE1-1":
        repl = [f"{mycall}*"]
    elif mode == "wide" and hop == "WIDE2-1":
        repl = [f"{mycall}*"]
    elif mode == "wide" and hop == "WIDE2-2":
        repl = [f"{mycall}*", "WIDE2-1"]
    else:
        return None, f"not a hop this digipeater serves ({hops[k]})"
    out = hops[:k] + repl + hops[k + 1:]
    if len(out) > MAX_HOPS:
        return None, "path too long"
    return ",".join(out), None


def callsign_valid(call):
    """The iGate firmware's callsign test (LoRa APRS iGate, Utils::callsignIsValid), the same rules."""
    if call == "WLNK-1":
        return True
    total = len(call)
    if total < 4:
        return False
    hyphen = call.find("-")
    base = hyphen if hyphen > 0 else total
    if hyphen > 0:                                       # SSID: one or two digits, not starting with 0
        if hyphen < 4:
            return False
        ssid = call[hyphen + 1:]
        if not ssid or len(ssid) > 2 or "-" in ssid or (len(ssid) == 2 and ssid[0] == "0") or not all(c in "0123456789" for c in ssid):
            return False
    if base < 4 or base > 6:
        return False
    alpha = lambda c: ("A" <= c <= "Z") or ("a" <= c <= "z")
    digit = lambda c: "0" <= c <= "9"
    padded = base == 4 and alpha(call[0]) and digit(call[1]) and alpha(call[2]) and alpha(call[3])     # A0AA is read as _A0AA
    c0, c1, c2, c3 = (" ", call[0], call[1], call[2]) if padded else (call[0], call[1], call[2], call[3])
    if not digit(c2) or not alpha(c3):
        if c0 != "R" and not digit(c1) and not alpha(c2):
            return False
    ok = ((alpha(c0) or digit(c0) or c0 == " ") and alpha(c1)) or (alpha(c0) and digit(c1)) or \
         (c0 == "R" and base == 6 and digit(c1) and alpha(c2) and alpha(c3) and alpha(call[4]))
    return bool(ok) and all(alpha(c) for c in call[4:base])


def unread_refused(front):
    """Why a frame we cannot read (its front is "SRC>DST,PATH:") must not be relayed, as the iGate's digipeater would not
    repeat it either: NOGATE in the path, or a sender that is not a valid callsign. None if it may be relayed."""
    head = front.split(":", 1)[0]
    source = head.partition(">")[0]
    if "NOGATE" in head.upper():
        return "NOGATE in the path"
    if not callsign_valid(source):
        return "sender is not a valid callsign"
    return None


def decide(tnc2, mycall, mode):
    """For a received TNC2 packet: (packet to send, None) or (None, reason)."""
    head, sep, info = tnc2.partition(":")
    if not sep or ">" not in head:
        return None, "not an APRS packet"
    src, _, rest = head.partition(">")
    dest, _, path = rest.partition(",")
    if src.upper() == mycall:
        return None, "our own packet"
    path2, reason = new_path(path, mycall, mode)
    if path2 is None:
        return None, reason
    return f"{src}>{dest},{path2}:{info}", None


def build_frame(received_frame, fmt, new_tnc2, mycall, tuple4):
    """The frame to transmit: a standard frame as text, an LACF frame relayed per spec 11 (type 02), spec 12
    (type 03, lacf3) or, for a type 03 frame whose model we do not hold, relayed without the model (lacf3-pass)."""
    if fmt == "lacf":
        return lacf.relay_to(received_frame, new_tnc2.encode(), mycall.encode(), tuple4)
    if fmt == "lacf3":
        return lacf3.relay_to(received_frame, new_tnc2.encode(), mycall.encode(), tuple4)
    if fmt == "lacf3-pass":
        return lacf3.relay_to_passthrough(received_frame, new_tnc2.encode(), mycall.encode(), tuple4)
    frame = b"<\xff\x01" + new_tnc2.encode()
    return frame if len(frame) <= 255 else None
