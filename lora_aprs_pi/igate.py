"""iGate: passes packets heard on LoRa to the APRS-IS internet network.

The gating rules follow Dire Wolf and the APRS-IS conventions:
  - never gate a packet whose path holds TCPIP, TCPXX, RFONLY or NOGATE
    (matched per hop, exactly, with or without the used mark '*')
  - unwrap third-party packets and apply the same rules to the inner packet
  - don't gate general queries, our own packets, or the same packet twice
    within 30 seconds (whatever path each copy came by)
  - never let a packet carry a line break or NUL onto APRS-IS

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
_TNC2 = re.compile(r"^([A-Za-z0-9-]{1,9})>([A-Za-z0-9-]{1,9})((?:,[A-Za-z0-9*-]{1,10}){0,8}):(.*)$", re.S)


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
    return [h.rstrip("*").upper() for h in path.split(",") if h]


def check(tnc2, own_call):
    """(packet text to gate, None) or (None, reason it isn't gated)."""
    if any(c in tnc2 for c in "\r\n\x00"):
        return None, "has a line break"
    m = _TNC2.match(tnc2)
    if not m:
        return None, "not an APRS packet"
    src, dest, path, info = m.groups()
    blocked = BLOCKED_HOPS.intersection(_hops(path))
    if blocked:
        return None, f"{sorted(blocked)[0]} in path"
    if info.startswith("}"):                       # third party: judge the packet inside
        inner = info[1:]
        mi = _TNC2.match(inner)
        if not mi:
            return None, "bad third-party packet"
        blocked = BLOCKED_HOPS.intersection(_hops(mi.group(3)))
        if blocked:
            return None, f"{sorted(blocked)[0]} in path"
        src, dest, path, info = mi.groups()
        tnc2 = inner
    if info.startswith("?"):
        return None, "general query"
    if src.upper() == own_call.upper():
        return None, "our own packet"
    return tnc2, None


def upload_line(tnc2, own_call):
    """The APRS-IS line: the packet as heard, plus ,qAO,<our call>."""
    head, _, info = tnc2.partition(":")
    return f"{head},qAO,{own_call}:{info}"


class Dedupe:
    """Remembers source + destination + information field for DEDUPE_S seconds."""

    def __init__(self, seconds=DEDUPE_S):
        self.seconds = seconds
        self.seen = collections.OrderedDict()

    def is_new(self, tnc2, now=None):
        now = now if now is not None else time.monotonic()
        while self.seen and next(iter(self.seen.values())) < now - self.seconds:
            self.seen.popitem(last=False)
        head, _, info = tnc2.partition(":")
        key = (head.split(",")[0].upper(), info)   # SRC>DEST without the path, plus the info
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
        self._out = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aprs-is", daemon=True)
        self._thread.start()

    def send(self, line):
        """Queue a line for APRS-IS. Lines queued while disconnected wait up to 5 minutes."""
        try:
            self._out.put_nowait((time.monotonic(), line))
        except queue.Full:
            pass

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": self.state, "verified": self.verified, "error": self.error,
                "server": self.server_name or f"{self.server}:{self.port}", "sent": self.sent}

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
            if time.monotonic() - queued > 300:
                continue                       # too old to be useful
            sock.sendall((line + "\r\n").encode("utf-8", "replace"))
            self.sent += 1
