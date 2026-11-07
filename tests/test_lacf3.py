"""LACF 3.0.0-dev.1 (type 03) frames against the vectors of the proposal repository."""

import os
import unittest

from lora_aprs_pi import lacf3
from lora_aprs_pi.lacf import LacfError

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_CALL = b"N0RLY-1"


def vectors(name):
    with open(os.path.join(HERE, "vectors", name)) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                yield line.split()


@unittest.skipUnless(lacf3.available(), "run 'make' first")
class TestType03(unittest.TestCase):
    def test_decode_matches_vectors(self):
        n = 0
        for frame, tnc2 in vectors("vectors_hb3_v1.txt"):
            if frame == "-":
                continue
            got, _ = lacf3.decode(bytes.fromhex(frame))
            self.assertEqual(got, bytes.fromhex(tnc2))
            n += 1
        self.assertGreater(n, 100)

    def test_encode_matches_vectors(self):
        n = 0
        for frame, tnc2 in vectors("vectors_hb3_v1.txt"):
            if frame == "-":
                self.assertIsNone(lacf3.encode(bytes.fromhex(tnc2)))
                continue
            self.assertEqual(lacf3.encode(bytes.fromhex(tnc2)), bytes.fromhex(frame))
            n += 1
        self.assertGreater(n, 100)

    def test_rejection_vectors_are_dropped(self):
        n = 0
        for line in vectors("vectors_hb3_reject_v1.txt"):
            with self.assertRaises(LacfError, msg=line[0]):
                lacf3.decode(bytes.fromhex(line[0]))
            n += 1
        self.assertGreater(n, 10)

    def test_capability_vectors(self):
        n = 0
        for frame, tnc2, capb in vectors("vectors_hb3_cap_v1.txt"):
            got, details = lacf3.decode(bytes.fromhex(frame))
            self.assertEqual(got, bytes.fromhex(tnc2))
            self.assertEqual(details["capability_bytes"], bytes.fromhex(capb))
            self.assertEqual(lacf3.encode(bytes.fromhex(tnc2), bytes.fromhex(capb)), bytes.fromhex(frame))
            n += 1
        self.assertGreater(n, 3)

    def test_capability_pack_parse(self):
        b = lacf3.cap_pack(5, 1, 7, 0, 0, 22, 2)
        self.assertEqual(lacf3.cap_parse(b), {"role": 5, "model": 1, "sf": 7, "bw": 0, "cr": 0, "power": 22, "preset": 2})
        with self.assertRaises(ValueError):
            lacf3.cap_pack(8, 0, 0, 0, 0, 0, 0)

    def _relay_vectors(self, name):
        n = 0
        for k, line in enumerate(vectors(name)):
            received, relayed, relayed_pkt = line
            frame = bytes.fromhex(received)
            if relayed == "-":
                continue
            hops = lacf3.decode(frame)[1]["relay_tuples"]
            call = b"N0RLY-%d" % (hops + 1)
            tup = bytes([50 + hops, 20 + hops, 45 + hops, 10 + hops])
            got = lacf3.relay_to(frame, bytes.fromhex(relayed_pkt), call, tup)
            self.assertEqual(got, bytes.fromhex(relayed))
            n += 1
        self.assertGreater(n, 5)

    def test_relay_vectors(self):
        self._relay_vectors("vectors_hb3_relay_v1.txt")
        self._relay_vectors("vectors_hb3_relay_chain_v1.txt")

    def test_passthrough_vectors(self):
        n = 0
        for received, pass_relayed, decoder_relayed in vectors("vectors_hb3_passthrough_v1.txt"):
            if received == "-":
                continue
            frame = bytes.fromhex(received)
            if pass_relayed == "-":
                with self.assertRaises(LacfError):
                    lacf3.front(frame)
                continue
            text, _ = lacf3.front(frame)
            self.assertTrue(text.endswith(b":"))
            # the digipeater's own path rule decides the new path; here it is the one of the expected frame
            new_text, _ = lacf3.front(bytes.fromhex(pass_relayed))
            got = lacf3.relay_to_passthrough(frame, new_text, RELAY_CALL, bytes([50, 20, 45, 10]))
            self.assertEqual(got, bytes.fromhex(pass_relayed))
            n += 1
        self.assertGreater(n, 5)


if __name__ == "__main__":
    unittest.main()
