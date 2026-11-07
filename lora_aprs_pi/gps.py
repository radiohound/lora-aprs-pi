"""Optional GPS: reads NMEA sentences from a serial port.

Any GPS that outputs standard NMEA works (USB GPS sticks, u-blox modules,
ATGM336H). Uses only the standard library, so there is nothing to install.

Besides the position, the reader keeps track of how far along the GPS is, so
the page can say which step is missing:

  not connected   the device can't be opened (unplugged, or a wrong name)
  no data         it opened, but nothing is arriving
  garbled         bytes arrive but no sentence passes its checksum (usually the baud rate)
  no fix          good sentences, no position yet (with the satellites in view)
  rough fix       a position, but 2D only or poor geometry (HDOP over 5)
  good fix        a 3D position
"""

import os
import termios
import threading
import time

BAUDS = {4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200,
         38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}
STALE_S = 5          # nothing for this long: "no data" / "garbled"
FIX_FRESH_S = 10     # a fix older than this isn't used for beacons
ROUGH_HDOP = 5.0


def checksum_ok(line):
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, given = line[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value, hemi, width):
    if not value:
        return None
    deg = int(value[:width])
    minutes = float(value[width:])
    out = deg + minutes / 60
    return -out if hemi in ("S", "W") else out


def serial_ports():
    """Serial devices a GPS may be on: USB GPS (ttyACM, ttyUSB) and the Pi's own UART."""
    import glob
    found = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/ttyUSB*"))
    return found + [p for p in ("/dev/serial0",) if os.path.exists(p)]


def new_fix():
    return {"valid": False, "lat": None, "lon": None, "alt_m": None, "sats": 0, "time": 0,
            "fix_type": None, "hdop": None, "in_view": {}, "speed_kn": None, "course": None}


def parse(line, fix):
    """Update the fix dict from one NMEA line. Returns True if the line was a good sentence."""
    line = line.strip()
    if not checksum_ok(line):
        return False
    f = line.split("*")[0].split(",")
    talker, kind = f[0][1:3], f[0][3:]
    try:
        if kind == "RMC" and len(f) >= 7:
            fix["valid"] = f[2] == "A"
            if fix["valid"]:
                fix["lat"] = _coord(f[3], f[4], 2)
                fix["lon"] = _coord(f[5], f[6], 3)
                fix["time"] = time.time()
                fix["speed_kn"] = float(f[7]) if len(f) > 7 and f[7] else None
                fix["course"] = float(f[8]) if len(f) > 8 and f[8] else None
        elif kind == "GGA" and len(f) >= 10:
            fix["sats"] = int(f[7] or 0)                   # satellites used in the fix
            fix["hdop"] = float(f[8]) if f[8] else None
            if f[6] not in ("", "0") and f[9]:
                fix["alt_m"] = float(f[9])
        elif kind == "GSA" and len(f) >= 3 and f[2] in ("1", "2", "3"):
            fix["fix_type"] = int(f[2])                    # 1 none, 2 = 2D, 3 = 3D
        elif kind == "GSV" and len(f) >= 4 and f[3]:
            fix.setdefault("in_view", {})[talker] = int(f[3])   # per system: GP, GL, GA, GB...
    except ValueError:
        return False
    return True


class Gps:
    """Reads a GPS in a background thread. `fix()` gives the latest position and how it's doing."""

    def __init__(self, device, baud):
        self.device, self.baud = device, int(baud)
        self._fix = new_fix()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error = None
        self.opened_at = None              # when the port was opened (None: not open)
        self.last_bytes = 0                # when anything last arrived
        self.last_sentence = 0             # when a sentence last passed its checksum
        self._thread = threading.Thread(target=self._run, name="gps", daemon=True)
        self._thread.start()

    def fix(self):
        now = time.time()
        with self._lock:
            f = dict(self._fix)
            f["in_view"] = sum(self._fix.get("in_view", {}).values())
        f["fresh"] = f["valid"] and now - f["time"] < FIX_FRESH_S
        f["state"] = self._state(f, now)
        return f

    def _state(self, f, now):
        if self.opened_at is None:
            return "not connected"
        if now - self.last_sentence < STALE_S:
            if not f["fresh"]:
                return "no fix"
            rough = f["fix_type"] == 2 or (f["hdop"] is not None and f["hdop"] > ROUGH_HDOP) \
                or (f["fix_type"] is None and 0 < f["sats"] < 4)
            return "rough fix" if rough else "good fix"
        if now - self.last_bytes < STALE_S:
            return "garbled"
        return "no data"

    def stop(self):
        self._stop.set()

    def _open(self):
        fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            speed = BAUDS.get(self.baud, termios.B9600)
            attrs[0] = termios.IGNPAR                          # iflag
            attrs[1] = 0                                       # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                                       # raw: lines are split here, so garbage still counts as bytes
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 5                        # a read waits up to 0.5 s
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            os.set_blocking(fd, True)
        except (OSError, termios.error):
            os.close(fd)
            raise
        return fd

    def _run(self):
        while not self._stop.is_set():
            fd = None
            try:
                fd = self._open()
                self.error, self.opened_at = None, time.time()
                pending = b""
                while not self._stop.is_set():
                    data = os.read(fd, 512)
                    if not data:
                        if not os.path.exists(self.device):
                            raise OSError(2, "unplugged")
                        continue
                    self.last_bytes = time.time()
                    pending += data
                    *lines, pending = pending.split(b"\n")
                    pending = pending[-512:]
                    for raw in lines:
                        with self._lock:
                            if parse(raw.decode("ascii", "replace"), self._fix):
                                self.last_sentence = time.time()
            except (OSError, termios.error) as exc:
                msg = getattr(exc, "strerror", None) or str(exc)
                self.error = f"{self.device}: {msg}"
            finally:
                self.opened_at = None
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._stop.wait(3)
