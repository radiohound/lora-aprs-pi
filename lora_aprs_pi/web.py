"""The setup web page and its small JSON API. Standard library only."""

import hmac
import ipaddress
import json
import logging
import os
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, hardware, igate

log = logging.getLogger("web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "LoRaAPRSPi"
    station = None
    wifi = None

    def log_message(self, fmt, *args):          # quiet: only errors go to the journal
        pass

    # ---- replies ----

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data).encode(), "application/json")

    def _file(self, name):
        path = os.path.join(STATIC, name)
        with open(path, "rb") as f:
            self._send(200, f.read(), TYPES[os.path.splitext(name)[1]])

    # ---- who is asking ----

    def _host_ok(self):
        """True when the browser asked for this Pi by its address or its own name.

        Any other name means either a phone's captive-portal check (sent to the
        page) or a web page elsewhere trying to reach this one through DNS
        rebinding (refused)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        name = socket.gethostname().lower()
        return host in ("localhost", name, name + ".local")

    def _same_origin(self):
        """A change must come from this page, not a form or script on another site."""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"      # plain HTML forms can't send this type

    def _local_address(self):
        addr = self.connection.getsockname()[0]
        return f"[{addr}]" if ":" in addr else addr

    # ---- requests ----

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        st = self.station
        if not self._host_ok():
            if url.path.startswith("/api/"):
                return self._json({"error": "Open this page by the Pi's address or name"}, 403)
            # A phone checking for a sign-in page: send it to the setup page.
            self.send_response(302)
            port = self.server.server_address[1]
            where = self._local_address() + ("" if port == 80 else f":{port}")
            self.send_header("Location", f"http://{where}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if url.path in FILES:
                return self._file(FILES[url.path])
            if url.path == "/api/status":
                data = st.status()
                data["network"] = self.wifi.status() if self.wifi else None
                return self._json(data)
            if url.path == "/api/heard":
                since = int(q.get("since", ["0"])[0] or 0)
                return self._json({"heard": st.heard_since(since)})
            if url.path == "/api/settings":
                settings = st.settings()
                call = settings["station"]["callsign"]
                return self._json({"settings": settings, "options": {
                    # the passcode for the station's own saved callsign only
                    "auto_passcode": igate.passcode(call) if call != "N0CALL" else None,
                    "profiles": {k: {"label": p["label"], "pa_gain_db": p["pa_gain_db"],
                                     "max_chip_dbm": p["max_chip_dbm"]} for k, p in hardware.PROFILES.items()},
                    "symbols": config.SYMBOLS}})
            if url.path == "/api/wifi":
                return self._json(self.wifi.status() if self.wifi else {"available": False})
            if url.path == "/api/wifi/scan":
                return self._json({"networks": self.wifi.scan() if self.wifi else []})
            # Anything else gets the page, so a phone joining the hotspot pops it up as a sign-in page.
            return self._file("index.html")
        except Exception as exc:
            log.exception("GET %s", url.path)
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self._host_ok() or not self._same_origin():
            return self._json({"error": "Changes are only accepted from this station's own page"}, 403)
        if not self._allowed():
            return self._json({"error": "Settings password needed"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError
            if length > MAX_BODY:
                return self._json({"error": "Request too large"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "Bad request"}, 400)

        try:
            if url.path == "/api/settings":
                errors = self.station.change_settings(body)
                return self._json({"errors": errors}, 200 if not errors else 422)
            if url.path == "/api/beacon":
                self.station.beacon_now()
                return self._json({"ok": True})
            if url.path == "/api/wifi/connect" and self.wifi:
                ssid = str(body.get("ssid", ""))
                if not ssid:
                    return self._json({"error": "Choose a network"}, 400)
                return self._json({"message": self.wifi.connect(ssid, str(body.get("password", "")))})
            if url.path == "/api/wifi/hotspot" and self.wifi:
                return self._json({"message": self.wifi.switch_to_hotspot()})
            return self._json({"error": "Not found"}, 404)
        except Exception as exc:
            log.exception("POST %s", url.path)
            return self._json({"error": str(exc)}, 500)

    def _allowed(self):
        want = self.station.password()
        if not want:
            return True
        given = self.headers.get("X-Station-Password", "")
        return hmac.compare_digest(given.encode(), want.encode())


def serve(station, wifi, host, port):
    Handler.station = station
    Handler.wifi = wifi
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info("web page on http://%s:%d", host or "0.0.0.0", port)
    return server
