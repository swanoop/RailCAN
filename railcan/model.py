"""A small correlated train model for test data, not a vehicle dynamics model."""

from __future__ import annotations

import math
import random
from dataclasses import asdict

from .profile import Train

MODES = {"station": 0, "accelerating": 1, "cruising": 2, "braking": 3, "emergency": 4, "depot": 5}
FAULT_CODES = {"emergency_brake": 1, "door_obstruction": 2, "hvac_overheat": 3,
               "low_voltage": 4, "stuck_speed": 5, "message_dropout": 6,
               "checksum_error": 7, "counter_freeze": 8, "speed_spike": 9}


class TrainModel:
    def __init__(self, train: Train, seed: int = 42, depot: bool = False):
        self.train = train
        self.random = random.Random(seed)
        self.depot = depot
        self.phase = "station"
        self.phase_time = 0.0
        self.speed_mps = 0.0
        self.distance = 0.0
        self.acceleration = 0.0
        self.station = 0
        self.motor_temp = 35.0
        self.cabin_temp = train.cabin_c
        self.telemetry = {}
        self.step(0, (), 0)

    def step(self, dt: float, active_faults: tuple, timestamp: float) -> None:
        active = {fault.kind for fault in active_faults}
        emergency = "emergency_brake" in active
        obstruction = "door_obstruction" in active and self.phase == "station"
        self.phase_time += dt
        target = min(self.train.target_speed_kph, 25) if self.depot else self.train.target_speed_kph
        target_mps = target / 3.6
        previous = self.speed_mps
        if emergency:
            self.phase = "emergency"
            self.phase_time = 0
        elif self.phase == "emergency":
            self.phase = "braking" if self.speed_mps > 0 else "station"
            self.phase_time = 0

        if self.phase == "station":
            self.speed_mps = 0
            if self.phase_time >= self.train.dwell_s and not obstruction:
                self.phase = "accelerating"
                self.phase_time = 0
        elif self.phase == "accelerating":
            self.speed_mps = min(target_mps, self.speed_mps + self.train.acceleration_mps2 * dt)
            if self.speed_mps >= target_mps:
                self.phase = "cruising"
                self.phase_time = 0
        elif self.phase == "cruising":
            self.speed_mps = target_mps
            cruise_s = min(self.train.cruise_s, 8) if self.depot else self.train.cruise_s
            if self.phase_time >= cruise_s:
                self.phase = "braking"
                self.phase_time = 0
        elif self.phase in {"braking", "emergency"}:
            deceleration = self.train.emergency_mps2 if emergency else self.train.braking_mps2
            self.speed_mps = max(0, self.speed_mps - deceleration * dt)
            if self.speed_mps == 0 and not emergency:
                self.phase = "station"
                self.phase_time = 0
                self.station = (self.station + 1) % 256

        self.acceleration = (self.speed_mps - previous) / dt if dt else 0
        self.distance += (previous + self.speed_mps) * .5 * dt
        doors_open = self.phase == "station" and (self.phase_time < self.train.dwell_s - 2 or obstruction)
        locked = not doors_open and not obstruction
        traction = 0.0
        if self.phase == "accelerating" and locked and not emergency:
            traction = 75.0
        elif self.phase == "cruising" and locked:
            traction = 12.0
        brake_pct = 100.0 if emergency else (70.0 if self.phase == "braking" else 0.0)
        parking = self.speed_mps == 0 and self.phase in {"station", "emergency"}
        heat_target = 35 + traction * .7
        self.motor_temp += (heat_target - self.motor_temp) * dt / 90
        overheat = "hvac_overheat" in active
        setpoint = 21.0
        if overheat:
            self.cabin_temp = min(60, self.cabin_temp + .6 * dt)
        else:
            # Small seeded fluctuations around a setpoint and ambient heat exchange.
            ambient_gain = (self.train.ambient_c - self.cabin_temp) * .003
            cooling = (setpoint - self.cabin_temp) * .04
            noise = self.random.gauss(0, .005) * math.sqrt(dt) if dt else 0
            self.cabin_temp += (ambient_gain + cooling) * dt + noise
        hvac = 3 if overheat else (2 if self.cabin_temp > setpoint + .3 else
                                 1 if self.cabin_temp < setpoint - .3 else 0)
        voltage = max(0, min(2000, self.train.supply_voltage + 3 * math.sin(timestamp * .3)))
        low_voltage = "low_voltage" in active
        if low_voltage:
            voltage *= .55
        mechanical_kw = self.train.mass_kg * self.acceleration * self.speed_mps / 1000
        current = (mechanical_kw * 1000 / max(voltage, 1)) + 40
        current = max(-3000, min(3000, current))
        fault_code = next((code for name, code in FAULT_CODES.items() if name in active), 0)
        telemetry = asdict(self.train)
        telemetry.update({
            "target_speed_kph": target,
            "speed_kph": round(self.speed_mps * 3.6, 5),
            "acceleration": round(self.acceleration, 6), "distance_m": self.distance,
            "mode": 5 if self.depot and self.phase == "cruising" else MODES[self.phase],
            "emergency": int(emergency), "motor_rpm": min(12000, round(self.speed_mps * 250)),
            "traction_pct": traction, "motor_temp_c": self.motor_temp,
            "brake_pipe_bar": max(0, 5 - brake_pct * .035),
            "brake_cylinder_bar": brake_pct * .035 + (1 if parking else 0),
            "brake_pct": brake_pct, "parking_brake": int(parking), "brake_fault": 0,
            "left_doors": 0xFF if doors_open and self.station % 2 == 0 else 0,
            "right_doors": 0xFF if doors_open and self.station % 2 else 0,
            "doors_locked": int(locked), "interlock": int(locked and not emergency),
            "door_obstruction": int(obstruction), "door_fault": int(obstruction),
            "station_index": self.station, "supply_voltage": voltage,
            "dc_current_a": current, "battery_voltage": 85 if low_voltage else 110.2,
            "cabin_c": self.cabin_temp, "hvac_state": hvac, "setpoint_c": setpoint,
            "uptime_s": timestamp, "fault_code": fault_code,
        })
        self.telemetry = telemetry

    def snapshot(self) -> dict:
        return {**self.telemetry, "phase": self.phase, "mode_label": self.phase.replace("_", " ")}
