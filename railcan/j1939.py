"""SAE J1939 helpers: 29-bit identifiers, DM1 diagnostics and TP.BAM transport.

Only the pieces a traffic generator and monitor need are implemented: broadcast
multi-packet transfer (BAM), not connection-mode RTS/CTS or address claiming.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PGN_DM1 = 0xFECA          # 65226 Active diagnostic trouble codes
PGN_TP_CM = 0xEC00        # 60416 Transport protocol connection management
PGN_TP_DT = 0xEB00        # 60160 Transport protocol data transfer
TP_BAM = 0x20
BAM_PACKET_GAP_MS = 50    # J1939-21 allows 50–200 ms between BAM data packets
GLOBAL = 0xFF

KNOWN_PGNS = {PGN_DM1: "DM1", PGN_TP_CM: "TP.CM", PGN_TP_DT: "TP.DT", 0xEA00: "Request",
              0xEE00: "AddressClaimed"}


def is_pdu1(pgn: int) -> bool:
    return ((pgn >> 8) & 0xFF) < 240


def compose_id(pgn: int, priority: int = 6, source_address: int = 0, destination: int = GLOBAL) -> int:
    """Build a 29-bit J1939 identifier. PDU1 PGNs (PF < 240) carry a destination address."""
    if not 0 <= pgn <= 0x3FFFF:
        raise ValueError("PGN must be 0–262143")
    if not 0 <= priority <= 7:
        raise ValueError("J1939 priority must be 0–7")
    if not 0 <= source_address <= 0xFF or not 0 <= destination <= 0xFF:
        raise ValueError("J1939 addresses must be 0–255")
    if is_pdu1(pgn):
        if pgn & 0xFF:
            raise ValueError(f"PDU1 PGN 0x{pgn:05X} must have a zero low byte; the destination goes there")
        pgn |= destination
    return (priority << 26) | (pgn << 8) | source_address


def split_id(can_id: int) -> tuple[int, int, int, int]:
    """Return (priority, pgn, source_address, destination) for a 29-bit identifier."""
    priority = (can_id >> 26) & 0x7
    pgn = (can_id >> 8) & 0x3FFFF
    source = can_id & 0xFF
    destination = GLOBAL
    if is_pdu1(pgn):
        destination = pgn & 0xFF
        pgn &= 0x3FF00
    return priority, pgn, source, destination


def pgn_of(can_id: int) -> int:
    return split_id(can_id)[1]


@dataclass(frozen=True)
class DTC:
    spn: int
    fmi: int
    occurrence: int = 1

    def __post_init__(self):
        if not 0 <= self.spn <= 0x7FFFF:
            raise ValueError("SPN must be 0–524287")
        if not 0 <= self.fmi <= 31:
            raise ValueError("FMI must be 0–31")
        if not 0 <= self.occurrence <= 126:
            raise ValueError("Occurrence count must be 0–126")

    def encode(self) -> bytes:
        # Conversion method 0 (J1939-73 version 4 SPN layout).
        return bytes([self.spn & 0xFF, (self.spn >> 8) & 0xFF,
                      ((self.spn >> 16) & 0x7) << 5 | self.fmi, self.occurrence & 0x7F])

    @classmethod
    def decode(cls, data: bytes) -> DTC:
        spn = data[0] | data[1] << 8 | (data[2] >> 5) << 16
        return cls(spn, data[2] & 0x1F, data[3] & 0x7F)


def dm1_payload(dtcs: list[DTC]) -> bytes:
    """DM1 data: lamp status, lamp flash, then one 4-byte DTC per entry (or an empty DTC)."""
    lamps = 0b00000100 if dtcs else 0  # amber warning lamp on while any DTC is active
    body = b"".join(dtc.encode() for dtc in dtcs) or b"\0\0\0\0"
    data = bytes([lamps, 0xFF]) + body
    return data.ljust(8, b"\xFF") if len(data) < 8 else data


def decode_dm1(data: bytes) -> dict:
    if len(data) < 6:
        raise ValueError("DM1 payload too short")
    dtcs = []
    for offset in range(2, len(data) - 3, 4):
        chunk = data[offset:offset + 4]
        if chunk in (b"\0\0\0\0", b"\xFF\xFF\xFF\xFF"):
            continue
        dtcs.append(DTC.decode(chunk))
    return {"amber_warning_lamp": (data[0] >> 2) & 0x3 == 1,
            "protect_lamp": data[0] & 0x3 == 1,
            "red_stop_lamp": (data[0] >> 4) & 0x3 == 1,
            "malfunction_lamp": (data[0] >> 6) & 0x3 == 1,
            "dtcs": [{"spn": d.spn, "fmi": d.fmi, "oc": d.occurrence} for d in dtcs]}


def bam_frames(pgn: int, payload: bytes, priority: int, source_address: int) -> list[tuple[int, bytes]]:
    """Split a 9–1785 byte payload into one TP.CM BAM and its TP.DT packets: [(can_id, data)]."""
    if not 9 <= len(payload) <= 1785:
        raise ValueError("BAM payloads must be 9–1785 bytes")
    packets = -(-len(payload) // 7)
    announce = bytes([TP_BAM, len(payload) & 0xFF, len(payload) >> 8, packets, 0xFF,
                      pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])
    result = [(compose_id(PGN_TP_CM, priority, source_address, GLOBAL), announce)]
    data_id = compose_id(PGN_TP_DT, priority, source_address, GLOBAL)
    for number in range(packets):
        chunk = payload[number * 7:(number + 1) * 7].ljust(7, b"\xFF")
        result.append((data_id, bytes([number + 1]) + chunk))
    return result


@dataclass
class _Transfer:
    pgn: int
    size: int
    packets: int
    started: float
    data: dict = field(default_factory=dict)


class BAMReassembler:
    """Collect broadcast multi-packet transfers; feed every frame, get (pgn, payload) when complete."""

    def __init__(self, timeout_s: float = 0.75):
        self.timeout_s = timeout_s
        self.open: dict[int, _Transfer] = {}
        self.aborted = 0

    def feed(self, timestamp: float, can_id: int, data: bytes, extended: bool = True):
        if not extended:
            return None
        _, pgn, source, destination = split_id(can_id)
        if pgn == PGN_TP_CM and destination == GLOBAL and len(data) == 8 and data[0] == TP_BAM:
            if source in self.open:
                self.aborted += 1
            size = data[1] | data[2] << 8
            self.open[source] = _Transfer(data[5] | data[6] << 8 | data[7] << 16, size, data[3], timestamp)
            return None
        if pgn == PGN_TP_DT and destination == GLOBAL and source in self.open and len(data) == 8:
            transfer = self.open[source]
            if timestamp - transfer.started > self.timeout_s * transfer.packets + self.timeout_s:
                del self.open[source]
                self.aborted += 1
                return None
            transfer.data[data[0]] = data[1:]
            if len(transfer.data) == transfer.packets:
                del self.open[source]
                if set(transfer.data) != set(range(1, transfer.packets + 1)):
                    self.aborted += 1
                    return None
                payload = b"".join(transfer.data[n] for n in range(1, transfer.packets + 1))[:transfer.size]
                return transfer.pgn, source, payload
        return None


def describe(can_id: int) -> str:
    _, pgn, source, _ = split_id(can_id)
    return f"{KNOWN_PGNS.get(pgn, 'PGN')} 0x{pgn:04X} SA {source}"
