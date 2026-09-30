import unittest
from dataclasses import replace

from railcan.profile import default_profile
from railcan.simulator import Fault, SCENARIOS, Simulator


def short_profile():
    profile = default_profile()
    return replace(profile, train=replace(profile.train, target_speed_kph=18, dwell_s=3, cruise_s=1))


class SimulatorTests(unittest.TestCase):
    def test_counts_and_order(self):
        sim = Simulator()
        frames = list(sim.frames(1))
        self.assertEqual(len(frames), 62)
        self.assertEqual([f.can_id for f in frames[:8]], sorted(sim.profile.by_id))
        motion = [f for f in frames if f.can_id == 0x100]
        self.assertEqual([round(f.timestamp * 1000) for f in motion], [i * 50 for i in range(20)])
        self.assertTrue(all(f.timestamp < 1 and f.checksum_valid for f in frames))

    def test_seed_is_repeatable_and_controls_noise(self):
        first = list(Simulator(seed=123).frames(4))
        self.assertEqual(first, list(Simulator(seed=123).frames(4)))
        a, b = Simulator(seed=1), Simulator(seed=2)
        list(a.frames(4))
        list(b.frames(4))
        self.assertNotEqual(a.model.cabin_temp, b.model.cabin_temp)

    def test_model_invariants_and_station_cycle(self):
        sim = Simulator(short_profile())
        distance, braking_current, phases = 0, False, set()
        for _ in range(3500):
            frames = sim.tick()
            t = sim.model.telemetry
            self.assertGreaterEqual(t["distance_m"], distance)
            distance = t["distance_m"]
            self.assertTrue(0 <= t["speed_kph"] <= 18.00001)
            self.assertTrue(-.75001 <= t["acceleration"] <= .65001)
            if t["left_doors"] or t["right_doors"]:
                self.assertEqual(t["speed_kph"], 0)
                self.assertEqual(t["traction_pct"], 0)
                self.assertEqual(t["interlock"], 0)
            braking_current |= t["dc_current_a"] < 0
            phases.add(sim.model.phase)
            self.assertTrue(all(frame.checksum_valid for frame in frames))
        self.assertGreaterEqual(sim.model.station, 1)
        self.assertTrue(braking_current)
        self.assertEqual(phases, {"station", "accelerating", "cruising", "braking"})

    def test_counter_wrap(self):
        motion = [f for f in Simulator().frames(14) if f.can_id == 0x100]
        self.assertEqual(motion[255].data[6], 255)
        self.assertEqual(motion[256].data[6], 0)
        self.assertEqual(motion[257].data[6], 1)

    def test_dropout_window_and_source_counter(self):
        sim = Simulator(faults=[Fault("message_dropout", .2, .3, "Traction")])
        traction = [f for f in sim.frames(1) if f.can_id == 0x200]
        self.assertEqual([round(f.timestamp, 1) for f in traction], [0, .1, .5, .6, .7, .8, .9])
        self.assertEqual(traction[2].data[6], 5)

    def test_corrupt_checksum_only_inside_window(self):
        frames = list(Simulator(faults=[Fault("checksum_error", .2, .3)]).frames(1))
        self.assertEqual(sum(f.checksum_valid is False for f in frames), 6)
        self.assertTrue(all(f.checksum_valid for f in frames if f.can_id != 0x100 or not .2 <= f.timestamp < .5))

    def test_sensor_freeze_and_recovery(self):
        sim = Simulator(short_profile(), faults=[Fault("stuck_speed", 4, 3)])
        message = sim.profile.by_id[0x100]
        motion = [f for f in sim.frames(8) if f.can_id == 0x100]
        values = {f.decode(message)["SpeedKph"] for f in motion if 4 <= f.timestamp < 7}
        self.assertEqual(len(values), 1)
        recovered = next(f for f in motion if f.timestamp == 7)
        self.assertGreater(recovered.decode(message)["SpeedKph"], next(iter(values)))
        self.assertTrue(all(f.checksum_valid for f in motion))

    def test_door_obstruction_holds_departure(self):
        sim = Simulator(short_profile(), faults=[Fault("door_obstruction", 2, 4)])
        list(sim.frames(5))
        self.assertEqual(sim.model.speed_mps, 0)
        self.assertEqual(sim.model.telemetry["doors_locked"], 0)
        list(sim.frames(2))
        self.assertGreater(sim.model.speed_mps, 0)

    def test_emergency_deceleration_and_interlock(self):
        sim = Simulator(short_profile(), faults=[Fault("emergency_brake", 7, 6)])
        list(sim.frames(7))
        before = sim.model.speed_mps
        for _ in range(100):
            sim.tick()
            self.assertEqual(sim.model.telemetry["emergency"], 1)
            self.assertEqual(sim.model.telemetry["traction_pct"], 0)
        self.assertLess(sim.model.speed_mps, before)
        list(sim.frames(5))
        self.assertEqual(sim.model.speed_mps, 0)

    def test_all_preset_scenarios_encode(self):
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertGreater(sum(1 for f in Simulator(scenario=name).frames(80)), 4000)

    def test_invalid_faults_and_durations(self):
        for args in [("bad", 0, 1), ("stuck_speed", -1, 1), ("stuck_speed", 0, 0)]:
            with self.assertRaises(ValueError):
                Fault(*args)
        with self.assertRaises(ValueError):
            Simulator(faults=[Fault("message_dropout", 0, 1, "Unknown")])
        for duration in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                list(Simulator().frames(duration))

    def test_clear_faults(self):
        sim = Simulator(faults=[Fault("counter_freeze", 0, 1)])
        sim.tick()
        sim.clear_faults()
        motion = next(f for f in sim.frames(.1) if f.can_id == 0x100)
        self.assertEqual(motion.data[6], 1)
