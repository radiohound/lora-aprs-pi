"""iGate: passes packets heard on LoRa to the APRS-IS internet network.

The gating rules follow aprsc's IGATE-HINTS.md and the APRS-IS conventions:
  - never gate a packet whose path holds TCPIP, TCPXX, RFONLY or NOGATE
    (matched per hop, exactly, with or without the used mark '*')
  - unwrap third-party packets and apply the same rules to the inner packet
  - don't gate general queries or our own packets
  - gate every other copy heard, duplicates included: the APRS-IS servers drop
    duplicates themselves and use the extra copies to see who heard whom
  - packets are bytes from radio to APRS-IS, never decoded and re-encoded, so a
    byte that isn't UTF-8 goes out as it came in (no modified duplicates)
  - send the packet up to its first CR or LF; NUL and other bytes stay as they are
  - nothing is held back while APRS-IS is down: a late copy would make a moving
    station jump backwards on the maps

This station only gates RF -> APRS-IS, so uploads are tagged qAO
(a receive-only iGate). LACF frames arrive here already decoded to text,
so LACF stations reach APRS-IS like any other.
"""

import collections
import logging
import queue
import re
import socket
import threading
import time

log = logging.getLogger("igate")

BLOCKED_HOPS = {"TCPIP", "TCPXX", "RFONLY", "NOGATE"}
DEDUPE_S = 30
SOFTWARE = "LoRaAPRSPi"
FRESH_S = 5                    # a line handed to the connection goes out within this, or not at all
_TNC2 = re.compile(rb"^([A-Za-z0-9-]{1,9})>([A-Za-z0-9-]{1,9})((?:,[A-Za-z0-9*-]{1,10}){0,8}):(.*)$", re.S)


def passcode(callsign):
    """The APRS-IS passcode for a callsign (the SSID is ignored).

    This is the widely published APRS-IS hash, not a secret. The station only
    ever works it out for its own configured callsign."""
    call = callsign.upper().split("-")[0]
    h = 0x73E2
    for i, ch in enumerate(call):
        h ^= ord(ch) << (8 if i % 2 == 0 else 0)
    return h & 0x7FFF


def _hops(path):
    return [h.rstrip("*").upper() for h in path.decode("ascii").split(",") if h]


def check(tnc2, own_call):
    """(packet bytes to gate, None) or (None, reason it isn't gated).

    tnc2 is the packet as received, in bytes. Anything after a CR or LF is cut off,
    as IGATE-HINTS asks, so a packet can't carry a second line onto APRS-IS."""
    tnc2 = re.split(rb"[\r\n]", tnc2, maxsplit=1)[0]
    m = _TNC2.match(tnc2)
    if not m:
        return None, "not an APRS packet"
    src, dest, path, info = m.groups()
    blocked = BLOCKED_HOPS.intersection(_hops(path))
    if blocked:
        return None, f"{sorted(blocked)[0]} in path"
    if info.startswith(b"}"):                       # third party: judge the packet inside
        inner = info[1:]
        mi = _TNC2.match(inner)
        if not mi:
            return None, "bad third-party packet"
        blocked = BLOCKED_HOPS.intersection(_hops(mi.group(3)))
        if blocked:
            return None, f"{sorted(blocked)[0]} in path"
        src, dest, path, info = mi.groups()
        tnc2 = inner
    if info.startswith(b"?"):
        return None, "general query"
    if src.decode("ascii").upper() == own_call.upper():
        return None, "our own packet"
    return tnc2, None


def upload_line(tnc2, own_call):
    """The APRS-IS line, in bytes: the packet as heard, plus ,qAO,<our call>."""
    head, _, info = tnc2.partition(b":")
    return head + b",qAO," + own_call.encode("ascii") + b":" + info


