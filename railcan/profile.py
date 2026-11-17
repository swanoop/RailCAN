"""Validated, declarative signal profiles; no external DBC dependency."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

TICK_MS = 10
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TRAILERS = ("counter_xor", "none")
WAVEFORM = re.compile(r"^(sine|ramp|square|triangle|step|random|const)\((.*)\)$")
WAVEFORM_ARGS = {"sine": 3, "ramp": 3, "square": 3, "triangle": 3, "step": 3, "random": 2, "const": 1}
SEQUENCE_SOURCE = "seq"


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
    trailer: str = "counter_xor"
    dlc: int = 8
    fill: int = 0
    pgn: int | None = None

    @property
    def has_trailer(self) -> bool:
        return self.trailer == "counter_xor"

    @property
    def signal_bits(self) -> int:
        """Bits available to signals; the counter/checksum trailer owns bytes 6–7."""
        return 48 if self.has_trailer else self.dlc * 8

    @property
    def frame_bits(self) -> int:
        # Includes intermission, excludes bit stuffing, arbitration delays, errors.
        return (67 if self.extended else 47) + 8 * self.dlc


@dataclass(frozen=True)
class DM1Config:
    """J1939 DM1 active-diagnostic broadcast built from active fault windows."""
    source_address: int = 0
    period_ms: int = 1000
    priority: int = 6
    fault_dtcs: tuple[tuple[str, int, int], ...] = ()


def parse_waveform(source: str) -> tuple[str, tuple[float, ...]] | None:
    """Return (kind, arguments) for a waveform source such as sine(0,100,10)."""
    match = WAVEFORM.fullmatch(source.replace(" ", "")) if isinstance(source, str) else None
    if not match:
        return None
    kind, text = match.groups()
    try:
        args = tuple(float(item) for item in text.split(",")) if text else ()
    except ValueError as exc:
        raise ValueError(f"Waveform arguments must be numbers: {source}") from exc
    if len(args) != WAVEFORM_ARGS[kind] or not all(math.isfinite(a) for a in args):
        raise ValueError(f"{kind}() expects {WAVEFORM_ARGS[kind]} finite numbers: {source}")
    if kind in {"sine", "ramp", "square", "triangle"} and args[2] <= 0:
        raise ValueError(f"{kind}() period must be positive: {source}")
    if kind == "step" and args[2] < 0:
        raise ValueError(f"step() time must be nonnegative: {source}")
    return kind, args


def waveform_bounds(kind: str, args: tuple[float, ...]) -> tuple[float, float]:
    if kind == "const":
        return args[0], args[0]
    return min(args[0], args[1]), max(args[0], args[1])


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
    dm1: DM1Config | None = None

    @property
    def by_id(self) -> dict[int, Message]:
        return {message.can_id: message for message in self.messages}

    def lookup(self, can_id: int, extended: bool) -> Message | None:
        """Exact ID first; for J1939 traffic, fall back to the PGN, ignoring priority and source."""
        message = self.by_id.get(can_id)
        if message is not None and message.extended == extended:
            return message
        if extended:
            from .j1939 import pgn_of
            pgn = pgn_of(can_id)
            for message in self.messages:
                if message.pgn == pgn:
                    return message
        return None

    @property
    def frames_per_second(self) -> float:
        return sum(1000 / message.period_ms for message in self.messages)

    @property
    def nominal_bus_load_percent(self) -> float:
        # Includes intermission, excludes bit stuffing, arbitration delays, errors.
        load = sum(message.frame_bits * 1000 / message.period_ms for message in self.messages)
        if self.dm1:
            load += 131 * 1000 / self.dm1.period_ms
        return load / self.bitrate * 100

    def to_dict(self) -> dict:
        data = {"name": self.name, "bitrate": self.bitrate, "train": asdict(self.train),
                "messages": [asdict(message) for message in self.messages]}
        if self.dm1:
            dm1 = asdict(self.dm1)
            dm1["fault_dtcs"] = {kind: {"spn": spn, "fmi": fmi} for kind, spn, fmi in self.dm1.fault_dtcs}
            data["dm1"] = dm1
        return data

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        if not isinstance(data, dict):
            raise ValueError("Profile must be a JSON object")
        unknown = set(data) - {"name", "bitrate", "train", "messages", "dm1"}
        if unknown:
            raise ValueError(f"Unknown profile keys: {', '.join(sorted(unknown))}")
        from .j1939 import compose_id
        try:
            train = Train(**data.get("train", {}))
            messages = []
            for item in data["messages"]:
                values = dict(item)
                # J1939 shorthand: give pgn (+ priority, source_address, destination) instead of can_id.
                j1939 = {key: values.pop(key) for key in ("priority", "source_address", "destination") if key in values}
                for key in ("can_id", "pgn"):
                    if isinstance(values.get(key), str):
                        values[key] = int(values[key], 0)
                if values.get("pgn") is not None and "can_id" not in values:
                    values["can_id"] = compose_id(values["pgn"], j1939.get("priority", 6),
                                                  j1939.get("source_address", 0), j1939.get("destination", 0xFF))
                    values.setdefault("extended", True)
                    values.setdefault("trailer", "none")
                    values.setdefault("fill", 0xFF)
                elif j1939:
                    raise ValueError("priority/source_address/destination need a pgn")
                values["signals"] = tuple(Signal(**signal) for signal in values["signals"])
                messages.append(Message(**values))
            dm1 = None
            if data.get("dm1") is not None:
                values = dict(data["dm1"])
                mapping = values.pop("fault_dtcs", {}) or {}
                if not isinstance(mapping, dict):
                    raise ValueError("dm1.fault_dtcs must map fault kinds to {spn, fmi}")
                values["fault_dtcs"] = tuple((kind, item["spn"], item["fmi"]) for kind, item in mapping.items())
                dm1 = DM1Config(**values)
            profile = cls(data.get("name", "CustomTrain"), train, tuple(messages), data.get("bitrate", 250000), dm1)
        except (TypeError, KeyError, OverflowError, AttributeError) as exc:
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
            if message.trailer not in TRAILERS:
                raise ValueError(f"{message.name}: trailer must be one of {', '.join(TRAILERS)}")
            if not 1 <= integer(message.dlc, "dlc") <= 8 or (message.has_trailer and message.dlc != 8):
                raise ValueError(f"{message.name}: dlc must be 1–8, and 8 with a counter_xor trailer")
            if not 0 <= integer(message.fill, "fill") <= 255:
                raise ValueError(f"{message.name}: fill must be a byte value 0–255")
            if message.pgn is not None:
                from .j1939 import compose_id, split_id
                integer(message.pgn, "pgn")
                if not message.extended:
                    raise ValueError(f"{message.name}: a J1939 pgn requires a 29-bit (extended) ID")
                priority, _, source, destination = split_id(message.can_id)
                if compose_id(message.pgn, priority, source, destination) != message.can_id:
                    raise ValueError(f"{message.name}: can_id does not carry pgn {message.pgn}")
            period = integer(message.period_ms, "period_ms")
            if not TICK_MS <= period <= 60000 or period % TICK_MS:
                raise ValueError(f"{message.name}: period_ms must be a multiple of {TICK_MS}, up to 60000")
            if not 1 <= len(message.signals) <= 48:
                raise ValueError(f"{message.name}: expected 1–48 signals")
            occupied, signal_names = set(), set()
            for signal in message.signals:
                if not isinstance(signal.name, str) or not IDENTIFIER.fullmatch(signal.name):
                    raise ValueError("Signal names must be valid DBC identifiers")
                if signal.name in signal_names or (message.has_trailer and signal.name in {"AliveCounter", "Checksum"}):
                    raise ValueError("Signal names must be unique and cannot use trailer names")
                signal_names.add(signal.name)
                wave = parse_waveform(signal.source)
                if signal.source not in sources and signal.source != SEQUENCE_SOURCE and wave is None:
                    raise ValueError(f"Unknown telemetry source: {signal.source}")
                if not isinstance(signal.signed, bool):
                    raise ValueError("signed must be true or false")
                start, length = integer(signal.start, "start"), integer(signal.length, "length")
                if start < 0 or length < 1 or start + length > message.signal_bits:
                    if message.has_trailer:
                        raise ValueError("Signals must fit in bits 0–47; bits 48–63 are reserved")
                    raise ValueError(f"{message.name}.{signal.name} must fit in bits 0–{message.signal_bits - 1}")
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
                if wave is not None:
                    low, high = waveform_bounds(*wave)
                    if low < signal.minimum or high > signal.maximum:
                        raise ValueError(f"{message.name}.{signal.name}: {signal.source} leaves "
                                         f"[{signal.minimum:g}, {signal.maximum:g}]")
        if self.dm1 is not None:
            dm1 = self.dm1
            if not 0 <= integer(dm1.source_address, "dm1 source_address") <= 253:
                raise ValueError("dm1 source_address must be 0–253")
            if not 0 <= integer(dm1.priority, "dm1 priority") <= 7:
                raise ValueError("dm1 priority must be 0–7")
            period = integer(dm1.period_ms, "dm1 period_ms")
            if not 100 <= period <= 60000 or period % TICK_MS:
                raise ValueError(f"dm1 period_ms must be a multiple of {TICK_MS} between 100 and 60000")
            for kind, spn, fmi in dm1.fault_dtcs:
                if not isinstance(kind, str):
                    raise ValueError("dm1 fault kind must be a string")
                if not 0 <= integer(spn, "spn") <= 0x7FFFF or not 0 <= integer(fmi, "fmi") <= 31:
                    raise ValueError("DTC spn must be 0–524287 and fmi 0–31")
        if self.nominal_bus_load_percent > 80:
            raise ValueError("Nominal traffic exceeds 80% bus load; increase periods or bitrate")


MODEL_SOURCES = {
    "speed_kph", "acceleration", "distance_m", "mode", "emergency", "motor_rpm",
    "traction_pct", "motor_temp_c", "brake_pipe_bar", "brake_cylinder_bar",
    "brake_pct", "parking_brake", "brake_fault", "left_doors", "right_doors",
    "doors_locked", "interlock", "door_obstruction", "door_fault", "station_index",
    "dc_current_a", "battery_voltage", "hvac_state", "setpoint_c", "uptime_s", "fault_code",
    "engine_speed_rpm", "engine_load_pct", "coolant_temp_c", "oil_pressure_kpa",
    "fuel_rate_lph", "engine_hours_h", "engine_running",
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


def j1939_profile() -> Profile:
    """Generic SAE J1939 engine profile built from public standard PGNs and SPN scalings.

    Unused bits are 0xFF and there is no counter/checksum trailer, as on a J1939 network.
    PGN 0xFF00 (proprietary B) carries a 32-bit sequence number for end-to-end tracing.
    """
    def message(name, pgn, priority, period, signals, sender="Engine"):
        from .j1939 import compose_id
        return Message(name, compose_id(pgn, priority, 0), period, tuple(signals), sender, True,
                       "none", 8, 0xFF, pgn)

    def sig(name, source, start, length, scale=1, offset=0, minimum=0, maximum=255, unit=""):
        return Signal(name, source, start, length, scale, offset, minimum, maximum, unit)

    profile = Profile("J1939_Engine_Demo", Train(), (
        message("EEC1", 0xF004, 3, 20, (
            sig("ActualEnginePercentTorque", "engine_load_pct", 16, 8, 1, -125, 0, 125, "%"),
            sig("EngineSpeed", "engine_speed_rpm", 24, 16, .125, 0, 0, 8031.875, "rpm"),
        )),
        message("EEC2", 0xF003, 3, 50, (
            sig("EnginePercentLoadAtCurrentSpeed", "engine_load_pct", 16, 8, 1, 0, 0, 250, "%"),
        )),
        message("LFE1", 0xFEF2, 6, 100, (
            sig("EngineFuelRate", "fuel_rate_lph", 0, 16, .05, 0, 0, 3212.75, "L/h"),
        )),
        message("EFL_P1", 0xFEEF, 6, 500, (
            sig("EngineOilPressure", "oil_pressure_kpa", 24, 8, 4, 0, 0, 1000, "kPa"),
        )),
        message("ET1", 0xFEEE, 6, 1000, (
            sig("EngineCoolantTemperature", "coolant_temp_c", 0, 8, 1, -40, -40, 210, "degC"),
        )),
        message("HOURS", 0xFEE5, 6, 1000, (
            sig("EngineTotalHoursOfOperation", "engine_hours_h", 0, 32, .05, 0, 0, 210554060.75, "h"),
        )),
        message("VEP1", 0xFEF7, 6, 1000, (
            sig("BatteryPotential", "battery_voltage", 32, 16, .05, 0, 0, 3212.75, "V"),
        )),
        message("Probe", 0xFF00, 6, 1000, (
            sig("ProbeSequence", "seq", 0, 32, 1, 0, 0, 4294967295, ""),
        ), "TestHarness"),
    ), 250000, DM1Config(0, 1000, 6, (("coolant_overtemp", 110, 0), ("low_oil_pressure", 100, 1))))
    profile.validate()
    return profile


BUILTIN_PROFILES = {"emu": default_profile, "j1939": j1939_profile}


def load_profile(reference: str | Path | None) -> Profile:
    """Load a built-in profile by name (emu, j1939) or a JSON profile file."""
    if reference is None:
        return default_profile()
    if str(reference) in BUILTIN_PROFILES and not Path(reference).exists():
        return BUILTIN_PROFILES[str(reference)]()
    return Profile.load(reference)


def export_dbc(profile: Profile) -> str:
    """Export Intel/little-endian signals, cycle times, counters and checksum."""
    profile.validate()
    lines = ['VERSION "RailCAN synthetic profile 0.1"', '', 'NS_ :', '    CM_',
             '    BA_DEF_', '    BA_', '', 'BS_:', '',
             'BU_: Analyzer ' + ' '.join(sorted({message.sender for message in profile.messages})), '',
             'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 60000;',
             'BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","reserved","J1939PG";',
             'BA_DEF_DEF_ "GenMsgCycleTime" 0;', 'BA_DEF_DEF_ "VFrameFormat" "StandardCAN";', '']
    for message in profile.messages:
        dbc_id = message.can_id | (0x80000000 if message.extended else 0)
        lines.append(f'BO_ {dbc_id} {message.name}: {message.dlc} {message.sender}')
        for signal in message.signals:
            sign = '-' if signal.signed else '+'
            lines.append(f' SG_ {signal.name} : {signal.start}|{signal.length}@1{sign} '
                         f'({signal.scale:g},{signal.offset:g}) [{signal.minimum:g}|{signal.maximum:g}] '
                         f'"{signal.unit}" Analyzer')
        if message.has_trailer:
            lines.extend([' SG_ AliveCounter : 48|8@1+ (1,0) [0|255] "" Analyzer',
                          ' SG_ Checksum : 56|8@1+ (1,0) [0|255] "" Analyzer'])
        lines.append('')
        lines.append(f'BA_ "GenMsgCycleTime" BO_ {dbc_id} {message.period_ms};')
        if message.extended:
            lines.append(f'BA_ "VFrameFormat" BO_ {dbc_id} {3 if message.pgn is not None else 1};')
        if message.has_trailer:
            lines.append(f'CM_ BO_ {dbc_id} "Synthetic telemetry; checksum is XOR of ID bytes and payload bytes 0-6.";')
        elif message.pgn is not None:
            lines.append(f'CM_ BO_ {dbc_id} "Synthetic J1939 PGN {message.pgn} (0x{message.pgn:04X}); unused bits 0x{message.fill:02X}.";')
        lines.append('')
    return '\n'.join(lines) + '\n'
