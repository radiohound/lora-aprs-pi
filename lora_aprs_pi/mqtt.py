"""MQTT feed for the LoRa APRS collector map (https://tiles.aivu.com/lora).

The station sends the same event lines as the LACF build of the ESP32 iGate
(protocol "lora-aprs-events", the fields of its events_format.cpp), so the
collector and the map read both alike:

  lora-aprs/<callsign>/rx       each packet heard, with RSSI, SNR, frequency offset
  lora-aprs/<callsign>/tx       each frame sent (class and size, not the text)
  lora-aprs/<callsign>/status   once a minute: uptime, noise floor, counts by frame type

Packets with RFONLY or NOGATE anywhere in the header are counted but never
sent, and neither are the bytes of a damaged (CRC error) frame, since a damaged
private packet could no longer be recognised as private.

The MQTT client is a small MQTT 3.1.1 publisher written here (QoS 0), so no
extra package is needed. The collector's broker takes a shared, public login on
its plain port 1884 and lets a station write only under its own client id,
which is the callsign.
"""

import base64
import json
import logging
import math
import queue
import random
import select
import socket
import struct
import threading
import time

log = logging.getLogger("mqtt")

COLLECTOR_HOST = "mqtt.lora-aprs.org"
STATUS_EVERY_S = 60
CRC_EVENTS_PER_MIN = 30
KEEPALIVE_S = 60
CLASSES = ("plain", "ppm_full", "ppm_primed", "native", "unknown", "hybrid")
CONNACK_ERRORS = {1: "the server does not speak MQTT 3.1.1", 2: "client id (callsign) refused",
                  3: "server unavailable", 4: "wrong user name or password", 5: "not allowed"}


# ---- the event lines ----

def classify(raw):
    """The frame class from the first bytes on the air, like the iGate's EventsFormat::classify."""
    if raw and len(raw) >= 3 and raw[0] == 0x3C and raw[1] == 0xFF:
        if raw[2] == 0x01:
            return "plain"
        if raw[2] == 0x81:
            return "ppm_full"
        if raw[2] == 0xC1:
            return "ppm_primed"
        if raw[2] == 0x02:
            return "hybrid" if len(raw) >= 4 and (raw[3] & 3) >= 2 else "native"
    return "unknown"


def private_path(tnc2):
    """True if RFONLY or NOGATE appears anywhere in the header (any case), or the packet can't be read."""
    head, colon, _ = tnc2.partition(b":")
    if not colon or b">" not in head:
        return True
    head = head.upper()
    return b"RFONLY" in head or b"NOGATE" in head


def _is_alias(hop):
    return hop.upper().startswith(("WIDE", "RELAY", "TRACE", "ECHO", "TCPIP"))


def _packet(tnc2):
    """The packet part of an rx event, as the iGate writes it."""
    text = tnc2.decode("latin-1")
    out = {"raw_tnc2_base64": base64.b64encode(tnc2).decode(), "tnc2": text, "parse_status": "ok"}
    head = text.partition(":")[0]
    src, gt, rest = head.partition(">")
    if not gt or not src:
        return out
    dst, *path = rest.split(",")
    path = [p for p in path if p]
    if not dst:
        return out
    last = max((i for i, p in enumerate(path) if p.endswith("*")), default=-1)
    who, direct, hops = src, last < 0, last + 1
    if last >= 0:
        who = path[last].rstrip("*")
        if _is_alias(who):                           # look for the real digipeater before the alias
            who = next((path[j].rstrip("*") for j in range(last - 1, -1, -1)
                        if not _is_alias(path[j].rstrip("*"))), None)
    out.update(source=src, destination=dst, path=path, heard_from=who, direct=direct,
               hops_used=hops, private_path=private_path(tnc2))
    return out


