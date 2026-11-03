"""Building our own APRS packets, and reading enough of others' to show them."""

import math
import re

TOCALL = "APZLPI"     # APZ = experimental, until a device ID is registered

_TNC2 = re.compile(rb"^([A-Za-z0-9-]{1,9})>([A-Za-z0-9-]{1,9})((?:,[A-Za-z0-9*-]{1,10}){0,8}):(.*)$", re.S)


# ---- building ----

def _degmin(value, width, pos, neg):
    """Decimal degrees -> DDMM.mm / DDDMM.mm, rounded in hundredths of a minute."""
    hemi = pos if value >= 0 else neg
    hundredths = int(round(abs(value) * 6000))
    deg, rem = divmod(hundredths, 6000)
    return f"{deg:0{width}d}{rem // 100:02d}.{rem % 100:02d}{hemi}"


def position_info(lat, lon, symbol, comment="", altitude_ft=None):
    """Plain (uncompressed) position report, no timestamp, no messaging.

    Uncompressed is used on purpose: LACF carries it in its lossless native
    position block, and every LoRa APRS receiver can read it.
    """
    info = f"!{_degmin(lat, 2, 'N', 'S')}{symbol[0]}{_degmin(lon, 3, 'E', 'W')}{symbol[1]}{comment}"
    if altitude_ft is not None:
        alt = max(-99999, min(999999, int(round(altitude_ft))))
        info += f" /A={alt:06d}" if alt >= 0 else f" /A=-{-alt:05d}"
    return info


def build(source, path, info):
    """TNC2 packet bytes: SOURCE>TOCALL,PATH:INFO"""
    head = f"{source}>{TOCALL}" + (f",{path}" if path else "")
    return f"{head}:{info}".encode("utf-8")


# ---- reading ----

def _num(s):
    return float(s.replace(" ", "0"))


def _uncompressed(body):
    # DDMM.mmN/DDDMM.mmW$ (spaces allowed for ambiguity)
    m = re.match(r"(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)", body)
    if not m:
        return None
    lat = int(m.group(1)) + _num(m.group(2)) / 60
    lon = int(m.group(5)) + _num(m.group(6)) / 60
    if m.group(3) == "S":
        lat = -lat
    if m.group(7) == "W":
        lon = -lon
    return lat, lon, m.group(4) + m.group(8), body[m.end():]


def _b91(s):
    v = 0
    for ch in s:
        v = v * 91 + ord(ch) - 33
    return v


def _compressed(body):
    if len(body) < 13 or not ("!" <= body[1] <= "{"):
        return None
    sym_table, y, x, sym = body[0], body[1:5], body[5:9], body[9]
    if not (sym_table in "/\\" or sym_table.isalnum()):
        return None
    lat = 90 - _b91(y) / 380926.0
    lon = -180 + _b91(x) / 190463.0
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon, sym_table + sym, body[13:]


def parse(tnc2):
    """A dict describing a TNC2 packet, or None if it isn't one."""
    m = _TNC2.match(tnc2)
    if not m:
        return None
    src, dest, path, info = (g.decode("utf-8", "replace") for g in m.groups())
    out = {"source": src, "dest": dest, "path": path.lstrip(","), "info": info,
           "type": "other", "lat": None, "lon": None, "symbol": None, "comment": ""}
    if not info:
        return out
    dti = info[0]
    if dti in "!=/@":
        body = info[1:]
        if dti in "/@":
            body = body[7:]                       # skip the timestamp
        pos = _uncompressed(body) or _compressed(body)
        out["type"] = "position"
        if pos:
            out["lat"], out["lon"], out["symbol"], out["comment"] = pos
            out["lat"], out["lon"] = round(out["lat"], 5), round(out["lon"], 5)
    elif dti in "`'":
        out["type"] = "Mic-E position"
    elif dti == ":":
        out["type"] = "message"
        out["comment"] = info[11:] if len(info) > 11 else ""
    elif dti == ">":
        out["type"] = "status"
        out["comment"] = info[1:]
    elif dti == ";":
        out["type"] = "object"
    elif dti == "T":
        out["type"] = "telemetry"
    elif dti == "_":
        out["type"] = "weather"
    elif dti == "}":
        out["type"] = "third party"
    alt = re.search(r"/A=(-?\d{5,6})", info)
    if alt:
        out["altitude_ft"] = int(alt.group(1))
    return out


def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360
