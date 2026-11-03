"""LACF frames (spec 11 of lora-aprs-ideas), through the C reference codec.

The codec lives in lacf/liblacf.so, built by `make` from the vendored C
reference. If the library is missing the station still runs; it just can't
decode or send LACF frames, and the web page says so.
"""

import ctypes
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_PATH = os.path.join(ROOT, "lacf", "liblacf.so")
LIST_PATH = os.path.join(ROOT, "lacf", "tocall_set1.txt")

PREFIX = b"<\xff\x02"
MAX_FRAME = 255
MAX_TUPLES = 7


class LacfError(Exception):
    """A frame was dropped; the message is the codec's reason."""


class _Rxt(ctypes.Structure):
    _fields_ = [
        ("n", ctypes.c_int),
        ("idx", ctypes.c_uint8 * MAX_TUPLES),
        ("t", (ctypes.c_uint8 * 4) * MAX_TUPLES),
    ]


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
    ]


_lib = None
_load_error = None
_lock = threading.Lock()   # the C codec keeps the destination list in a static table


def _load():
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    try:
        lib = ctypes.CDLL(LIB_PATH)
    except OSError as exc:
        _load_error = f"LACF library not built ({exc}); run 'make'"
        return None

    lib.hb_load_list.argtypes = [ctypes.c_char_p]
    lib.hb_load_list.restype = ctypes.c_int
    lib.hb_reason.argtypes = [ctypes.c_int]
    lib.hb_reason.restype = ctypes.c_char_p
    lib.hb_encode.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
                              ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(_Info)]
    lib.hb_encode.restype = ctypes.c_size_t
    lib.hb_decode.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                              ctypes.c_size_t, ctypes.POINTER(_Info)]
    lib.hb_decode.restype = ctypes.c_int
    lib.hb_path_ok.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.hb_path_ok.restype = ctypes.c_int
    lib.hb_relay.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                             ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.hb_relay.restype = ctypes.c_size_t

    n = lib.hb_load_list(LIST_PATH.encode())
    if n != 62:
        _load_error = f"LACF destination list not loaded from {LIST_PATH}"
        return None
    _lib = lib
    return _lib


def available():
    """True when the codec is built and ready."""
    return _load() is not None


def load_error():
    _load()
    return _load_error


def _need():
    lib = _load()
    if lib is None:
        raise LacfError(_load_error)
    return lib


def path_ok(tnc2):
    """False when the path holds a WIDEn-N that breaks the APRS rule.

    Spec 11 says such a packet is not sent at all, in any format.
    """
    lib = _need()
    with _lock:
        return bool(lib.hb_path_ok(tnc2, len(tnc2)))


def encode(tnc2):
    """TNC2 bytes -> LACF frame bytes, or None if the packet can't be encoded.

    The codec picks LACF content when it is smaller and plain content (with
    its CRC-16) otherwise, so the result is always a 3C FF 02 frame.
    """
    lib = _need()
    out = ctypes.create_string_buffer(MAX_FRAME + 1)
    info = _Info()
    with _lock:
        n = lib.hb_encode(tnc2, len(tnc2), None, out, MAX_FRAME, ctypes.byref(info))
    return out.raw[:n] if n else None


def decode(frame):
    """LACF frame bytes -> (TNC2 bytes, details dict). Raises LacfError if dropped."""
    lib = _need()
    out = ctypes.create_string_buffer(1024)
    info = _Info()
    with _lock:
        n = lib.hb_decode(frame, len(frame), out, len(out), ctypes.byref(info))
        if n < 0:
            raise LacfError(lib.hb_reason(n).decode())
    details = {
        "kind": info.kind,
        "compressed": info.kind >= 2,
        "table_version": info.ver,
        "relay_tuples": info.rxt.n if info.has_rxt else 0,
    }
    return out.raw[:n], details


def relay(frame, relay_call, tuple4):
    """Spec 11 relay: rewrite the path, copy the content, append our RXT tuple."""
    lib = _need()
    out = ctypes.create_string_buffer(MAX_FRAME + 1)
    with _lock:
        n = lib.hb_relay(frame, len(frame), relay_call, bytes(tuple4), out, MAX_FRAME)
    return out.raw[:n] if n else None