class Events:
    """Builds the event lines and keeps the counts for the status line."""

    def __init__(self, callsign, radio):
        self.callsign = callsign
        self.radio = radio                           # {"freq_mhz", "bw_khz", "sf", "cr"}
        self.boot_id = "%08x" % random.getrandbits(32)
        self.sequence = 0
        self.started = time.monotonic()
        self.received = dict.fromkeys(CLASSES, 0)
        self.parse_failed = 0
        self.crc_errors = 0
        self.loop_max_ms = 0
        self._noise = []
        self._crc_window = (0.0, 0)

    def _head(self, event):
        now = time.time()
        if now < 1_700_000_000:                      # the clock isn't set yet (no NTP)
            return None
        self.sequence += 1
        ms = int(now * 1000)
        return {"protocol": "lora-aprs-events", "schema_version": "0.1", "event": event,
                "boot_id": self.boot_id, "sequence": self.sequence, "time_ms": ms,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % (ms % 1000),
                "receiver": {"station": self.callsign, "interface": "lora-aprs-pi"}}

    def _radio(self):
        r = self.radio
        return {"frequency_hz": int(round(r["freq_mhz"] * 1e6)), "bandwidth_hz": int(r["bw_khz"] * 1000),
                "spreading_factor": int(r["sf"]), "coding_rate": "4/%d" % int(r["cr"])}

    def rx(self, raw, rssi, snr, fo, text):
        """The rx event for a received frame, or None when nothing is to be sent.
        raw is None for a CRC error; text is the decoded TNC2 packet (bytes) or None if unreadable."""
        local = {"rssi_dbm": int(round(rssi)), "snr_db": round(float(snr), 1), "freq_error_hz": int(fo or 0)}
        if raw is None:
            self.crc_errors += 1
            start, n = self._crc_window
            now = time.monotonic()
            if now - start >= 60:
                start, n = now, 0
            if n >= CRC_EVENTS_PER_MIN:              # a burst of collisions must not flood the broker
                return None
            self._crc_window = (start, n + 1)
            ev = self._head("rx")
            if ev:
                ev.update(reception={"crc_valid": False, "radio": self._radio(), "local": local},
                          frame={"class": "unknown", "air_bytes": 0, "plain_bytes": 0, "outcome": "crc_error"},
                          packet=None)
            return ev
        cls = classify(raw)
        if text is None:
            self.parse_failed += 1
            ev = self._head("rx")
            if ev:
                ev.update(reception={"crc_valid": True, "radio": self._radio(), "local": local},
                          frame={"class": cls, "air_bytes": len(raw), "plain_bytes": 0, "outcome": "parse_failed"},
                          packet=None)
            return ev
        self.received[cls] += 1
        if private_path(text):
            return None                              # its sender asked for it to stay off the internet
        ev = self._head("rx")
        if ev:
            ev.update(reception={"crc_valid": True, "radio": self._radio(), "local": local},
                      frame={"class": cls, "air_bytes": len(raw), "plain_bytes": len(text) + 3, "outcome": "ok"},
                      packet=_packet(text))
        return ev

    def tx(self, frame, ok=True):
        ev = self._head("tx")
        if ev:
            ev.update(radio=self._radio(), frame={"class": classify(frame), "air_bytes": len(frame), "ok": bool(ok)})
        return ev

    def noise(self, dbm):
        """One idle-channel RSSI reading (about once a second)."""
        if -160 <= dbm < 0:
            self._noise.append(dbm)

    def loop_pass(self, ms):
        self.loop_max_ms = max(self.loop_max_ms, int(ms))

    def status(self):
        ev = self._head("status")
        samples, self._noise = self._noise, []
        loop_ms, self.loop_max_ms = self.loop_max_ms, 0
        if not ev:
            return None
        nf = None
        if samples:
            avg = 10 * math.log10(sum(10 ** (s / 10) for s in samples) / len(samples))   # power average
            nf = {"avg_dbm": round(avg, 1), "min_dbm": int(min(samples)), "max_dbm": int(max(samples)),
                  "samples": len(samples)}
        ev.update(uptime_s=int(time.monotonic() - self.started), loop_max_ms=loop_ms, noise_floor=nf,
                  rx_gain="boosted",
                  received=dict(self.received, dropped_no_reference=0, parse_failed=self.parse_failed),
                  crc_errors=self.crc_errors)
        return ev


