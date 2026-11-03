"""A pretend radio for trying the station without a HAT (--simulate).

It "hears" a few made-up stations now and then, in both formats, and keeps
what the station transmits in `sent` so tests can check it.
"""

import random
import time

from . import lacf
from .sx126x import airtime_s

PLAIN = b"<\xff\x01"

SAMPLE_PACKETS = [
    b"N0TEST-7>APLRT1,WIDE1-1:!3704.20N/12133.80W>Test tracker on the trail",
    b"N0TEST-9>APLRT1,WIDE1-1:!3711.55N/12129.43W[Hiker /A=001820",
    b"N0DEMO-10>APLRG1,WIDE1-1:!3700.60N/12134.12WL LoRa iGate 433.775 MHz",
    b"N0DEMO-11>APLRT1,WIDE1-1:>Balloon payload test, all systems nominal",
    b"N0TEST-5>APLRT1:!3658.00N/12140.00W-Base station",
]


class SimRadio:
    def __init__(self, prof=None):
        self.settings = None
        self.sent = []
        self.next_rx = time.monotonic() + 3

    def begin(self, settings):
        self.configure(settings)

    def configure(self, settings):
        self.settings = dict(settings)

    def start_rx(self):
        pass

    def poll(self):
        if time.monotonic() < self.next_rx:
            return None
        self.next_rx = time.monotonic() + random.uniform(8, 20)
        text = random.choice(SAMPLE_PACKETS)
        frame = PLAIN + text
        if lacf.available() and random.random() < 0.5:
            frame = lacf.encode(text) or frame
        if random.random() < 0.05:
            return (None, -128.0, -14.5)                   # an occasional CRC error
        return (frame, round(random.uniform(-125, -80), 1), round(random.uniform(-12, 9), 2))

    def transmit(self, payload):
        s = self.settings
        t = airtime_s(len(payload), int(s["sf"]), float(s["bw_khz"]), int(s["cr"]), int(s["preamble"]))
        self.sent.append(bytes(payload))
        return t

    def close(self):
        pass
