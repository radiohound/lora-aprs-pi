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

from . import lacf

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
    """The frame to transmit: a standard frame as text, an LACF frame relayed per spec 11."""
    if fmt == "lacf":
        return lacf.relay_to(received_frame, new_tnc2.encode(), mycall.encode(), tuple4)
    frame = b"<\xff\x01" + new_tnc2.encode()
    return frame if len(frame) <= 255 else None
