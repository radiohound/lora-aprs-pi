"""LACF 3.0.0-dev.1 frames (type byte 3C FF 03, proposal of lora-aprs-ideas spec 12), through the C reference codec.

Same library as lacf.py (lacf/liblacf.so). Type 02 frames stay with lacf.py; this module is the second decoder, picked by the type byte.
"""

import ctypes
import threading

from . import lacf
from .lacf import LacfError, MAX_FRAME, _Rxt

PREFIX = b"<\xff\x03"
ROLES = ["transmit only", "listens", "digipeater", "digipeater late", "iGate",
         "iGate and digipeater", "iGate and digipeater late", "reserved"]


class _Info(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("cc", ctypes.c_int),
        ("ver", ctypes.c_int),
        ("npath", ctypes.c_int),
        ("frame_len", ctypes.c_size_t),
        ("content_len", ctypes.c_size_t),
        ("payload_len", ctypes.c_size_t),
        ("front_len", ctypes.c_size_t),
        ("path_start", ctypes.c_size_t),
        ("content_start", ctypes.c_size_t),
        ("rxt", _Rxt),
        ("has_rxt", ctypes.c_int),
        ("has_cap", ctypes.c_int),
        ("cap", ctypes.c_uint8 * 4),
    ]


_ready = None
_load_error = None
_lock = threading.Lock()


def _load():
    global _ready, _load_error
    if _ready is not None or _load_error is not None:
        return _ready
    lib = lacf._load()
    if lib is None:
        _load_error = lacf.load_error()
        return None
    try:
        lib.hb3_load_list.argtypes = [ctypes.c_char_p]
        lib.hb3_load_list.restype = ctypes.c_int
        lib.hb3_reason.argtypes = [ctypes.c_int]
        lib.hb3_reason.restype = ctypes.c_char_p
        lib.hb3_path_ok.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.hb3_path_ok.restype = ctypes.c_int
        lib.hb3_encode_cap.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(_Info)]
        lib.hb3_encode_cap.restype = ctypes.c_size_t
        lib.hb3_decode.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                   ctypes.c_size_t, ctypes.POINTER(_Info)]
        lib.hb3_decode.restype = ctypes.c_int
        lib.hb3_decode_front.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                         ctypes.c_size_t, ctypes.POINTER(_Info)]
        lib.hb3_decode_front.restype = ctypes.c_int
        lib.hb3_relay_to.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                                     ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.hb3_relay_to.restype = ctypes.c_size_t
        lib.hb3_relay_to_passthrough.argtypes = lib.hb3_relay_to.argtypes
        lib.hb3_relay_to_passthrough.restype = ctypes.c_size_t
        lib.hb3_cap_pack.argtypes = [ctypes.POINTER(ctypes.c_int * 7), ctypes.c_char_p]
        lib.hb3_cap_pack.restype = ctypes.c_int
        lib.hb3_cap_parse.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int * 7)]
        lib.hb3_cap_parse.restype = None
    except AttributeError as exc:
        _load_error = f"LACF library has no type 03 codec ({exc}); run 'make'"
        return None
    if lib.hb3_load_list(lacf.LIST_PATH.encode()) <= 0:
        _load_error = f"LACF destination list not loaded from {lacf.LIST_PATH}"
        return None
    _ready = lib
    return _ready


def available():
    """True when the codec library has the type 03 decoder and it is ready."""
    return _load() is not None


def load_error():
    _load()
    return _load_error


def _need():
    lib = _load()
    if lib is None:
        raise LacfError(_load_error)
    return lib


ERR_FRONT, ERR_VERSION = -4, -6      # HB3_ERR_FRONT (address or path; an unassigned destination code), HB3_ERR_VERSION


def _error(lib, code):
    exc = LacfError(lib.hb3_reason(code).decode())
    exc.code = code
    return exc


