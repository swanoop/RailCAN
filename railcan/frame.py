"""Classical CAN packing, decoding and a demonstrator integrity byte."""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor

from .profile import Message, Profile, finite, integer


def checksum(can_id: int, data: bytes) -> int:
    """XOR the four little-endian ID bytes and first seven payload bytes.

    This is an application-level demonstration checksum, not CAN's wire CRC.
    """
    return reduce(xor, can_id.to_bytes(4, "little") + data[:7], 0)


@dataclass(frozen=True)
class Frame:
    timestamp: float
    can_id: int
    data: bytes
    extended: bool = False

    def __post_init__(self) -> None:
        if finite(self.timestamp, "timestamp") < 0:
            raise ValueError("Frame timestamp cannot be negative")
        if not isinstance(self.extended, bool):
            raise ValueError("extended must be true or false")
        limit = 0x1FFFFFFF if self.extended else 0x7FF
        if not 0 <= integer(self.can_id, "can_id") <= limit:
            raise ValueError("CAN ID out of range")
        if not isinstance(self.data, bytes) or len(self.data) > 8:
            raise ValueError("Classical CAN data must be bytes of length 0–8")

    @property
    def hex_id(self) -> str:
        return f"{self.can_id:08X}" if self.extended else f"{self.can_id:03X}"

    @property
    def checksum_valid(self) -> bool | None:
        if len(self.data) != 8:
            return None
        return checksum(self.can_id, self.data) == self.data[7]

    def matches(self, message: Message) -> bool:
        if self.extended != message.extended or len(self.data) != message.dlc:
            return False
        if self.can_id == message.can_id:
            return True
        if message.pgn is not None:
            from .j1939 import pgn_of
            return pgn_of(self.can_id) == message.pgn
        return False

    def decode(self, message: Message) -> dict[str, float | int]:
        if not self.matches(message):
            raise ValueError(f"Frame does not match the {message.dlc}-byte {message.name} definition")
        payload = int.from_bytes(self.data, "little")
        result = {signal.name: signal.decode(payload) for signal in message.signals}
        if message.has_trailer:
            result.update(AliveCounter=self.data[6], Checksum=self.data[7])
        return result

    def integrity(self, message: Message) -> bool | None:
        """Checksum result for messages that carry the demonstrator trailer, otherwise None."""
        return self.checksum_valid if message.has_trailer else None

    def record(self, profile: Profile) -> dict:
        message = profile.lookup(self.can_id, self.extended)
        known = message is not None and self.matches(message)
        record = {"timestamp": round(self.timestamp, 6), "can_id": self.can_id,
                  "can_id_hex": self.hex_id, "extended": self.extended,
                  "dlc": len(self.data), "data_hex": self.data.hex().upper(),
                  "message": message.name if known else "Unknown",
                  "signals": self.decode(message) if known else {},
                  "checksum_valid": self.integrity(message) if known else None}
        if not known and self.extended:
            from .j1939 import KNOWN_PGNS, PGN_DM1, decode_dm1, pgn_of
            pgn = pgn_of(self.can_id)
            if pgn in KNOWN_PGNS:
                record["message"] = KNOWN_PGNS[pgn]
                if pgn == PGN_DM1 and len(self.data) == 8:
                    dm1 = decode_dm1(self.data)
                    record["signals"] = {"AmberWarningLamp": int(dm1["amber_warning_lamp"]),
                                         "DTCCount": len(dm1["dtcs"]),
                                         **({"SPN": dm1["dtcs"][0]["spn"], "FMI": dm1["dtcs"][0]["fmi"]}
                                            if dm1["dtcs"] else {})}
        return record


def encode(message: Message, telemetry: dict, counter: int, timestamp: float) -> Frame:
    bits = message.dlc * 8
    payload = int.from_bytes(bytes([message.fill]) * message.dlc, "little")
    for signal in message.signals:
        mask = ((1 << signal.length) - 1) << signal.start
        payload = (payload & ~mask) | (signal.encode(telemetry[signal.source]) << signal.start)
    if message.has_trailer:
        payload = (payload & ~(0xFF << 48)) | (counter & 0xFF) << 48
    data = (payload & ((1 << bits) - 1)).to_bytes(message.dlc, "little")
    if message.has_trailer:
        data = data[:7] + bytes([checksum(message.can_id, data)])
    return Frame(timestamp, message.can_id, data, message.extended)
