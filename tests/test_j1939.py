import unittest

from railcan.j1939 import (
    DTC,
    PGN_DM1,
    BAMReassembler,
    bam_frames,
    compose_id,
    decode_dm1,
    dm1_payload,
    split_id,
)
from railcan.profile import (
    Profile,
    default_profile,
    export_dbc,
    j1939_profile,
    load_profile,
)
from railcan.simulator import Fault, Simulator
from railcan.traces import inspect_trace


class IdentifierTests(unittest.TestCase):
    def test_pdu2_and_pdu1(self):
        self.assertEqual(compose_id(0xF004, 3, 0), 0x0CF00400)          # EEC1 from SA 0
        self.assertEqual(compose_id(0xFEEE, 6, 0x21), 0x18FEEE21)
        self.assertEqual(compose_id(0xEA00, 6, 0xF9, 0x00), 0x18EA00F9)  # request to address 0
        self.assertEqual(split_id(0x18EA00F9), (6, 0xEA00, 0xF9, 0x00))
        self.assertEqual(split_id(0x0CF00400), (3, 0xF004, 0, 0xFF))
        with self.assertRaises(ValueError):
            compose_id(0xEA05)  # PDU1 PGN with a low byte
        with self.assertRaises(ValueError):
            compose_id(0xF004, priority=8)


class DiagnosticTests(unittest.TestCase):
    def test_dm1_single_and_empty(self):
        self.assertEqual(dm1_payload([]), bytes.fromhex("00FF00000000FFFF"))
        payload = dm1_payload([DTC(110, 0)])
        self.assertEqual(payload, bytes.fromhex("04FF6E000001FFFF"))
        self.assertEqual(decode_dm1(payload)["dtcs"], [{"spn": 110, "fmi": 0, "oc": 1}])
        high = DTC(0x7FFFF, 31, 126)
        self.assertEqual(DTC.decode(high.encode()), high)

    def test_bam_round_trip(self):
        payload = dm1_payload([DTC(110, 0), DTC(100, 1), DTC(190, 2)])
        frames = bam_frames(PGN_DM1, payload, 7, 0)
        self.assertEqual(frames[0][1][0], 0x20)
        self.assertEqual(len(frames), 1 + 2)
        reassembler = BAMReassembler()
        results = [reassembler.feed(i * .05, can_id, data) for i, (can_id, data) in enumerate(frames)]
        self.assertEqual(results[-1], (PGN_DM1, 0, payload))
        self.assertEqual(len(decode_dm1(results[-1][2])["dtcs"]), 3)

    def test_incomplete_bam_is_counted(self):
        frames = bam_frames(PGN_DM1, dm1_payload([DTC(1, 1), DTC(2, 2)]), 7, 0)
        reassembler = BAMReassembler()
        reassembler.feed(0, *frames[0])
        reassembler.feed(0, *frames[0])  # a new announcement aborts the unfinished one
        self.assertEqual(reassembler.aborted, 1)


class ProfileTests(unittest.TestCase):
    def test_builtin_j1939_profile(self):
        profile = j1939_profile()
        self.assertEqual(Profile.from_dict(profile.to_dict()), profile)
        self.assertIs(load_profile("j1939").name, profile.name)
        self.assertTrue(all(m.extended and not m.has_trailer and m.fill == 0xFF for m in profile.messages))
        dbc = export_dbc(profile)
        self.assertIn('BA_ "VFrameFormat" BO_ 2364539904 3;', dbc)  # 0x0CF00400 | 0x80000000
        self.assertNotIn("AliveCounter", dbc)

    def test_pgn_shorthand_and_full_payload(self):
        profile = Profile.from_dict({"name": "Mini", "messages": [{
            "name": "Custom", "pgn": "0xFF10", "priority": 6, "source_address": 0x42, "period_ms": 100,
            "signals": [{"name": "Wide", "source": "const(5)", "start": 0, "length": 64, "maximum": 10}]}]})
        message = profile.messages[0]
        self.assertEqual(message.can_id, 0x18FF1042)
        frame = Simulator(profile).tick()[0]
        self.assertEqual(frame.data, (5).to_bytes(8, "little"))

    def test_invalid_layouts(self):
        base = j1939_profile().to_dict()
        cases = [
            lambda d: d["messages"][0].update(trailer="crc"),
            lambda d: d["messages"][0].update(dlc=9),
            lambda d: d["messages"][0].update(fill=256),
            lambda d: d["messages"][0].update(can_id=0x0CF00500),  # disagrees with pgn
            lambda d: d["messages"][0]["signals"][0].update(source="sine(0,500,10)"),  # leaves range
            lambda d: d["messages"][0]["signals"][0].update(source="sine(0,1)"),
            lambda d: d["messages"][0]["signals"][0].update(start=60),
            lambda d: d["dm1"].update(period_ms=50),
            lambda d: d["dm1"]["fault_dtcs"].update(x={"spn": 600000, "fmi": 1}),
        ]
        for change in cases:
            data = Profile.from_dict(base).to_dict()
            change(data)
            with self.subTest(change=change), self.assertRaises(ValueError):
                Profile.from_dict(data)

    def test_trailer_faults_need_a_trailer(self):
        with self.assertRaisesRegex(ValueError, "trailer"):
            Simulator(j1939_profile(), faults=[Fault("checksum_error", 1, 1, "EEC1")])


