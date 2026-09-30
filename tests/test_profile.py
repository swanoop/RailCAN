import copy
import unittest
from dataclasses import replace

from railcan.frame import Frame, encode
from railcan.profile import Profile, Signal, default_profile, export_dbc
from railcan.simulator import Simulator


class ProfileTests(unittest.TestCase):
    def test_default_layout_and_load(self):
        profile = default_profile()
        self.assertEqual(profile.frames_per_second, 62)
        self.assertAlmostEqual(profile.nominal_bus_load_percent, 2.7528)
        self.assertEqual(Profile.from_dict(profile.to_dict()), profile)

    def test_known_signed_frame_bytes(self):
        sim = Simulator()
        telemetry = dict(sim.model.telemetry, speed_kph=123.45, acceleration=-.675, mode=3, emergency=0)
        frame = encode(sim.profile.by_id[0x100], telemetry, 254, 1.25)
        self.assertEqual(frame.data.hex().upper(), "39305DFD0300FE55")
        self.assertTrue(frame.checksum_valid)
        decoded = frame.decode(sim.profile.by_id[0x100])
        self.assertEqual(decoded["SpeedKph"], 123.45)
        self.assertEqual(decoded["Acceleration"], -.675)
        self.assertEqual(decoded["AliveCounter"], 254)

    def test_offset_signal(self):
        signal = Signal("Temperature", "cabin_c", 0, 8, 1, -40, -40, 100)
        self.assertEqual(signal.encode(-10), 30)
        self.assertEqual(signal.decode(30), -10)
        with self.assertRaises(ValueError):
            signal.encode(101)

    def test_extended_dbc_identifier(self):
        profile = default_profile()
        message = replace(profile.messages[0], can_id=0x123456, extended=True)
        profile = replace(profile, messages=(message, *profile.messages[1:]))
        dbc = export_dbc(profile)
        self.assertIn(f"BO_ {0x80123456} Motion: 8 VCU", dbc)
        self.assertIn('BA_ "GenMsgCycleTime" BO_', dbc)
        self.assertIn("48|8@1+", dbc)

    def test_hex_ids_can_be_loaded(self):
        data = default_profile().to_dict()
        data["messages"][0]["can_id"] = "0x123"
        self.assertEqual(Profile.from_dict(data).messages[0].can_id, 0x123)

    def test_invalid_profiles_rejected(self):
        original = default_profile().to_dict()
        mutations = [
            lambda d: d.update(bitrate=0),
            lambda d: d.update(bitrate=True),
            lambda d: d.update(unexpected="value"),
            lambda d: d["train"].update(target_speed_kph=float("nan")),
            lambda d: d["train"].update(passengers=2.5),
            lambda d: d["messages"][0].update(period_ms=55),
            lambda d: d["messages"][0].update(period_ms=True),
            lambda d: d["messages"][0].update(can_id=0x800),
            lambda d: d["messages"][0].update(can_id=True),
            lambda d: d["messages"][0].update(name="bad name"),
            lambda d: d["messages"][1].update(can_id=0x100),
            lambda d: d["messages"][0]["signals"][0].update(start=40),
            lambda d: d["messages"][0]["signals"][1].update(start=0),
            lambda d: d["messages"][0]["signals"][0].update(scale=0),
            lambda d: d["messages"][0]["signals"][0].update(source="no_such_signal"),
            lambda d: d["messages"][0]["signals"][0].update(maximum=1000),
            lambda d: d["messages"][0]["signals"][0].update(name="AliveCounter"),
            lambda d: d["messages"][0]["signals"][0].update(signed="false"),
        ]
        for mutate in mutations:
            data = copy.deepcopy(original)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                Profile.from_dict(data)

    def test_invalid_frames_rejected(self):
        for args in [(-1, 1, b""), (float("nan"), 1, b""), (0, 0x800, b""),
                     (0, True, b""), (0, 1, b"123456789"), (0, 1, "abc")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                Frame(*args)
