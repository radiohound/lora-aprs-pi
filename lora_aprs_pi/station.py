"""The station: owns the radio, keeps the list of heard packets, sends beacons."""

import collections
import copy
import logging
import random
import threading
import time

from . import aprs, config, digi, frames, hardware, igate, lacf
from .sx126x import airtime_s
from .gps import Gps
from .sx126x import RadioError

log = logging.getLogger("station")

VERSION = "0.3.0"
DIGI_DELAY_S = (0.5, 2.0)      # random wait before repeating, so digipeaters don't all key up at once
FIRST_BEACON_DELAY_S = 60
RADIO_RETRY_S = 15


class Station:
    def __init__(self, cfg_path, simulate=False):
        self.cfg_path = cfg_path
        self.simulate = simulate
        self.cfg = config.load(cfg_path)
        self.lock = threading.RLock()
        self.heard = collections.deque(maxlen=200)
        self.next_id = 1
        self.counts = {"rx_ok": 0, "rx_dropped": 0, "tx": 0, "gated": 0, "repeated": 0}
        self.started = time.time()

        self.radio = None
        self.radio_state = "starting"
        self.radio_error = None
        self.radio_dirty = False
        self.radio_retry_at = 0

        self.gps = None
        self.beacon_due = time.time() + FIRST_BEACON_DELAY_S
        self.beacon_now_flag = threading.Event()
        self.beacon_number = 0
        self.last_beacon = None
        self.beacon_note = None

        self.aprsis = None                   # APRS-IS connection when the iGate is on
        self.dedupe = igate.Dedupe()
        self.digi_seen = igate.Dedupe()     # packets already repeated (or queued) in the last 30 s
        self.digi_jobs = []                  # repeats waiting for their moment
        self.is_position_due = time.time() + FIRST_BEACON_DELAY_S

        self.on_wifi_settings = None       # set by the program to apply hotspot changes
        self._stop = threading.Event()
        self._thread = None

    # ---- running ----

    def start(self):
        self._start_gps()
        self._start_igate()
        self._thread = threading.Thread(target=self._loop, name="radio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.gps:
            self.gps.stop()
        if self.aprsis:
            self.aprsis.stop()
        self._close_radio()

    def _start_gps(self):
        old = self.gps
        dev = self.cfg["gps"]["device"]
        self.gps = Gps(dev, self.cfg["gps"]["baud"]) if dev else None
        if old:
            old.stop()

    def _start_igate(self):
        old = self.aprsis
        with self.lock:
            ig, call = dict(self.cfg["igate"]), self.cfg["station"]["callsign"]
        self.aprsis = None
        if ig["enabled"] and call != "N0CALL":
            code = ig["passcode"] if ig["passcode"] != "" else igate.passcode(call)
            self.aprsis = igate.AprsIs(ig["server"], ig["port"], call, code)
        if old:
            old.stop()

    def _open_radio(self):
        with self.lock:
            prof, settings = config.radio_settings(self.cfg)
        if self.simulate:
            from .sim_radio import SimRadio
            radio = SimRadio(prof)
        else:
            from .sx126x import SX126x
            radio = SX126x(prof)
        try:
            radio.begin(settings)
            radio.start_rx()
        except Exception:
            radio.close()
            raise
        self.radio = radio
        self.radio_state = "ok"
        self.radio_error = None
        log.info("radio ready: %s, %.3f MHz SF%d", prof["label"], settings["freq_mhz"], settings["sf"])

    def _close_radio(self):
        if self.radio:
            try:
                self.radio.close()
            except Exception:
                pass
        self.radio = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.radio is None or self.radio_dirty:
                    if time.time() < self.radio_retry_at and not self.radio_dirty:
                        self._stop.wait(0.5)
                        continue
                    self.radio_dirty = False
                    self._close_radio()
                    self._open_radio()

                got = self.radio.poll()
                if got:
                    try:
                        self._received(*got)
                    except Exception:                 # a strange packet must never take the radio down
                        log.exception("could not process a received packet")

                if self.beacon_now_flag.is_set():
                    self.beacon_now_flag.clear()
                    self._beacon(manual=True)
                elif time.time() >= self.beacon_due:
                    self._beacon(manual=False)
                self._run_digi_jobs()
                if self.aprsis and time.time() >= self.is_position_due:
                    self._position_to_aprsis()

                self._stop.wait(0.02)
            except (RadioError, OSError) as exc:
                self._radio_failed(str(exc))
            except Exception as exc:              # keep the station alive whatever happens
                log.exception("radio loop error")
                self._radio_failed(f"unexpected error: {exc}")

    def _radio_failed(self, msg):
        log.error("radio: %s", msg)
        self.radio_state = "error"
        self.radio_error = msg
        self._close_radio()
        self.radio_retry_at = time.time() + RADIO_RETRY_S

    # ---- receiving ----

    def _received(self, raw, rssi, snr, freq_offset=None):
        entry = {"dir": "rx", "rssi": rssi, "snr": snr}
        if raw is None:
            entry.update(format="-", error="CRC error (damaged packet)", text=None)
        else:
            got = frames.decode(raw)
            entry.update(format=got["format"], error=got["error"], bytes=len(raw),
                         text=got["text"].decode("utf-8", "replace") if got["text"] else None)
            if got["lacf"]:
                entry["lacf"] = got["lacf"]
            if got["text"]:
                pkt = aprs.parse(got["text"])
                if pkt:
                    entry.update(pkt)
                    self._add_distance(entry)
                self._gate(entry)
                self._maybe_repeat(entry, raw, rssi, snr, freq_offset)
        with self.lock:
            self.counts["rx_ok" if entry.get("text") else "rx_dropped"] += 1
        self._remember(entry)
        log.info("RX %s %s (RSSI %.0f, SNR %.1f)", entry["format"], entry.get("text") or entry["error"], rssi, snr)

    def _gate(self, entry):
        """Pass a received packet to APRS-IS when the iGate is on, and note why if not."""
        conn = self.aprsis
        if not conn:
            return
        call = self.cfg["station"]["callsign"]
        text, reason = igate.check(entry["text"], call)
        if text is None:
            entry["not_gated"] = reason
            return
        if not self.dedupe.is_new(text):
            entry["not_gated"] = "already gated in the last 30 s"
            return
        conn.send(igate.upload_line(text, call))
        entry["gated"] = True
        with self.lock:
            self.counts["gated"] += 1

    def _maybe_repeat(self, entry, raw, rssi, snr, freq_offset):
        """Queue a repeat when the digipeater serves this packet's path."""
        mode = self.cfg["digi"]["mode"]
        if mode == "off" or entry.get("format") not in ("standard", "lacf"):
            return
        call = self.cfg["station"]["callsign"]
        if call == "N0CALL":
            return
        new_tnc2, reason = digi.decide(entry["text"], call, mode)
        if new_tnc2 is None:
            if reason not in ("digipeater off", "no hops left"):
                entry["not_repeated"] = reason
            return
        if not self.digi_seen.is_new(entry["text"]):
            entry["not_repeated"] = "already repeated in the last 30 s"
            return
        self.digi_jobs.append({
            "due": time.monotonic() + random.uniform(*DIGI_DELAY_S), "heard": time.monotonic(),
            "format": entry["format"], "raw": raw, "tnc2": new_tnc2, "source": entry.get("source"),
            "rssi": rssi, "snr": snr, "freq_offset": freq_offset})
        entry["repeating"] = True

    def _run_digi_jobs(self):
        now = time.monotonic()
        due = [j for j in self.digi_jobs if j["due"] <= now]
        if not due:
            return
        self.digi_jobs = [j for j in self.digi_jobs if j["due"] > now]
        for job in due:
            self._repeat(job)

    def _repeat(self, job):
        """Send one repeat. The RXT tuple is filled in now, as spec 11 asks: its time to hop
        runs from the end of reception to the end of this transmission."""
        with self.lock:
            call = self.cfg["station"]["callsign"]
            rd = dict(self.cfg["radio"])

        def tuple_for(length):
            on_air = airtime_s(length, rd["sf"], rd["bw_khz"], rd["cr"], config.PREAMBLE)
            tth_ms = (time.monotonic() - job["heard"] + on_air) * 1000
            return digi.rxt_tuple(job["rssi"], job["snr"], job["freq_offset"], tth_ms, rd["sf"], rd["bw_khz"])

        frame = digi.build_frame(job["raw"], job["format"], job["tnc2"], call, tuple_for(len(job["raw"]) + 13))
        if frame and job["format"] == "lacf":
            frame = digi.build_frame(job["raw"], "lacf", job["tnc2"], call, tuple_for(len(frame)))
        if not frame:
            log.info("not repeated (frame too long or not relayable): %s", job["tnc2"])
            return
        on_air = self.radio.transmit(frame)
        with self.lock:
            self.counts["repeated"] += 1
        self._remember({"dir": "tx", "kind": "repeat", "format": job["format"], "bytes": len(frame),
                        "text": job["tnc2"], "error": None, "source": job["source"],
                        "airtime_ms": round(on_air * 1000)})
        log.info("REPEAT %s %d bytes: %s", job["format"], len(frame), job["tnc2"])

    def _position_to_aprsis(self):
        """The station's own position, sent straight to APRS-IS (no radio time used)."""
        with self.lock:
            bc, call = dict(self.cfg["beacon"]), self.cfg["station"]["callsign"]
            self.is_position_due = time.time() + max(10, bc["interval_min"]) * 60
        pos = self._our_position()
        if self.aprsis and pos:
            info = aprs.position_info(pos[0], pos[1], bc["symbol"], bc["comment"], pos[2])
            self.aprsis.send(f"{call}>{aprs.TOCALL},TCPIP*:{info}")

    def _our_position(self):
        bc = self.cfg["beacon"]
        gps = self.gps                                 # the web thread may replace it at any moment
        if bc["use_gps"] and gps:
            f = gps.fix()
            if f["fresh"]:
                alt = round(f["alt_m"] * 3.28084) if f["alt_m"] is not None else None
                return f["lat"], f["lon"], alt
        if bc["latitude"] is not None and bc["longitude"] is not None:
            return bc["latitude"], bc["longitude"], bc["altitude_ft"]
        return None

    def _add_distance(self, entry):
        here = self._our_position()
        if here and entry.get("lat") is not None:
            entry["distance_km"] = round(aprs.distance_km(here[0], here[1], entry["lat"], entry["lon"]), 1)
            entry["bearing"] = round(aprs.bearing_deg(here[0], here[1], entry["lat"], entry["lon"]))

    def _remember(self, entry):
        entry["time"] = time.time()
        with self.lock:
            entry["id"] = self.next_id
            self.next_id += 1
            self.heard.appendleft(entry)

    # ---- beaconing ----

    def beacon_now(self):
        self.beacon_now_flag.set()

    def _beacon(self, manual):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
            interval = cfg["beacon"]["interval_min"] * 60
            self.beacon_due = time.time() + interval
        bc, call = cfg["beacon"], cfg["station"]["callsign"]
        if not (bc["enabled"] or manual):
            self.beacon_note = "Beacon is turned off"
            return
        if call == "N0CALL":
            self.beacon_note = "Set your callsign before transmitting"
            return
        pos = self._our_position()
        if pos is None:
            self.beacon_note = "No position yet (waiting for GPS)" if bc["use_gps"] else "Set a position first"
            if bc["use_gps"]:
                self.beacon_due = time.time() + 30   # try again soon
            return

        fmt = bc["format"]
        if fmt == "alternate":
            fmt = "lacf" if self.beacon_number % 2 else "standard"
        self.beacon_number += 1
        tnc2 = aprs.build(call, bc["path"], aprs.position_info(pos[0], pos[1], bc["symbol"], bc["comment"], pos[2]))
        try:
            frame, used = frames.encode(tnc2, fmt)
        except ValueError as exc:
            self.beacon_note = f"Beacon not sent: {exc}"
            return
        on_air = self.radio.transmit(frame)
        self.last_beacon = {"time": time.time(), "format": used, "bytes": len(frame),
                            "airtime_ms": round(on_air * 1000), "text": tnc2.decode()}
        self.beacon_note = None
        with self.lock:
            self.counts["tx"] += 1
        self._remember({"dir": "tx", "format": used, "bytes": len(frame), "text": tnc2.decode(),
                        "error": None, "source": call, "airtime_ms": round(on_air * 1000),
                        "type": "position", "lat": round(pos[0], 5), "lon": round(pos[1], 5)})
        log.info("TX %s beacon, %d bytes, %d ms: %s", used, len(frame), on_air * 1000, tnc2.decode())

    # ---- settings ----

    def settings(self):
        with self.lock:
            return config.public(self.cfg)

    def password(self):
        with self.lock:
            return self.cfg["web"]["password"]

    def change_settings(self, changes):
        """Check, save and apply settings from the web page. Returns {} or the problems."""
        changes = copy.deepcopy(changes)
        web = changes.get("web", {})
        if web.pop("clear_password", False):
            web["password"] = ""
        elif not web.get("password"):
            web.pop("password", None)
        wifi = changes.get("wifi", {})
        if not wifi.get("hotspot_password"):
            wifi.pop("hotspot_password", None)

        with self.lock:
            old = self.cfg
            new, errs = config.update(old, changes)
            if errs:
                return errs
            config.save(self.cfg_path, new)
            self.cfg = new
            if new["radio"] != old["radio"]:
                self.radio_dirty = True
            if new["beacon"]["interval_min"] != old["beacon"]["interval_min"]:
                self.beacon_due = min(self.beacon_due, time.time() + new["beacon"]["interval_min"] * 60)
        if new["gps"] != old["gps"]:
            self._start_gps()
        if new["igate"] != old["igate"] or new["station"] != old["station"]:
            self._start_igate()
        if new["wifi"] != old["wifi"] and self.on_wifi_settings:
            # in the background: restarting the hotspot would otherwise cut off this reply
            threading.Timer(2.0, self.on_wifi_settings, args=(dict(new["wifi"]),)).start()
        return {}

    # ---- for the web page ----

    def heard_since(self, since_id=0):
        with self.lock:
            return [e for e in self.heard if e["id"] > since_id]

    def status(self):
        with self.lock:
            cfg = self.cfg
            prof, rs = config.radio_settings(cfg)
            gps, reader = None, self.gps
            if reader:
                gps = reader.fix()
                gps["error"] = reader.error
            return {
                "version": VERSION,
                "simulate": self.simulate,
                "uptime_s": round(time.time() - self.started),
                "callsign": cfg["station"]["callsign"],
                "radio": {
                    "state": self.radio_state, "error": self.radio_error,
                    "board": prof["label"], "freq_mhz": rs["freq_mhz"], "sf": rs["sf"],
                    "bw_khz": rs["bw_khz"], "cr": rs["cr"], "chip_dbm": rs["chip_dbm"],
                    "antenna_dbm": hardware.antenna_power(rs["chip_dbm"], prof),
                    "max_antenna_dbm": hardware.antenna_power(prof["max_chip_dbm"], prof),
                },
                "lacf": {"available": lacf.available(), "error": lacf.load_error()},
                "gps": gps,
                "beacon": {
                    "enabled": cfg["beacon"]["enabled"], "format": cfg["beacon"]["format"],
                    "next_in_s": max(0, round(self.beacon_due - time.time())) if cfg["beacon"]["enabled"] else None,
                    "last": self.last_beacon, "note": self.beacon_note,
                },
                "counts": dict(self.counts),
                "digi": {"mode": cfg["digi"]["mode"], "waiting": len(self.digi_jobs)},
                "igate": dict(self.aprsis.status(), enabled=True) if self.aprsis else {
                    "enabled": cfg["igate"]["enabled"],
                    "state": "off" if not cfg["igate"]["enabled"] else "needs a callsign"},
            }
