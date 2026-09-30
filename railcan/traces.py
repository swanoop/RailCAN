"""Streaming candump, CSV, JSONL and SocketCAN PCAP trace I/O."""

from __future__ import annotations

import csv
import json
import re
import struct
from pathlib import Path
from typing import Iterable, Iterator

from .frame import Frame
from .profile import Profile, default_profile, finite

FORMATS = {"log": "candump", "candump": "candump", "csv": "csv", "jsonl": "jsonl", "pcap": "pcap"}
CANDUMP = re.compile(r"^\((\d+(?:\.\d+)?)\)\s+(\S+)\s+([0-9a-fA-F]{1,8})#([0-9a-fA-F]{0,16})$")


def trace_format(path: str | Path, selected: str | None = None) -> str:
    key = selected or Path(path).suffix.lower().lstrip(".")
    if key not in FORMATS:
        raise ValueError("Choose .log, .csv, .jsonl or .pcap, or specify --format")
    return FORMATS[key]


def candump_line(frame: Frame, channel: str = "vcan0") -> str:
    return f"({frame.timestamp:.6f}) {channel} {frame.hex_id}#{frame.data.hex().upper()}\n"


def socketcan_packet(frame: Frame, network_order: bool = False) -> bytes:
    can_id = frame.can_id | (0x80000000 if frame.extended else 0)
    return struct.pack(("!" if network_order else "=") + "IB3x8s", can_id, len(frame.data), frame.data.ljust(8, b"\0"))


def write_trace(frames: Iterable[Frame], path: str | Path, profile: Profile | None = None,
                format: str | None = None, channel: str = "vcan0", epoch: float = 0.0) -> int:
    format = trace_format(path, format)
    profile = profile or default_profile()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", channel):
        raise ValueError("Invalid trace channel")
    if finite(epoch, "epoch") < 0:
        raise ValueError("epoch must be nonnegative")
    count = 0
    if format == "pcap":
        with Path(path).open("wb") as handle:
            # PCAP 2.4, microseconds, LINKTYPE_CAN_SOCKETCAN (227).
            handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 227))
            for frame in frames:
                micros = round((epoch + frame.timestamp) * 1000000)
                seconds, remainder = divmod(micros, 1000000)
                if seconds > 0xFFFFFFFF:
                    raise ValueError("PCAP timestamp exceeds 32-bit seconds")
                packet = socketcan_packet(frame, network_order=True)
                handle.write(struct.pack("<IIII", seconds, remainder, len(packet), len(packet)))
                handle.write(packet)
                count += 1
        return count
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = None
        if format == "csv":
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "channel", "can_id_hex", "extended", "dlc", "data_hex",
                             "message", "signals_json", "checksum_valid"])
        for frame in frames:
            if format == "candump":
                handle.write(candump_line(frame, channel))
            else:
                record = frame.record(profile)
                if format == "jsonl":
                    handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
                else:
                    writer.writerow([f"{frame.timestamp:.6f}", channel, record["can_id_hex"],
                                     int(frame.extended), len(frame.data), record["data_hex"], record["message"],
                                     json.dumps(record["signals"], separators=(",", ":")), record["checksum_valid"]])
            count += 1
    return count


def frame_from_record(record: dict) -> Frame:
    if not isinstance(record, dict):
        raise ValueError("Trace record must be an object")
    try:
        frame = Frame(finite(record["timestamp"], "timestamp"), record["can_id"],
                      bytes.fromhex(record["data_hex"]), record.get("extended", False))
        if "dlc" in record and record["dlc"] != len(frame.data):
            raise ValueError("DLC does not match payload")
        return frame
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Invalid trace record: {exc}") from exc


