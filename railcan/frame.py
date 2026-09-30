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

    def decode(self, message: Message) -> dict[str, float | int]:
        if len(self.data) != 8 or self.can_id != message.can_id or self.extended != message.extended:
            raise ValueError("Frame does not match this eight-byte message definition")
        payload = int.from_bytes(self.data, "little")
        result = {signal.name: signal.decode(payload) for signal in message.signals}
        result.update(AliveCounter=self.data[6], Checksum=self.data[7])
        return result

    def record(self, profile: Profile) -> dict:
        message = profile.by_id.get(self.can_id)
        known = message is not None and self.extended == message.extended and len(self.data) == 8
        return {"timestamp": round(self.timestamp, 6), "can_id": self.can_id,
                "can_id_hex": self.hex_id, "extended": self.extended,
                "dlc": len(self.data), "data_hex": self.data.hex().upper(),
                "message": message.name if known else "Unknown",
                "signals": self.decode(message) if known else {},
                "checksum_valid": self.checksum_valid if known else None}


def encode(message: Message, telemetry: dict, counter: int, timestamp: float) -> Frame:
    payload = 0
    for signal in message.signals:
        payload |= signal.encode(telemetry[signal.source]) << signal.start
    payload |= (counter & 0xFF) << 48
    data = payload.to_bytes(8, "little")
    data = data[:7] + bytes([checksum(message.can_id, data)])
    return Frame(timestamp, message.can_id, data, message.extended)