class SimulationTests(unittest.TestCase):
    def test_waveforms(self):
        data = default_profile().to_dict()
        data["messages"][0]["signals"][0]["source"] = "ramp(0,100,10)"
        data["messages"][0]["signals"][2]["source"] = "square(1,4,2)"
        profile = Profile.from_dict(data)
        frames = [f for f in Simulator(profile).frames(3) if f.can_id == 0x100]
        speeds = [f.decode(profile.messages[0])["SpeedKph"] for f in frames]
        modes = [f.decode(profile.messages[0])["OperatingMode"] for f in frames]
        self.assertEqual(speeds[0], 0)
        self.assertAlmostEqual(speeds[20], 10, places=2)   # t = 1.0 s
        self.assertEqual(modes[0], 1)
        self.assertEqual(modes[30], 4)                       # t = 1.5 s, second half of the period

    def test_probe_sequence_counts_transmissions(self):
        profile = j1939_profile()
        probe = next(m for m in profile.messages if m.name == "Probe")
        values = [f.decode(probe)["ProbeSequence"] for f in Simulator(profile).frames(5) if f.can_id == probe.can_id]
        self.assertEqual(values, [0, 1, 2, 3, 4])

    def test_engine_telemetry_tracks_load_and_faults(self):
        sim = Simulator(j1939_profile(), "low_oil_pressure")
        list(sim.frames(19))
        normal = sim.model.telemetry["oil_pressure_kpa"]
        list(sim.frames(2))
        self.assertLess(sim.model.telemetry["oil_pressure_kpa"], normal * .5)

    def test_dm1_reports_faults_including_bam(self):
        profile = j1939_profile()
        summary = inspect_trace(Simulator(profile, "multi_dtc").frames(50), profile)
        dm1 = summary["j1939"]
        self.assertEqual(dm1["bam_incomplete"], 0)
        self.assertEqual({(d["spn"], d["fmi"]) for d in dm1["dtcs"]}, {(110, 0), (100, 1)})
        self.assertEqual(dm1["dtcs"][0]["broadcasts"], 30)
        names = {row["message"] for row in summary["messages"]}
        self.assertTrue({"TP.CM", "TP.DT", "DM1", "EEC1"} <= names)

    def test_mapped_fault_dtc(self):
        profile = j1939_profile()
        summary = inspect_trace(Simulator(profile, "engine_overheat").frames(25), profile)
        self.assertEqual([(d["spn"], d["fmi"]) for d in summary["j1939"]["dtcs"]], [(110, 0)])

    def test_explicit_dtc_fault_validation(self):
        with self.assertRaises(ValueError):
            Fault("dtc", 0, 1)
        with self.assertRaises(ValueError):
            Fault("dtc", 0, 1, spn=1, fmi=40)

    def test_deterministic(self):
        first = list(Simulator(j1939_profile(), "multi_dtc", 7).frames(20))
        second = list(Simulator(j1939_profile(), "multi_dtc", 7).frames(20))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class FreezeTests(unittest.TestCase):
    def test_freeze_message_holds_values_but_not_sequence(self):
        profile = j1939_profile()
        oil = next(m for m in profile.messages if m.name == "EEC1")
        probe = next(m for m in profile.messages if m.name == "Probe")
        frames = list(Simulator(profile, faults=[Fault("freeze_message", 13, 5, "EEC1"),
                                                 Fault("freeze_message", 2, 3, "Probe")]).frames(20))
        speeds = [f.decode(oil)["EngineSpeed"] for f in frames if f.can_id == oil.can_id and 13 <= f.timestamp < 18]
        later = [f.decode(oil)["EngineSpeed"] for f in frames if f.can_id == oil.can_id and f.timestamp >= 18]
        self.assertEqual(len(set(speeds)), 1)
        self.assertGreater(len(set(later)), 1)
        sequence = [f.decode(probe)["ProbeSequence"] for f in frames if f.can_id == probe.can_id]
        self.assertEqual(sequence, list(range(20)))
