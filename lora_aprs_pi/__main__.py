"""Start the station: python3 -m lora_aprs_pi [--simulate] [--config PATH] [--port N]"""

import argparse
import logging
import signal
import sys
import threading

from . import config
from .station import Station
from .web import serve
from .wifi import Wifi


def main():
    ap = argparse.ArgumentParser(description="LoRa APRS station for Raspberry Pi LoRa HATs")
    ap.add_argument("--config", default=config.DEFAULT_PATH, help="settings file (default %(default)s)")
    ap.add_argument("--port", type=int, default=80, help="web page port (default 80)")
    ap.add_argument("--host", default="", help="address to listen on (default: all)")
    ap.add_argument("--simulate", action="store_true", help="use a pretend radio (no HAT needed)")
    ap.add_argument("--no-wifi", action="store_true", help="don't manage WiFi or the hotspot")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(name)s: %(message)s")

    station = Station(args.config, simulate=args.simulate)
    wifi = None if (args.no_wifi or args.simulate) else Wifi(station.cfg["wifi"])
    if wifi:
        station.on_wifi_settings = wifi.apply_settings
    station.start()

    server = serve(station, wifi, args.host, args.port)
    stop = threading.Event()

    def shutdown(*_):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        station.stop()
        if wifi:
            wifi.stop()


if __name__ == "__main__":
    main()
