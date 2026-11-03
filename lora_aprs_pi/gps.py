"""Optional GPS: reads NMEA sentences from a serial port.

Any GPS that outputs standard NMEA works (USB GPS sticks, u-blox modules,
ATGM336H). Uses only the standard library, so there is nothing to install.
"""

import os
import termios
import threading
import time

BAUDS = {4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200,
         38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}


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


def parse(line, fix):
    """Update the fix dict from one NMEA line. Returns True if it changed anything."""
    line = line.strip()
    if not checksum_ok(line):
        return False
    f = line.split("*")[0].split(",")
    kind = f[0][3:]
    if kind == "RMC" and len(f) >= 7:
        fix["valid"] = f[2] == "A"
        if fix["valid"]:
            fix["lat"] = _coord(f[3], f[4], 2)
            fix["lon"] = _coord(f[5], f[6], 3)
            fix["time"] = time.time()
        return True
    if kind == "GGA" and len(f) >= 10:
        if f[6] not in ("", "0"):
            fix["sats"] = int(f[7] or 0)
            if f[9]:
                fix["alt_m"] = float(f[9])
        return True
    return False


class Gps:
    """Reads a GPS in a background thread. `fix()` gives the latest position."""

    def __init__(self, device, baud):
        self.device, self.baud = device, int(baud)
        self._fix = {"valid": False, "lat": None, "lon": None, "alt_m": None, "sats": 0, "time": 0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error = None
        self._thread = threading.Thread(target=self._run, name="gps", daemon=True)
        self._thread.start()

    def fix(self):
        with self._lock:
            f = dict(self._fix)
        f["fresh"] = f["valid"] and time.time() - f["time"] < 10
        return f

    def stop(self):
        self._stop.set()

    def _open(self):
        fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        speed = BAUDS.get(self.baud, termios.B9600)
        attrs[0] = termios.IGNPAR                              # iflag
        attrs[1] = 0                                           # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = termios.ICANON                              # read whole lines
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return os.fdopen(fd, "rb", buffering=0)

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._open() as port:
                    self.error = None
                    while not self._stop.is_set():
                        line = port.readline().decode("ascii", "replace")
                        if not line:
                            time.sleep(0.1)
                            continue
                        with self._lock:
                            parse(line, self._fix)
            except OSError as exc:
                self.error = f"{self.device}: {exc.strerror or exc}"
                self._stop.wait(5)
