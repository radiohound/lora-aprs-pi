"""LoRa APRS frames on the air.

Standard:  3C FF 01 + the TNC2 text (what every LoRa APRS station sends)
LACF:      3C FF 02 + the compact frame of spec 11 (radiohound/lora-aprs-ideas)

Any other 3C FF xx frame is dropped and never shown as text, as spec 11 asks.
"""

from . import lacf

PLAIN = b"<\xff\x01"
LACF = lacf.PREFIX


def decode(raw):
    """What a received frame holds: {'format', 'text' (bytes or None), 'error', 'lacf'}."""
    if raw.startswith(PLAIN):
        text = raw[3:].rstrip(b"\r\n\x00")
        if b">" not in text or b":" not in text:
            return {"format": "standard", "text": None, "error": "not an APRS packet", "lacf": None}
        return {"format": "standard", "text": text, "error": None, "lacf": None}
    if raw.startswith(LACF):
        if not lacf.available():
            return {"format": "lacf", "text": None, "error": "LACF codec not built", "lacf": None}
        try:
            text, details = lacf.decode(raw)
        except lacf.LacfError as exc:
            return {"format": "lacf", "text": None, "error": f"dropped ({exc})", "lacf": None}
        return {"format": "lacf", "text": text, "error": None, "lacf": details}
    if raw.startswith(b"<\xff"):
        return {"format": "unknown", "text": None,
                "error": f"unknown frame type 0x{raw[2]:02X}" if len(raw) > 2 else "short frame", "lacf": None}
    return {"format": "other", "text": None, "error": "not a LoRa APRS frame", "lacf": None}


def encode(tnc2, fmt):
    """The frame to send for a TNC2 packet, and which format was used.

    fmt is 'standard' or 'lacf'. LACF falls back to a standard frame only when
    the codec can't encode the packet. A packet with a broken WIDEn-N path is
    refused outright, since spec 11 says it must not be sent in any form.
    """
    if fmt == "lacf" and lacf.available():
        if not lacf.path_ok(tnc2):
            raise ValueError("the path has a WIDEn-N that breaks the APRS rule")
        frame = lacf.encode(tnc2)
        if frame:
            return frame, "lacf"
    frame = PLAIN + tnc2
    if len(frame) > 255:
        raise ValueError("packet too long for one LoRa frame")
    return frame, "standard"
