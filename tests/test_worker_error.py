import time
import unittest
from dataclasses import replace

from railcan.dashboard import Session
from railcan.profile import default_profile


class WorkerErrorTests(unittest.TestCase):
    def test_encoding_error_stops_session_and_reset_recovers(self):
        profile = default_profile()
        motion = profile.messages[0]
        # A valid representable range that excludes the initial stationary speed.
        motion = replace(motion, signals=(replace(motion.signals[0], minimum=1), *motion.signals[1:]))
        profile = replace(profile, messages=(motion, *profile.messages[1:]))
        session = Session(profile)
        try:
            session.control({"action": "start"})
            deadline = time.monotonic() + 2
            while session.state()["status"] != "error" and time.monotonic() < deadline:
                time.sleep(.01)
            state = session.state()
            self.assertEqual(state["status"], "error")
            self.assertIn("SpeedKph", state["error"])
            self.assertTrue(session.worker.is_alive())
            session.control({"action": "reset"})
            self.assertEqual(session.state()["status"], "ready")
            self.assertIsNone(session.state()["error"])
        finally:
            session.close()