# ---- the MQTT client ----

def _str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _packet_bytes(kind, body):
    n, length = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        length += bytes([byte | (0x80 if n else 0)])
        if not n:
            return bytes([kind]) + length + body


def connect_packet(client_id, user, password, keepalive=KEEPALIVE_S):
    flags = 0x02 | (0x80 if user else 0) | (0x40 if user and password else 0)
    body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + _str(client_id)
    if user:
        body += _str(user)
        if password:
            body += _str(password)
    return _packet_bytes(0x10, body)


def publish_packet(topic, payload):
    return _packet_bytes(0x30, _str(topic) + payload)


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


class Mqtt:
    """One connection to the broker, kept up in the background, publishing what it is given."""

    def __init__(self, server, port, client_id, user, password, topic):
        self.server, self.port = server, int(port)
        self.client_id, self.user, self.password = client_id, user, password
        self.topic = topic.strip("/")
        self.state = "connecting"                # connecting | connected | error
        self.error = None
        self.sent = 0
        self._out = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()

    def publish(self, kind, event):
        """Queue one event (a dict) for lora-aprs/<callsign>/<kind>. Dropped if the queue is full."""
        if event is None:
            return
        line = json.dumps(event, separators=(",", ":")).encode()
        try:
            self._out.put_nowait((time.monotonic(), kind, line))
        except queue.Full:
            pass

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": self.state, "error": self.error, "server": f"{self.server}:{self.port}", "sent": self.sent}

    def _run(self):
        backoff = 15
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 15
            except socket.gaierror:
                self.state, self.error = "error", f"can't find {self.server} (no internet yet?)"
            except ConnectionRefusedError:
                self.state, self.error = "error", f"{self.server} refused port {self.port}"
            except socket.timeout:
                self.state, self.error = "error", f"no answer from {self.server}"
            except (OSError, ValueError) as exc:
                self.state, self.error = "error", str(exc) or exc.__class__.__name__
            if self.state == "error":
                log.warning("MQTT: %s (next try in %d s)", self.error, backoff)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)

    def _session(self):
        self.state, self.error = "connecting", None
        with socket.create_connection((self.server, self.port), timeout=15) as sock:
            sock.sendall(connect_packet(self.client_id, self.user, self.password))
            ack = b""
            while len(ack) < 4:
                data = sock.recv(4 - len(ack))
                if not data:
                    raise ConnectionError("the server closed the connection")
                ack += data
            if ack[0] != 0x20:
                raise ValueError("unexpected answer from the server")
            if ack[3] != 0:
                raise ConnectionError("refused: " + CONNACK_ERRORS.get(ack[3], f"code {ack[3]}"))
            self.state = "connected"
            log.info("MQTT: connected to %s:%d as %s", self.server, self.port, self.client_id)
            sock.settimeout(1.0)
            last_ping = last_heard = time.monotonic()
            while not self._stop.is_set():
                try:
                    queued, kind, line = self._out.get(timeout=1.0)
                    if time.monotonic() - queued < 300:
                        sock.sendall(publish_packet(f"{self.topic}/{self.client_id}/{kind}", line))
                        self.sent += 1
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_ping >= KEEPALIVE_S / 2:
                    sock.sendall(PINGREQ)
                    last_ping = now
                if select.select([sock], [], [], 0)[0]:
                    data = sock.recv(256)                  # PINGRESP; nothing is subscribed
                    if not data:
                        raise ConnectionError("the server closed the connection")
                    last_heard = now
                if now - last_heard > KEEPALIVE_S * 1.5:
                    raise ConnectionError("no answer from the server")
            try:
                sock.sendall(DISCONNECT)
            except OSError:
                pass