def _pcap_frames(path: Path) -> Iterator[Frame]:
    with path.open("rb") as handle:
        header = handle.read(24)
        if len(header) != 24:
            raise ValueError("Truncated PCAP header")
        magic = header[:4]
        variants = {b'\xd4\xc3\xb2\xa1': ("<", 1000000), b'\xa1\xb2\xc3\xd4': (">", 1000000),
                    b'\x4d\x3c\xb2\xa1': ("<", 1000000000), b'\xa1\xb2\x3c\x4d': (">", 1000000000)}
        if magic not in variants:
            raise ValueError("Expected PCAP, not PCAPNG or another format")
        order, resolution = variants[magic]
        _, major, minor, _, _, _, linktype = struct.unpack(order + "IHHIIII", header)
        if (major, minor) != (2, 4) or linktype != 227:
            raise ValueError("PCAP must use version 2.4 and SocketCAN link type 227")
        while block := handle.read(16):
            if len(block) != 16:
                raise ValueError("Truncated PCAP record header")
            seconds, fraction, size, original = struct.unpack(order + "IIII", block)
            if fraction >= resolution or size != 16 or original != 16:
                raise ValueError("Expected a complete 16-byte Classical SocketCAN packet")
            packet = handle.read(size)
            if len(packet) != size:
                raise ValueError("Truncated PCAP CAN packet")
            can_id, length = struct.unpack("!IB", packet[:5])
            if can_id & 0x60000000 or length > 8:
                raise ValueError("RTR, error and CAN FD frames are not supported")
            yield Frame(seconds + fraction / resolution, can_id & 0x1FFFFFFF,
                        packet[8:8 + length], bool(can_id & 0x80000000))


def read_trace(path: str | Path, format: str | None = None) -> Iterator[Frame]:
    path = Path(path)
    selected = trace_format(path, format)
    if selected == "pcap":
        source = _pcap_frames(path)
    else:
        def text_frames():
            with path.open(encoding="utf-8", newline="") as handle:
                if selected == "csv":
                    for line_number, row in enumerate(csv.DictReader(handle), 2):
                        try:
                            if row["extended"] not in {"0", "1"}:
                                raise ValueError("extended must be 0 or 1")
                            frame = Frame(float(row["timestamp"]), int(row["can_id_hex"], 16),
                                          bytes.fromhex(row["data_hex"]), row["extended"] == "1")
                            if int(row["dlc"]) != len(frame.data):
                                raise ValueError("DLC does not match payload")
                            yield frame
                        except (ValueError, KeyError, TypeError) as exc:
                            raise ValueError(f"Invalid CSV line {line_number}: {exc}") from exc
                else:
                    for line_number, line in enumerate(handle, 1):
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        try:
                            if selected == "jsonl":
                                yield frame_from_record(json.loads(line))
                            else:
                                match = CANDUMP.fullmatch(line)
                                if not match:
                                    raise ValueError("Expected candump log syntax: (seconds) channel ID#DATA")
                                timestamp, _, identifier, data = match.groups()
                                yield Frame(float(timestamp), int(identifier, 16), bytes.fromhex(data), len(identifier) == 8)
                        except (ValueError, KeyError, TypeError) as exc:
                            raise ValueError(f"Invalid trace line {line_number}: {exc}") from exc
        source = text_frames()
    previous = -1.0
    for frame in source:
        if frame.timestamp < previous:
            raise ValueError("Trace timestamps must be nondecreasing")
        previous = frame.timestamp
        yield frame


def inspect_trace(frames: Iterable[Frame], profile: Profile | None = None) -> dict:
    profile = profile or default_profile()
    definitions = profile.by_id
    results, previous, first, last, total = {}, {}, None, None, 0
    for frame in frames:
        total += 1
        first = frame.timestamp if first is None else first
        last = frame.timestamp
        key = (frame.can_id, frame.extended)
        message = definitions.get(frame.can_id)
        known = message is not None and message.extended == frame.extended and len(frame.data) == 8
        row = results.setdefault(key, {"can_id": frame.hex_id, "extended": frame.extended,
                                      "message": message.name if known else "Unknown", "frames": 0,
                                      "checksum_errors": 0, "counter_anomalies": 0,
                                      "missing_frames_estimate": 0, "max_gap_s": 0.0})
        row["frames"] += 1
        if known:
            row["checksum_errors"] += int(not frame.checksum_valid)
        if key in previous:
            prior = previous[key]
            gap = frame.timestamp - prior.timestamp
            row["max_gap_s"] = round(max(row["max_gap_s"], gap), 6)
            if known and len(prior.data) == 8:
                slots = max(1, round(gap * 1000 / message.period_ms))
                row["missing_frames_estimate"] += max(0, slots - 1)
                expected_counter = (prior.data[6] + slots) & 0xFF
                row["counter_anomalies"] += int(frame.data[6] != expected_counter)
        previous[key] = frame
    return {"frames": total, "start_s": first, "end_s": last,
            "span_s": round(last - first, 6) if total else 0,
            "messages": [results[key] for key in sorted(results)],
            "notes": "Gap estimates require the matching profile and include only gaps between observed frames."}
