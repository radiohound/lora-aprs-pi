"""The web page and its API, against a station with the simulated radio."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from lora_aprs_pi.station import Station
from lora_aprs_pi.web import serve


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.st = Station(os.path.join(cls.dir.name, "config.json"), simulate=True)
        cls.st.start()
        cls.server = serve(cls.st, None, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.st.stop()
        cls.dir.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def post(self, path, body, password=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        if password is not None:
            req.add_header("X-Station-Password", password)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_assets(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            code, ctype, _ = self.get(path)
            self.assertEqual(code, 200)
            self.assertTrue(ctype.startswith(kind))

    def test_unknown_path_serves_page_for_captive_portal(self):
        code, ctype, body = self.get("/generate_204")
        self.assertEqual(code, 200)
        self.assertIn(b"LoRa APRS Pi", body)

    def test_status_and_settings(self):
        _, _, body = self.get("/api/status")
        self.assertIn("radio", json.loads(body))
        _, _, body = self.get("/api/settings")
        data = json.loads(body)
        self.assertIn("meshadv_m33s", data["options"]["profiles"])
        self.assertEqual(data["settings"]["wifi"]["hotspot_password"], "")

    def test_bad_settings_give_field_errors(self):
        code, data = self.post("/api/settings", {"radio": {"sf": 99}})
        self.assertEqual(code, 422)
        self.assertIn("radio.sf", data["errors"])

    def test_password_protects_changes(self):
        code, _ = self.post("/api/settings", {"web": {"password": "letmein"}})
        self.assertEqual(code, 200)
        try:
            code, _ = self.post("/api/beacon", {})
            self.assertEqual(code, 401)
            code, _ = self.post("/api/beacon", {}, password="wrong")
            self.assertEqual(code, 401)
            code, _ = self.post("/api/beacon", {}, password="letmein")
            self.assertEqual(code, 200)
        finally:
            self.post("/api/settings", {"web": {"clear_password": True}}, password="letmein")

    def test_bad_json_rejected(self):
        req = urllib.request.Request(self.base + "/api/settings", data=b"not json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
