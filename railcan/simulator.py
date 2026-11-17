"""Integer-tick scheduling, seeded scenarios and finite fault windows."""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace

from .frame import Frame, encode
from .j1939 import BAM_PACKET_GAP_MS, DTC, PGN_DM1, bam_frames, compose_id, dm1_payload
from .model import FAULT_CODES, TrainModel
from .profile import (
    SEQUENCE_SOURCE,
    TICK_MS,
    Profile,
    default_profile,
    finite,
    integer,
    parse_waveform,
)

FAULT_KINDS = tuple(FAULT_CODES)
FRAME_FAULTS = {"stuck_speed", "message_dropout", "checksum_error", "counter_freeze", "speed_spike",
                "freeze_message"}
TRAILER_FAULTS = {"checksum_error", "counter_freeze"}


def waveform_value(kind: str, args: tuple[float, ...], t: float, rng: random.Random) -> float:
    if kind == "const":
        return args[0]
    low, high, period = args
    if kind == "random":
        return rng.uniform(low, high)
    if kind == "step":
        return high if t >= period else low
    phase = (t % period) / period
    if kind == "sine":
        return low + (high - low) * (1 - math.cos(2 * math.pi * phase)) / 2
    if kind == "ramp":
        return low + (high - low) * phase
    if kind == "square":
        return high if phase >= .5 else low
    return low + (high - low) * (1 - abs(2 * phase - 1))  # triangle
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
    "engine_overheat": {"label": "Engine overheat", "description": "Coolant over-temperature from 20 to 80 seconds; DM1 reports it."},
    "low_oil_pressure": {"label": "Low oil pressure", "description": "Oil pressure collapses from 20 to 40 seconds; DM1 reports it."},
    "multi_dtc": {"label": "Multiple DTCs", "description": "Two diagnostics active from 15 to 45 seconds; DM1 uses TP.BAM."},
}


@dataclass(frozen=True)
class Fault:
    kind: str
    start_s: float
    duration_s: float
    message: str | None = None
    value: float | None = None
    spn: int | None = None
    fmi: int | None = None

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
        if self.kind == "dtc" or self.spn is not None or self.fmi is not None:
            if self.spn is None or self.fmi is None:
                raise ValueError("A dtc fault needs spn and fmi")
            DTC(integer(self.spn, "spn"), integer(self.fmi, "fmi"))

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
        "engine_overheat": [Fault("coolant_overtemp", 20, 60)],
        "low_oil_pressure": [Fault("low_oil_pressure", 20, 20)],
        "multi_dtc": [Fault("dtc", 15, 30, spn=110, fmi=0), Fault("dtc", 15, 30, spn=100, fmi=1)],
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
        self.sequence = {message.can_id: 0 for message in self.profile.messages}
        self.waveforms = {}
        for message in self.profile.messages:
            for signal in message.signals:
                wave = parse_waveform(signal.source)
                if wave:
                    self.waveforms[signal.source] = wave
        self.wave_random = random.Random(seed ^ 0x5A17)
        self._pending: list[tuple[int, int, int, bytes]] = []  # (due_ms, order, can_id, data) for TP.DT
        self._pending_order = 0
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
        if fault.kind in TRAILER_FAULTS:
            target = next(m for m in self.profile.messages if m.name == message_name)
            if not target.has_trailer:
                raise ValueError(f"'{message_name}' has no counter/checksum trailer")
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
        for source, (kind, args) in self.waveforms.items():
            self.model.telemetry[source] = waveform_value(kind, args, now, self.wave_random)
        frames = []
        while self._pending and self._pending[0][0] <= self.elapsed_ms:
            _, _, can_id, data = heapq.heappop(self._pending)
            frames.append(Frame(now, can_id, data, True))
        dm1 = self.profile.dm1
        # A new BAM never interleaves with an unfinished one from the same source.
        if dm1 is not None and self.elapsed_ms % dm1.period_ms == 0 and not self._pending:
            frames.extend(self._dm1_frames(dm1, active, now))
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
            telemetry[SEQUENCE_SOURCE] = self.sequence[message.can_id]
            self.sequence[message.can_id] = (self.sequence[message.can_id] + 1) & 0xFFFFFFFF
            for index, fault in selected:
                if fault.kind == "stuck_speed":
                    telemetry["speed_kph"] = self._frozen.setdefault((index, "speed"), telemetry["speed_kph"])
                elif fault.kind == "speed_spike":
                    telemetry["speed_kph"] = fault.value if fault.value is not None else 160
                elif fault.kind == "freeze_message":
                    # Signal values stick at their first value in the window; sequence numbers continue.
                    frozen = self._frozen.setdefault((index, "message"), dict(telemetry))
                    telemetry = {**frozen, SEQUENCE_SOURCE: telemetry[SEQUENCE_SOURCE]}
                elif fault.kind == "counter_freeze":
                    counter = int(self._frozen.setdefault((index, "counter"), counter))
            frame = encode(message, telemetry, counter, now)
            if any(fault.kind == "checksum_error" for _, fault in selected):
                frame = replace(frame, data=frame.data[:7] + bytes([frame.data[7] ^ 0xFF]))
            frames.append(frame)
        frames.sort(key=lambda frame: (frame.can_id, frame.extended))
        self.timestamp = now
        self.elapsed_ms += TICK_MS
        return frames

    def active_dtcs(self, active: tuple[Fault, ...]) -> list[DTC]:
        mapping = {kind: (spn, fmi) for kind, spn, fmi in self.profile.dm1.fault_dtcs} if self.profile.dm1 else {}
        found = []
        for fault in active:
            if fault.kind == "dtc":
                dtc = DTC(fault.spn, fault.fmi)
            elif fault.kind in mapping:
                dtc = DTC(*mapping[fault.kind])
            else:
                continue
            if all((d.spn, d.fmi) != (dtc.spn, dtc.fmi) for d in found):
                found.append(dtc)
        return found

    def _dm1_frames(self, dm1, active: tuple[Fault, ...], now: float) -> list[Frame]:
        payload = dm1_payload(self.active_dtcs(active))
        if len(payload) == 8:
            return [Frame(now, compose_id(PGN_DM1, dm1.priority, dm1.source_address), payload, True)]
        packets = bam_frames(PGN_DM1, payload, 7, dm1.source_address)
        for number, (can_id, data) in enumerate(packets[1:], 1):
            self._pending_order += 1
            heapq.heappush(self._pending, (self.elapsed_ms + number * BAM_PACKET_GAP_MS,
                                           self._pending_order, can_id, data))
        return [Frame(now, packets[0][0], packets[0][1], True)]

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
