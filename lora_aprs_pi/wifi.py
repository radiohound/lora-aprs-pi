"""WiFi through NetworkManager (nmcli): join networks, and fall back to a hotspot.

When the Pi can't join any known network, it starts its own hotspot so a
phone can connect and open the setup page. While the hotspot runs with no
phone connected, it tries the known networks again every few minutes, so a
home router coming back after a power cut brings the Pi back too.
"""

import logging
import shutil
import socket
import subprocess
import threading
import time

log = logging.getLogger("wifi")

HOTSPOT = "lora-aprs-pi-hotspot"     # NetworkManager connection name
HOTSPOT_IP = "192.168.4.1"           # the hotspot's address: the usual one for setup hotspots (ESP32 boards use it too)
IFACE = "wlan0"
BOOT_GRACE_S = 90                    # how long to wait for a known network after boot
RETRY_KNOWN_S = 300                  # while idle in hotspot mode, try known networks this often


def _split(line):
    """Split one nmcli -t line on unescaped colons."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


class Wifi:
    def __init__(self, settings, enabled=True):
        self.enabled = enabled and shutil.which("nmcli") is not None
        self.settings = dict(settings)
        self.note = None
        self._last_scan = []
        self._last_scan_time = 0
        self._busy = threading.Lock()
        self._stop = threading.Event()
        if self.enabled:
            self._ensure_hotspot_profile()
            threading.Thread(target=self._watch, name="wifi", daemon=True).start()

    # ---- nmcli ----

    def _nm(self, *args, timeout=40):
        res = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip() or "nmcli failed")
        return res.stdout

    def _ensure_hotspot_profile(self):
        ssid, pw = self.settings["hotspot_ssid"], self.settings["hotspot_password"]
        props = ["connection.autoconnect", "no", "802-11-wireless.ssid", ssid,
                 "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                 "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24", "ipv6.method", "ignore",
                 "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw]
        try:
            names = [_split(l)[0] for l in self._nm("-t", "-f", "NAME", "connection", "show").splitlines()]
            if HOTSPOT in names:
                self._nm("connection", "modify", HOTSPOT, *props)
            else:
                self._nm("connection", "add", "type", "wifi", "ifname", IFACE, "con-name", HOTSPOT, *props)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            self.note = f"Could not set up the hotspot: {exc}"
            log.error(self.note)

    def apply_settings(self, settings):
        self.settings = dict(settings)
        if self.enabled:
            self._ensure_hotspot_profile()
            if self.status().get("mode") == "hotspot":
                self.start_hotspot()          # restart it with the new name/password

    # ---- state ----

    def status(self):
        if not self.enabled:
            return {"available": False, "mode": "none", "note": "WiFi control needs NetworkManager"}
        try:
            rows = [_split(l) for l in self._nm("-t", "-f", "DEVICE,STATE,CONNECTION", "device").splitlines()]
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"available": True, "mode": "unknown", "note": str(exc)}
        row = next((r for r in rows if r[0] == IFACE), None)
        out = {"available": True, "hostname": socket.gethostname(), "mode": "none", "ssid": None, "ip": None,
               "note": self.note}
        if not row:
            out["note"] = "No WiFi adapter (wlan0) found"
            return out
        state, con = row[1], row[2]
        if state.startswith("connected"):
            out["mode"] = "hotspot" if con == HOTSPOT else "client"
            out["ssid"] = self.settings["hotspot_ssid"] if con == HOTSPOT else self._ssid_of(con)
            out["ip"] = self._ip()
        else:
            out["note"] = out["note"] or f"WiFi is {state}"
        return out

    def _ssid_of(self, con):
        try:
            return self._nm("-g", "802-11-wireless.ssid", "connection", "show", con).strip() or con
        except (RuntimeError, subprocess.TimeoutExpired):
            return con

    def _ip(self):
        try:
            addr = self._nm("-g", "IP4.ADDRESS", "device", "show", IFACE).strip().split("|")[0]
            return addr.split("/")[0] or None
        except (RuntimeError, subprocess.TimeoutExpired):
            return None

    def scan(self):
        """Networks in range, strongest first. While the Pi is the hotspot its WiFi can't
        scan, so the list from just before the hotspot started is used instead."""
        if not self.enabled:
            return []
        try:
            nets = self._scan_now()
        except (RuntimeError, subprocess.TimeoutExpired):
            nets = []
        if nets:
            self._last_scan = nets
        return nets or self._last_scan

    def _remember_scan(self):
        if time.time() - self._last_scan_time > 600:
            self._last_scan_time = time.time()
            try:
                self._last_scan = self._scan_now() or self._last_scan
            except (RuntimeError, subprocess.TimeoutExpired):
                pass

    def _scan_now(self):
        out = self._nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes", timeout=30)
        best = {}
        for line in out.splitlines():
            ssid, signal, sec = (_split(line) + ["", "", ""])[:3]
            if not ssid or ssid == self.settings["hotspot_ssid"]:
                continue
            sig = int(signal or 0)
            if ssid not in best or sig > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": sig, "secure": sec not in ("", "--")}
        return sorted(best.values(), key=lambda n: -n["signal"])

    # ---- changes ----

    def connect(self, ssid, password):
        """Join a network in the background (the phone on the hotspot will drop off)."""
        threading.Thread(target=self._connect, args=(ssid, password), daemon=True).start()
        return (f"Joining {ssid}. This phone will lose the Pi's hotspot. Put the phone back on {ssid} "
                f"and open http://{socket.gethostname()}.local. If the Pi can't join, its hotspot comes back "
                f"within a minute.")

    def _connect(self, ssid, password):
        with self._busy:
            time.sleep(2)                      # let the web reply reach the phone first
            try:
                self._nm("connection", "down", HOTSPOT, timeout=15)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            args = ["device", "wifi", "connect", ssid, "ifname", IFACE]
            if password:
                args += ["password", password]
            try:
                self._nm(*args, timeout=60)
                self.note = None
                log.info("joined %s", ssid)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                self.note = f"Could not join {ssid}: {exc}"
                log.error(self.note)
                self._start_hotspot_locked()

    def start_hotspot(self):
        with self._busy:
            self._start_hotspot_locked()

    def _start_hotspot_locked(self):
        try:
            self._nm("connection", "up", HOTSPOT, timeout=30)
            log.info("hotspot %s started", self.settings["hotspot_ssid"])
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            self.note = f"Could not start the hotspot: {exc}"
            log.error(self.note)

    def switch_to_hotspot(self):
        threading.Thread(target=self.start_hotspot, daemon=True).start()
        return (f"Switching to the hotspot. Join \"{self.settings['hotspot_ssid']}\" on this phone, "
                f"then open http://{HOTSPOT_IP}")

    def _phones_on_hotspot(self):
        try:
            out = subprocess.run(["iw", "dev", IFACE, "station", "dump"], capture_output=True, text=True, timeout=5).stdout
            return out.count("Station ")
        except (OSError, subprocess.TimeoutExpired):
            return 1                           # can't tell: assume someone is there and leave it alone

    def _watch(self):
        started = time.time()
        down_since = None
        last_retry = time.time()
        while not self._stop.wait(15):
            if self._busy.locked():
                continue
            st = self.status()
            mode = st.get("mode")
            if mode == "unknown":
                continue                       # nmcli hiccup: never act on it
            if mode == "client":
                down_since = None
                self._remember_scan()
            elif mode == "hotspot":
                if time.time() - last_retry > RETRY_KNOWN_S and self._phones_on_hotspot() == 0:
                    last_retry = time.time()
                    self._retry_known()
            else:
                down_since = down_since or time.time()
                if time.time() - started > BOOT_GRACE_S and time.time() - down_since > 30:
                    log.info("no WiFi network; starting the hotspot")
                    self._remember_scan()
                    self.start_hotspot()
                    last_retry = time.time()

    def _retry_known(self):
        """Drop the hotspot briefly and let NetworkManager try the known networks."""
        with self._busy:
            try:
                self._nm("connection", "down", HOTSPOT, timeout=15)
            except (RuntimeError, subprocess.TimeoutExpired):
                return
            for _ in range(12):
                time.sleep(5)
                st = self.status()
                if st.get("mode") == "client":
                    log.info("back on %s", st.get("ssid"))
                    return
            self._start_hotspot_locked()

    def stop(self):
        self._stop.set()