class Dedupe:
    """Remembers source + destination + information field for DEDUPE_S seconds.

    For the digipeater only (dedupe belongs on the transmit side); the iGate gates
    every copy. Takes the packet as str or bytes."""

    def __init__(self, seconds=DEDUPE_S):
        self.seconds = seconds
        self.seen = collections.OrderedDict()

    def is_new(self, tnc2, now=None):
        now = now if now is not None else time.monotonic()
        while self.seen and next(iter(self.seen.values())) < now - self.seconds:
            self.seen.popitem(last=False)
        colon, comma = (b":", b",") if isinstance(tnc2, bytes) else (":", ",")
        head, _, info = tnc2.partition(colon)
        key = (head.split(comma)[0].upper(), info)   # SRC>DEST without the path, plus the info
        if key in self.seen:
            return False
        self.seen[key] = now
        return True


class AprsIs:
    """One APRS-IS connection, kept up in the background."""

    def __init__(self, server, port, callsign, code):
        self.server, self.port = server, int(port)
        self.callsign, self.code = callsign, int(code)
        self.state = "connecting"            # connecting | connected | error
        self.verified = None
        self.error = None
        self.server_name = None
        self.sent = 0
        self.dropped = 0                     # lines not sent because APRS-IS was down
        self.logins = 0                      # successful logins so far: the station re-announces itself after each
        self._out = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aprs-is", daemon=True)
        self._thread.start()

    def send(self, line):
        """Hand a line (bytes, or str for our own position and status) to APRS-IS.

        Only while logged in: a line sent while the connection is down is dropped,
        never held for later. The queue only carries lines to the socket thread."""
        if isinstance(line, str):
            line = line.encode("utf-8")
        if self.state != "connected":
            self.dropped += 1
            return
        try:
            self._out.put_nowait((time.monotonic(), line))
        except queue.Full:
            self.dropped += 1

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": self.state, "verified": self.verified, "error": self.error,
                "server": self.server_name or f"{self.server}:{self.port}", "sent": self.sent,
                "dropped": self.dropped}

    def _run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 5
            except (OSError, ValueError) as exc:
                self.state, self.error = "error", str(exc) or exc.__class__.__name__
                log.warning("APRS-IS: %s", self.error)
            self.verified = None
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)

    def _session(self):
        self.state, self.error = "connecting", None
        with socket.create_connection((self.server, self.port), timeout=20) as sock:
            # Short timeouts so queued lines go out promptly. Reading the socket directly:
            # a buffered reader can't be used again once it has timed out.
            sock.settimeout(1.0)
            login = f"user {self.callsign} pass {self.code} vers {SOFTWARE} 0.2\r\n"
            sock.sendall(login.encode())
            last_heard = time.monotonic()
            pending = b""
            while not self._stop.is_set():
                try:
                    data = sock.recv(4096)
                    if not data:
                        raise ConnectionError("server closed the connection")
                    last_heard = time.monotonic()
                    pending += data
                    *lines, pending = pending.split(b"\n")
                    for raw in lines:
                        self._server_line(raw.decode("utf-8", "replace").strip())
                    pending = pending[-4096:]
                except socket.timeout:
                    if time.monotonic() - last_heard > 120:
                        raise ConnectionError("no word from the server for 2 minutes")
                self._flush(sock)

    def _server_line(self, line):
        if line.startswith("# logresp"):
            # "# logresp CALL verified, server T2XXXX" or "unverified"
            self.verified = " verified" in line and "unverified" not in line
            m = re.search(r"server (\S+)", line)
            self.server_name = m.group(1) if m else None
            self.state = "connected"
            self.logins += 1
            log.info("APRS-IS: logged in to %s, %s", self.server_name,
                     "verified" if self.verified else "UNVERIFIED (check the callsign or passcode)")

    def _flush(self, sock):
        if self.state != "connected":
            return
        while True:
            try:
                queued, line = self._out.get_nowait()
            except queue.Empty:
                return
            if time.monotonic() - queued > FRESH_S:
                self.dropped += 1              # late copies make moving stations jump back
                continue
            sock.sendall(line + b"\r\n")
            self.sent += 1