def cap_pack(role, model, sf, bw, cr, power, preset):
    """The four capability bytes from the field codes (role 0-7, model decoded 0-31, sf code 0-7 = SF5..SF12,
    bw code 0-3, cr code 0-3, power 0-63 dBm, preset 0-31). Raises ValueError for a field out of range."""
    lib = _need()
    f = (ctypes.c_int * 7)(role, model, sf, bw, cr, power, preset)
    out = ctypes.create_string_buffer(4)
    if not lib.hb3_cap_pack(ctypes.byref(f), out):
        raise ValueError("capability field out of range")
    return out.raw[:4]


def cap_parse(b):
    """Four capability bytes -> dict of the field codes."""
    lib = _need()
    f = (ctypes.c_int * 7)()
    lib.hb3_cap_parse(bytes(b), ctypes.byref(f))
    return dict(zip(("role", "model", "sf", "bw", "cr", "power", "preset"), list(f)))


def path_ok(tnc2):
    lib = _need()
    with lacf._lock:
        return bool(lib.hb3_path_ok(tnc2, len(tnc2)))


def encode(tnc2, cap_bytes=None):
    """TNC2 bytes -> type 03 frame bytes (with the four capability bytes when given), or None when it can't be encoded."""
    lib = _need()
    out = ctypes.create_string_buffer(MAX_FRAME + 1)
    info = _Info()
    with lacf._lock:
        n = lib.hb3_encode_cap(tnc2, len(tnc2), None, bytes(cap_bytes) if cap_bytes else None,
                               out, MAX_FRAME, ctypes.byref(info))
    return out.raw[:n] if n else None


def _details(info):
    return {
        "kind": info.kind,
        "compressed": info.kind >= 2,
        "model": info.ver,
        "relay_tuples": info.rxt.n if info.has_rxt else 0,
        "rxt": [{"index": info.rxt.idx[i], "bytes": list(info.rxt.t[i])}
                for i in range(info.rxt.n)] if info.has_rxt else [],
        "capabilities": cap_parse(bytes(info.cap)) if info.has_cap else None,
        "capability_hex": bytes(info.cap).hex() if info.has_cap else None,   # a string: the details go to the web page as JSON
    }


def decode(frame):
    """Type 03 frame bytes -> (TNC2 bytes, details dict). Raises LacfError if the frame is dropped
    (including a model this station does not hold: use front() and relay_passthrough() for those)."""
    lib = _need()
    out = ctypes.create_string_buffer(1024)
    info = _Info()
    with lacf._lock:
        n = lib.hb3_decode(frame, len(frame), out, len(out), ctypes.byref(info))
        if n < 0:
            raise _error(lib, n)
    return out.raw[:n], _details(info)


def front(frame):
    """Structure and CRC only, whatever the model: (b"SRC>DST,PATH:", details). Raises LacfError if damaged.
    The text is only the front; it must not be shown, gated or used."""
    lib = _need()
    out = ctypes.create_string_buffer(1024)
    info = _Info()
    with lacf._lock:
        n = lib.hb3_decode_front(frame, len(frame), out, len(out), ctypes.byref(info))
        if n < 0:
            raise _error(lib, n)
    details = _details(info)
    # the content field as sent (what a duplicate is judged on), without the path, the RXT block or the CRC around it
    details["content"] = bytes(frame[info.content_start:info.content_start + info.content_len])
    return out.raw[:n], details


def relay_to(frame, new_tnc2, relay_call, tuple4):
    """Relay a frame this station can decode, with the path the digipeater chose. None if it must not be relayed."""
    lib = _need()
    out = ctypes.create_string_buffer(MAX_FRAME + 1)
    with lacf._lock:
        n = lib.hb3_relay_to(frame, len(frame), new_tnc2, len(new_tnc2), relay_call, bytes(tuple4), out, MAX_FRAME)
    return out.raw[:n] if n else None


def relay_to_passthrough(frame, new_tnc2, relay_call, tuple4):
    """Relay without the model: new_tnc2 is front()'s text with the new path. The content is copied byte for byte."""
    lib = _need()
    out = ctypes.create_string_buffer(MAX_FRAME + 1)
    with lacf._lock:
        n = lib.hb3_relay_to_passthrough(frame, len(frame), new_tnc2, len(new_tnc2), relay_call,
                                         bytes(tuple4), out, MAX_FRAME)
    return out.raw[:n] if n else None
