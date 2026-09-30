import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from railcan.dashboard import Session, create_server
from railcan.traces import read_trace


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = Session()
        cls.server = create_server(cls.session, 0)
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.session.close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.session.control({"action": "reset", "config": {"scenario": "normal", "duration": 2, "rate": 10}})

    def request(self, path, data=None, headers=None, auth=True):
        selected = {"X-RailCAN-Token": self.session.token} if auth else {}
        selected.update(headers or {})
        if data is not None:
            selected["Content-Type"] = "application/json"
        request = Request(self.address + path, json.dumps(data).encode() if data is not None else None, selected)
        return urlopen(request, timeout=3)

    def wait_for_frames(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.session.state()["frame_count"] > 8:
                return
            time.sleep(.01)
        self.fail("Worker did not generate traffic")

    def test_home_and_static_assets(self):
        with self.request("/", auth=False) as response:
            content = response.read().decode()
            self.assertIn("RailCAN", content)
            self.assertIn(self.session.token, content)
            self.assertNotIn("__RAILCAN_TOKEN__", content)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.request("/app.js", auth=False) as response:
            self.assertIn(b"boot();", response.read())

    def test_token_host_and_origin_checks(self):
        for path, headers, auth in [("/api/state", {}, False), ("/", {"Host": "attacker.invalid"}, True),
                                     ("/api/state", {"Origin": "https://attacker.invalid"}, True)]:
            with self.subTest(path=path, headers=headers), self.assertRaises(HTTPError) as context:
                self.request(path, headers=headers, auth=auth)
            self.assertEqual(context.exception.code, 403)

    def test_run_pause_resume_and_complete(self):
        with self.request("/api/control", {"action": "start"}) as response:
            self.assertEqual(json.load(response)["status"], "running")
        self.wait_for_frames()
        self.session.control({"action": "pause"})
        count = self.session.state()["frame_count"]
        time.sleep(.03)
        self.assertEqual(self.session.state()["frame_count"], count)
        self.session.control({"action": "resume"})
        deadline = time.monotonic() + 2
        while self.session.state()["status"] != "complete" and time.monotonic() < deadline:
            time.sleep(.01)
        state = self.session.state()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["frame_count"], 124)

    def test_export_snapshot_survives_reset(self):
        self.session.control({"action": "start"})
        self.wait_for_frames()
        path = self.session.export("pcap")
        self.session.control({"action": "reset"})
        frames = list(read_trace(path))
        self.assertGreaterEqual(len(frames), 8)
        self.assertLessEqual(len(frames), 124)
        self.assertTrue(all(f.checksum_valid for f in frames))

    def test_download_api_and_fault_controls(self):
        self.session.control({"action": "start"})
        self.wait_for_frames()
        self.session.control({"action": "pause"})
        self.session.control({"action": "fault", "kind": "checksum_error", "duration": 1})
        self.session.control({"action": "resume"})
        time.sleep(.1)
        state = self.session.state()
        self.assertGreater(state["checksum_errors"], 0)
        with self.request("/api/export?format=log") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertTrue(response.read().startswith(b"(0.000000)"))
        self.session.control({"action": "clear_faults"})
        self.assertEqual(self.session.sim.faults, [])

    def test_invalid_control_returns_json_error(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/control", {"action": "start", "config": {"rate": 0}})
        self.assertEqual(context.exception.code, 400)
        self.assertIn("error", json.load(context.exception))
