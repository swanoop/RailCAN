"""Integer-tick scheduling, seeded scenarios and finite fault windows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator

from .frame import Frame, encode
from .model import FAULT_CODES, TrainModel
from .profile import Profile, TICK_MS, default_profile, finite, integer

FAULT_KINDS = tuple(FAULT_CODES)
FRAME_FAULTS = {"stuck_speed", "message_dropout", "checksum_error", "counter_freeze", "speed_spike"}
SCENARIOS = {
    "normal": {"label": "Normal service", "description": "Dwell, depart, cruise, brake and repeat."},
    "depot": {"label": "Depot movement", "description": "Low-speed operation, capped at 25 km/h."},
    "emergency_stop": {"label": "Emergency stop", "description": "Emergency brake from 35 to 53 seconds."},
    "door_obstruction": {"label": "Door obstruction", "description": "Obstruction from 8 to 26 seconds delays departure."},
    "hvac_overheat": {"label": "HVAC overheat", "description": "Cooling failure from 20 to 70 seconds heats the cabin."},
    "low_voltage": {"label": "Low voltage", "description": "Supply and battery undervoltage from 25 to 45 seconds."},
    "sensor_fault": {"label": "Frozen speed sensor", "description": "On-wire speed freezes from 20 to 40 seconds."},
    "message_dropout": {"label": "Traction ECU dropout", "description": "Traction messages disappear from 20 to 35 seconds."},
    "checksum_error": {"label": "Corrupt checksum", "description": "Bad Motion checksums from 20 to 30 seconds."},
    "counter_freeze": {"label": "Frozen counter", "description": "Motion alive counter freezes from 20 to 30 seconds."},
}


@dataclass(frozen=True)
class Fault:
    kind: str
    start_s: float
    duration_s: float
    message: str | None = None
    value: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in FAULT_KINDS:
            raise ValueError(f"Unknown fault: {self.kind}")
        if finite(self.start_s, "fault start_s") < 0:
            raise ValueError("Fault start must be nonnegative")
        if finite(self.duration_s, "fault duration_s") <= 0:
            raise ValueError("Fault duration must be positive")
        if self.value is not None:
            finite(self.value, "fault value")
        if self.message is not None and not isinstance(self.message, str):
            raise ValueError("Fault message must be a message name")

    def active(self, timestamp: float) -> bool:
        return self.start_s <= timestamp < self.start_s + self.duration_s


def scenario_faults(name: str) -> list[Fault]:
    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{name}'; choose from {', '.join(SCENARIOS)}")
    return {
        "emergency_stop": [Fault("emergency_brake", 35, 18)],
        "door_obstruction": [Fault("door_obstruction", 8, 18)],
        "hvac_overheat": [Fault("hvac_overheat", 20, 50)],
        "low_voltage": [Fault("low_voltage", 25, 20)],
        "sensor_fault": [Fault("stuck_speed", 20, 20, "Motion")],
        "message_dropout": [Fault("message_dropout", 20, 15, "Traction")],
        "checksum_error": [Fault("checksum_error", 20, 10, "Motion")],
        "counter_freeze": [Fault("counter_freeze", 20, 10, "Motion")],
    }.get(name, [])


class Simulator:
    def __init__(self, profile: Profile | None = None, scenario: str = "normal", seed: int = 42,
                 faults: list[Fault] | None = None):
        self.profile = profile or default_profile()
        self.profile.validate()
        integer(seed, "seed")
        self.scenario, self.seed = scenario, seed
        presets = scenario_faults(scenario)
        self.faults: list[Fault] = []
        self.elapsed_ms = 0
        self.timestamp = 0.0
        self.model = TrainModel(self.profile.train, seed, depot=scenario == "depot")
        self.counters = {message.can_id: 0 for message in self.profile.messages}
        self._frozen: dict[tuple[int, str], float] = {}
        for fault in presets if faults is None else faults:
            self.add_fault(fault)

    def add_fault(self, fault: Fault) -> None:
        if not isinstance(fault, Fault):
            raise ValueError("Fault must be a Fault object")
        if len(self.faults) >= 100:
            raise ValueError("A session supports at most 100 fault events")
        message_name = fault.message or ("Traction" if fault.kind == "message_dropout" else "Motion")
        if fault.kind in FRAME_FAULTS and not any(m.name == message_name for m in self.profile.messages):
            raise ValueError(f"Fault targets missing message '{message_name}'")
        if fault.kind in {"stuck_speed", "speed_spike"}:
            target = next(m for m in self.profile.messages if m.name == message_name)
            if not any(s.source == "speed_kph" for s in target.signals):
                raise ValueError(f"'{message_name}' has no speed_kph signal")
            if fault.kind == "speed_spike":
                for signal in target.signals:
                    if signal.source == "speed_kph":
                        signal.encode(fault.value if fault.value is not None else 160)
        self.faults.append(fault)

    def clear_faults(self) -> None:
        self.faults.clear()
        self._frozen.clear()

    @property
    def active_faults(self) -> tuple[Fault, ...]:
        return tuple(fault for fault in self.faults if fault.active(self.timestamp))

    def tick(self) -> list[Frame]:
        now = self.elapsed_ms / 1000
        active = tuple(fault for fault in self.faults if fault.active(now))
        self.model.step(0 if self.elapsed_ms == 0 else TICK_MS / 1000, active, now)
        frames = []
        for message in sorted(self.profile.messages, key=lambda item: item.can_id):
            if self.elapsed_ms % message.period_ms:
                continue
            selected = [(index, fault) for index, fault in enumerate(self.faults)
                        if fault.active(now) and fault.kind in FRAME_FAULTS and
                        (fault.message or ("Traction" if fault.kind == "message_dropout" else "Motion")) == message.name]
            counter = self.counters[message.can_id]
            # Source counters progress even during dropout, exposing missing frames.
            self.counters[message.can_id] = (counter + 1) & 0xFF
            if any(fault.kind == "message_dropout" for _, fault in selected):
                continue
            telemetry = dict(self.model.telemetry)
            for index, fault in selected:
                if fault.kind == "stuck_speed":
                    telemetry["speed_kph"] = self._frozen.setdefault((index, "speed"), telemetry["speed_kph"])
                elif fault.kind == "speed_spike":
                    telemetry["speed_kph"] = fault.value if fault.value is not None else 160
                elif fault.kind == "counter_freeze":
                    counter = int(self._frozen.setdefault((index, "counter"), counter))
            frame = encode(message, telemetry, counter, now)
            if any(fault.kind == "checksum_error" for _, fault in selected):
                frame = replace(frame, data=frame.data[:7] + bytes([frame.data[7] ^ 0xFF]))
            frames.append(frame)
        self.timestamp = now
        self.elapsed_ms += TICK_MS
        return frames

    def frames(self, duration_s: float) -> Iterator[Frame]:
        if not .01 <= finite(duration_s, "duration_s") <= 86400:
            raise ValueError("duration_s must be between 0.01 and 86400")
        stop_ms = self.elapsed_ms + round(duration_s * 1000)
        while self.elapsed_ms < stop_ms:
            yield from self.tick()

    def snapshot(self) -> dict:
        return {"time_s": self.timestamp, "scenario": self.scenario, "seed": self.seed,
                "telemetry": self.model.snapshot(),
                "active_faults": [fault.kind for fault in self.active_faults]}
