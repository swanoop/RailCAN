"""Validated, declarative signal profiles; no external DBC dependency."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

TICK_MS = 10
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


@dataclass(frozen=True)
class Signal:
    name: str
    source: str
    start: int
    length: int
    scale: float = 1.0
    offset: float = 0.0
    minimum: float = 0.0
    maximum: float = 255.0
    unit: str = ""
    signed: bool = False

    def encode(self, value: float) -> int:
        number = finite(value, self.name)
        if not self.minimum <= number <= self.maximum:
            raise ValueError(f"{self.name}={number} outside [{self.minimum}, {self.maximum}]")
        raw = round((number - self.offset) / self.scale)
        low = -(1 << (self.length - 1)) if self.signed else 0
        high = (1 << (self.length - int(self.signed))) - 1
        if not low <= raw <= high:
            raise ValueError(f"{self.name} cannot be encoded in {self.length} bits")
        return raw & ((1 << self.length) - 1)

    def decode(self, payload: int) -> float:
        raw = (payload >> self.start) & ((1 << self.length) - 1)
        if self.signed and raw & (1 << (self.length - 1)):
            raw -= 1 << self.length
        return round(raw * self.scale + self.offset, 9)


@dataclass(frozen=True)
class Message:
    name: str
    can_id: int
    period_ms: int
    signals: tuple[Signal, ...]
    sender: str = "TCMS"
    extended: bool = False


@dataclass(frozen=True)
class Train:
    target_speed_kph: float = 90.0
    acceleration_mps2: float = 0.65
    braking_mps2: float = 0.75
    emergency_mps2: float = 1.2
    dwell_s: float = 12.0
    cruise_s: float = 30.0
    mass_kg: float = 160000.0
    passengers: int = 240
    ambient_c: float = 14.0
    cabin_c: float = 21.0
    supply_voltage: float = 750.0


@dataclass(frozen=True)
class Profile:
    name: str
    train: Train
    messages: tuple[Message, ...]
    bitrate: int = 250000

    @property
    def by_id(self) -> dict[int, Message]:
        return {message.can_id: message for message in self.messages}

    @property
    def frames_per_second(self) -> float:
        return sum(1000 / message.period_ms for message in self.messages)

    @property
    def nominal_bus_load_percent(self) -> float:
        # Includes intermission, excludes bit stuffing, arbitration delays, errors.
        return sum((111 + 20 * message.extended) * 1000 / message.period_ms
                   for message in self.messages) / self.bitrate * 100

    def to_dict(self) -> dict:
        return {"name": self.name, "bitrate": self.bitrate, "train": asdict(self.train),
                "messages": [asdict(message) for message in self.messages]}

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        if not isinstance(data, dict):
            raise ValueError("Profile must be a JSON object")
        unknown = set(data) - {"name", "bitrate", "train", "messages"}
        if unknown:
            raise ValueError(f"Unknown profile keys: {', '.join(sorted(unknown))}")
        try:
            train = Train(**data.get("train", {}))
            messages = []
            for item in data["messages"]:
                values = dict(item)
                if isinstance(values["can_id"], str):
                    values["can_id"] = int(values["can_id"], 0)
                values["signals"] = tuple(Signal(**signal) for signal in values["signals"])
                messages.append(Message(**values))
            profile = cls(data.get("name", "CustomTrain"), train, tuple(messages), data.get("bitrate", 250000))
        except (TypeError, KeyError, OverflowError) as exc:
            raise ValueError(f"Invalid profile structure: {exc}") from exc
        profile.validate()
        return profile

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 80:
            raise ValueError("Profile name must be 1–80 characters")
        if not 10000 <= integer(self.bitrate, "bitrate") <= 1000000:
            raise ValueError("bitrate must be between 10000 and 1000000")
        bounds = {
            "target_speed_kph": (1, 250), "acceleration_mps2": (0.05, 2),
            "braking_mps2": (0.05, 3), "emergency_mps2": (0.1, 3),
            "dwell_s": (3, 600), "cruise_s": (1, 1800), "mass_kg": (1000, 1000000),
            "ambient_c": (-40, 60), "cabin_c": (-20, 60), "supply_voltage": (1, 2000),
        }
        for key, (low, high) in bounds.items():
            if not low <= finite(getattr(self.train, key), key) <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        if not 0 <= integer(self.train.passengers, "passengers") <= 2000:
            raise ValueError("passengers must be between 0 and 2000")
        if not 1 <= len(self.messages) <= 64:
            raise ValueError("Profile must contain 1–64 messages")
        ids, names, sources = set(), set(), set(asdict(self.train)) | MODEL_SOURCES
        for message in self.messages:
            if not isinstance(message.extended, bool):
                raise ValueError("extended must be true or false")
            maximum_id = 0x1FFFFFFF if message.extended else 0x7FF
            if not 0 <= integer(message.can_id, "can_id") <= maximum_id:
                raise ValueError(f"Invalid CAN ID for {message.name}")
            if message.can_id in ids or message.name in names:
                raise ValueError("Message names and CAN IDs must be unique")
            ids.add(message.can_id)
            names.add(message.name)
            for token in (message.name, message.sender):
                if not isinstance(token, str) or not IDENTIFIER.fullmatch(token):
                    raise ValueError("Message and sender names must be valid DBC identifiers")
            period = integer(message.period_ms, "period_ms")
            if not TICK_MS <= period <= 60000 or period % TICK_MS:
                raise ValueError(f"{message.name}: period_ms must be a multiple of {TICK_MS}, up to 60000")
            if not 1 <= len(message.signals) <= 48:
                raise ValueError(f"{message.name}: expected 1–48 signals")
            occupied, signal_names = set(), set()
            for signal in message.signals:
                if not isinstance(signal.name, str) or not IDENTIFIER.fullmatch(signal.name):
                    raise ValueError("Signal names must be valid DBC identifiers")
                if signal.name in signal_names or signal.name in {"AliveCounter", "Checksum"}:
                    raise ValueError("Signal names must be unique and cannot use trailer names")
                signal_names.add(signal.name)
                if signal.source not in sources:
                    raise ValueError(f"Unknown telemetry source: {signal.source}")
                if not isinstance(signal.signed, bool):
                    raise ValueError("signed must be true or false")
                start, length = integer(signal.start, "start"), integer(signal.length, "length")
                if start < 0 or length < 1 or start + length > 48:
                    raise ValueError("Signals must fit in bits 0–47; bits 48–63 are reserved")
                bits = set(range(start, start + length))
                if occupied & bits:
                    raise ValueError(f"Overlapping signal: {message.name}.{signal.name}")
                occupied |= bits
                if finite(signal.scale, "scale") <= 0:
                    raise ValueError("scale must be positive")
                finite(signal.offset, "offset")
                if finite(signal.minimum, "minimum") > finite(signal.maximum, "maximum"):
                    raise ValueError("minimum must not exceed maximum")
                if not isinstance(signal.unit, str) or any(c in signal.unit for c in '\"\n\r\\'):
                    raise ValueError("Invalid unit string")
                signal.encode(signal.minimum)
                signal.encode(signal.maximum)
        if self.nominal_bus_load_percent > 80:
            raise ValueError("Nominal traffic exceeds 80% bus load; increase periods or bitrate")


MODEL_SOURCES = {
    "speed_kph", "acceleration", "distance_m", "mode", "emergency", "motor_rpm",
    "traction_pct", "motor_temp_c", "brake_pipe_bar", "brake_cylinder_bar",
    "brake_pct", "parking_brake", "brake_fault", "left_doors", "right_doors",
    "doors_locked", "interlock", "door_obstruction", "door_fault", "station_index",
    "dc_current_a", "battery_voltage", "hvac_state", "setpoint_c", "uptime_s", "fault_code",
}


def default_profile() -> Profile:
    def sig(name, source, start, length, scale=1, minimum=0, maximum=255, unit="", signed=False):
        return Signal(name, source, start, length, scale, 0, minimum, maximum, unit, signed)

    profile = Profile("EMU_Demo", Train(), (
        Message("Motion", 0x100, 50, (
            sig("SpeedKph", "speed_kph", 0, 16, .01, 0, 250, "km/h"),
            sig("Acceleration", "acceleration", 16, 16, .001, -3, 3, "m/s2", True),
            sig("OperatingMode", "mode", 32, 8, maximum=5),
            sig("EmergencyBrake", "emergency", 40, 1, maximum=1),
        ), "VCU"),
        Message("Position", 0x101, 100, (
            sig("DistanceM", "distance_m", 0, 32, .1, 0, 429496729.5, "m"),
            sig("TargetSpeed", "target_speed_kph", 32, 16, .01, 0, 250, "km/h"),
        ), "VCU"),
        Message("Traction", 0x200, 100, (
            sig("MotorRPM", "motor_rpm", 0, 16, maximum=12000, unit="rpm"),
            sig("TractionDemand", "traction_pct", 16, 16, .1, 0, 100, "%"),
            sig("MotorTemp", "motor_temp_c", 32, 16, .1, -40, 200, "degC", True),
        ), "TractionECU"),
        Message("Braking", 0x201, 100, (
            sig("BrakePipe", "brake_pipe_bar", 0, 16, .001, 0, 10, "bar"),
            sig("BrakeCylinder", "brake_cylinder_bar", 16, 16, .001, 0, 10, "bar"),
            sig("BrakeDemand", "brake_pct", 32, 8, .5, 0, 100, "%"),
            sig("Emergency", "emergency", 40, 1, maximum=1),
            sig("ParkingBrake", "parking_brake", 41, 1, maximum=1),
            sig("BrakeFault", "brake_fault", 42, 1, maximum=1),
        ), "BrakeECU"),
        Message("Doors", 0x300, 200, (
            sig("LeftDoorMask", "left_doors", 0, 8),
            sig("RightDoorMask", "right_doors", 8, 8),
            sig("DoorsLocked", "doors_locked", 16, 1, maximum=1),
            sig("TractionInterlock", "interlock", 17, 1, maximum=1),
            sig("Obstruction", "door_obstruction", 18, 1, maximum=1),
            sig("DoorFault", "door_fault", 19, 1, maximum=1),
            sig("PassengerCount", "passengers", 24, 16, maximum=2000),
            sig("StationIndex", "station_index", 40, 8),
        ), "DoorECU"),
        Message("Power", 0x400, 250, (
            sig("DCVoltage", "supply_voltage", 0, 16, .1, 0, 2000, "V"),
            sig("DCCurrent", "dc_current_a", 16, 16, .1, -3000, 3000, "A", True),
            sig("BatteryVoltage", "battery_voltage", 32, 16, .01, 0, 150, "V"),
        ), "PowerECU"),
        Message("HVAC", 0x500, 500, (
            sig("CabinTemp", "cabin_c", 0, 16, .1, -40, 100, "degC", True),
            sig("AmbientTemp", "ambient_c", 16, 16, .1, -40, 100, "degC", True),
            sig("HVACState", "hvac_state", 32, 8, maximum=3),
            sig("Setpoint", "setpoint_c", 40, 8, .5, 0, 50, "degC"),
        ), "HVACECU"),
        Message("Diagnostics", 0x600, 1000, (
            sig("Uptime", "uptime_s", 0, 32, .1, 0, 429496729.5, "s"),
            sig("FaultCode", "fault_code", 32, 16, maximum=65535),
        ), "TCMS"),
    ))
    profile.validate()
    return profile


def export_dbc(profile: Profile) -> str:
    """Export Intel/little-endian signals, cycle times, counters and checksum."""
    profile.validate()
    lines = ['VERSION "RailCAN synthetic profile 0.1"', '', 'NS_ :', '    CM_',
             '    BA_DEF_', '    BA_', '', 'BS_:', '',
             'BU_: Analyzer ' + ' '.join(sorted({message.sender for message in profile.messages})), '',
             'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 60000;', 'BA_DEF_DEF_ "GenMsgCycleTime" 0;', '']
    for message in profile.messages:
        dbc_id = message.can_id | (0x80000000 if message.extended else 0)
        lines.append(f'BO_ {dbc_id} {message.name}: 8 {message.sender}')
        for signal in message.signals:
            sign = '-' if signal.signed else '+'
            lines.append(f' SG_ {signal.name} : {signal.start}|{signal.length}@1{sign} '
                         f'({signal.scale:g},{signal.offset:g}) [{signal.minimum:g}|{signal.maximum:g}] '
                         f'"{signal.unit}" Analyzer')
        lines.extend([' SG_ AliveCounter : 48|8@1+ (1,0) [0|255] "" Analyzer',
                      ' SG_ Checksum : 56|8@1+ (1,0) [0|255] "" Analyzer', ''])
        lines.append(f'BA_ "GenMsgCycleTime" BO_ {dbc_id} {message.period_ms};')
        lines.append(f'CM_ BO_ {dbc_id} "Synthetic train telemetry; checksum is XOR of ID bytes and payload bytes 0-6.";')
        lines.append('')
    return '\n'.join(lines) + '\n'
