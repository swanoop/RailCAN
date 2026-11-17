"""Minimal MQTT 3.1.1 client and MessagePack codec using only the standard library.

It is enough to subscribe to a telemetry broker during a test, record what arrives with a
receipt time, and publish test messages. It is not a full client: QoS 2 and session
persistence are not implemented.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------- MessagePack


def msgpack_unpack(data: bytes):
    value, offset = _unpack(memoryview(data), 0)
    if offset != len(data):
        raise ValueError("Trailing bytes after MessagePack value")
    return value


def _unpack(view: memoryview, offset: int):
    def take(count):
        nonlocal offset
        if offset + count > len(view):
            raise ValueError("Truncated MessagePack data")
        chunk = bytes(view[offset:offset + count])
        offset += count
        return chunk

    def sequence(count, as_map):
        nonlocal offset
        if as_map:
            result = {}
            for _ in range(count):
                key, offset = _unpack(view, offset)
                value, offset = _unpack(view, offset)
                result[key if isinstance(key, (str, int, float, bool)) or key is None else str(key)] = value
            return result
        items = []
        for _ in range(count):
            value, offset = _unpack(view, offset)
            items.append(value)
        return items

    code = take(1)[0]
    if code <= 0x7F:
        return code, offset
    if code >= 0xE0:
        return code - 0x100, offset
    if 0x80 <= code <= 0x8F:
        return sequence(code & 0x0F, True), offset
    if 0x90 <= code <= 0x9F:
        return sequence(code & 0x0F, False), offset
    if 0xA0 <= code <= 0xBF:
        return take(code & 0x1F).decode("utf-8"), offset
    simple = {0xC0: None, 0xC2: False, 0xC3: True}
    if code in simple:
        return simple[code], offset
    sizes = {0xC4: 1, 0xC5: 2, 0xC6: 4, 0xD9: 1, 0xDA: 2, 0xDB: 4}
    if code in sizes:
        length = int.from_bytes(take(sizes[code]), "big")
        raw = take(length)
        return (raw.hex() if code <= 0xC6 else raw.decode("utf-8")), offset
    numbers = {0xCA: ">f", 0xCB: ">d", 0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
               0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
    if code in numbers:
        fmt = numbers[code]
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0], offset
    if code in (0xDC, 0xDD, 0xDE, 0xDF):
        count = int.from_bytes(take(2 if code in (0xDC, 0xDE) else 4), "big")
        return sequence(count, code >= 0xDE), offset
    if 0xC7 <= code <= 0xC9 or 0xD4 <= code <= 0xD8:
        if code <= 0xC9:
            length = int.from_bytes(take({0xC7: 1, 0xC8: 2, 0xC9: 4}[code]), "big")
        else:
            length = 1 << (code - 0xD4)
        kind = struct.unpack(">b", take(1))[0]
        raw = take(length)
        if kind == -1:  # timestamp extension -> Unix seconds
            if length == 4:
                return float(int.from_bytes(raw, "big")), offset
            if length == 8:
                value = int.from_bytes(raw, "big")
                return (value & 0x3FFFFFFFF) + (value >> 34) / 1e9, offset
            if length == 12:
                nanos, seconds = struct.unpack(">Iq", raw)
                return seconds + nanos / 1e9, offset
        return {"ext_type": kind, "data_hex": raw.hex()}, offset
    raise ValueError(f"Unsupported MessagePack type 0x{code:02X}")


def msgpack_pack(value) -> bytes:
    if value is None:
        return b"\xC0"
    if value is True:
        return b"\xC3"
    if value is False:
        return b"\xC2"
    if isinstance(value, int):
        if 0 <= value <= 0x7F:
            return bytes([value])
        if -32 <= value < 0:
            return struct.pack(">b", value)
        for low, high, code, fmt in ((0, 0xFF, 0xCC, ">B"), (0, 0xFFFF, 0xCD, ">H"),
                                     (0, 0xFFFFFFFF, 0xCE, ">I"), (0, 2**64 - 1, 0xCF, ">Q"),
                                     (-128, 127, 0xD0, ">b"), (-2**15, 2**15 - 1, 0xD1, ">h"),
                                     (-2**31, 2**31 - 1, 0xD2, ">i"), (-2**63, 2**63 - 1, 0xD3, ">q")):
            if low <= value <= high:
                return bytes([code]) + struct.pack(fmt, value)
        raise ValueError("Integer too large for MessagePack")
    if isinstance(value, float):
        return b"\xCB" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) < 32:
            return bytes([0xA0 | len(raw)]) + raw
        for limit, code, size in ((0xFF, 0xD9, 1), (0xFFFF, 0xDA, 2), (0xFFFFFFFF, 0xDB, 4)):
            if len(raw) <= limit:
                return bytes([code]) + len(raw).to_bytes(size, "big") + raw
    if isinstance(value, (bytes, bytearray)):
        for limit, code, size in ((0xFF, 0xC4, 1), (0xFFFF, 0xC5, 2), (0xFFFFFFFF, 0xC6, 4)):
            if len(value) <= limit:
                return bytes([code]) + len(value).to_bytes(size, "big") + bytes(value)
    if isinstance(value, (list, tuple)):
        head = bytes([0x90 | len(value)]) if len(value) < 16 else (
            b"\xDC" + len(value).to_bytes(2, "big") if len(value) <= 0xFFFF else b"\xDD" + len(value).to_bytes(4, "big"))
        return head + b"".join(msgpack_pack(item) for item in value)
    if isinstance(value, dict):
        head = bytes([0x80 | len(value)]) if len(value) < 16 else (
            b"\xDE" + len(value).to_bytes(2, "big") if len(value) <= 0xFFFF else b"\xDF" + len(value).to_bytes(4, "big"))
        return head + b"".join(msgpack_pack(k) + msgpack_pack(v) for k, v in value.items())
    raise ValueError(f"Cannot encode {type(value).__name__} as MessagePack")


def decode_payload(payload: bytes, encoding: str = "auto"):
    """Decode a telemetry payload as JSON, MessagePack or (failing both) hex."""
    if encoding in ("auto", "json"):
        text = payload.lstrip()[:1]
        if encoding == "json" or text in (b"{", b"["):
            try:
                return json.loads(payload.decode("utf-8")), "json"
            except (UnicodeDecodeError, ValueError):
                if encoding == "json":
                    raise ValueError("Payload is not valid JSON")
    if encoding in ("auto", "msgpack"):
        try:
            return msgpack_unpack(payload), "msgpack"
        except (ValueError, UnicodeDecodeError, struct.error):
            if encoding == "msgpack":
                raise ValueError("Payload is not valid MessagePack")
    return {"raw_hex": payload.hex()}, "raw"


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------- MQTT 3.1.1


def _remaining_length(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value % 128, value // 128
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _string(text: str | bytes) -> bytes:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > 0xFFFF:
        raise ValueError("MQTT string too long")
    return len(raw).to_bytes(2, "big") + raw


@dataclass
class Received:
    received_at: float
    topic: str
    payload: bytes
    qos: int
    retain: bool


class MQTTClient:
    def __init__(self, host: str, port: int | None = None, *, tls: bool = False, cafile: str | None = None,
                 certfile: str | None = None, keyfile: str | None = None, username: str | None = None,
                 password: str | None = None, client_id: str | None = None, keepalive: int = 30,
                 timeout: float = 10.0):
        if not host:
            raise ValueError("MQTT host is required")
        self.host, self.port = host, port or (8883 if tls else 1883)
        self.keepalive = max(5, min(int(keepalive), 600))
        self.client_id = client_id or f"railcan-{secrets.token_hex(4)}"
        self.packet_id = 0
        self.buffer = b""
        try:
            raw = socket.create_connection((host, self.port), timeout=timeout)
        except OSError as exc:
            raise ValueError(f"Cannot reach MQTT broker {host}:{self.port}: {exc}") from exc
        if tls:
            context = ssl.create_default_context(cafile=cafile)
            if certfile:
                context.load_cert_chain(certfile, keyfile)
            try:
                raw = context.wrap_socket(raw, server_hostname=host)
            except (ssl.SSLError, OSError) as exc:
                raw.close()
                raise ValueError(f"TLS handshake with {host}:{self.port} failed: {exc}") from exc
        self.socket = raw
        self.last_sent = time.monotonic()
        flags = 0x02 | (0x80 if username else 0) | (0x40 if password else 0)
        body = _string("MQTT") + bytes([4, flags]) + self.keepalive.to_bytes(2, "big") + _string(self.client_id)
        if username:
            body += _string(username)
        if password:
            body += _string(password)
        self._write(0x10, body)
        kind, _, data = self._read_packet(timeout)
        if kind != 2 or len(data) != 2:
            self.close()
            raise ValueError("Broker did not answer CONNECT with CONNACK")
        if data[1] != 0:
            reasons = {1: "unacceptable protocol version", 2: "client identifier rejected", 3: "server unavailable",
                       4: "bad user name or password", 5: "not authorised"}
            self.close()
            raise ValueError(f"MQTT connection refused: {reasons.get(data[1], data[1])}")

    def _write(self, header: int, body: bytes) -> None:
        self.socket.sendall(bytes([header]) + _remaining_length(len(body)) + body)
        self.last_sent = time.monotonic()

    def _fill(self, count: int, timeout: float) -> bool:
        self.socket.settimeout(timeout)
        while len(self.buffer) < count:
            try:
                chunk = self.socket.recv(65536)
            except (TimeoutError, ssl.SSLWantReadError):
                return False
            if not chunk:
                raise ConnectionError("MQTT broker closed the connection")
            self.buffer += chunk
        return True

    def _read_packet(self, timeout: float):
        if not self._fill(2, timeout):
            return None, None, None
        length, multiplier, index = 0, 1, 1
        while True:
            if not self._fill(index + 1, timeout):
                return None, None, None
            byte = self.buffer[index]
            length += (byte & 0x7F) * multiplier
            index += 1
            if not byte & 0x80:
                break
            multiplier *= 128
            if index > 4:
                raise ValueError("Malformed MQTT remaining length")
        if not self._fill(index + length, max(timeout, 5)):
            return None, None, None
        header = self.buffer[0]
        data = self.buffer[index:index + length]
        self.buffer = self.buffer[index + length:]
        return header >> 4, header & 0x0F, data

    def _next_id(self) -> int:
        self.packet_id = self.packet_id % 0xFFFF + 1
        return self.packet_id

    def subscribe(self, topic: str, qos: int = 1, timeout: float = 10.0) -> int:
        if qos not in (0, 1):
            raise ValueError("Subscription QoS must be 0 or 1")
        packet_id = self._next_id()
        self._write(0x82, packet_id.to_bytes(2, "big") + _string(topic) + bytes([qos]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            kind, _, data = self._read_packet(max(.1, deadline - time.monotonic()))
            if kind == 9 and data[:2] == packet_id.to_bytes(2, "big"):
                if data[2] == 0x80:
                    raise ValueError(f"Broker refused subscription to {topic}")
                return data[2]
        raise ValueError("Broker did not acknowledge the subscription")

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        if qos not in (0, 1):
            raise ValueError("Publish QoS must be 0 or 1")
        body = _string(topic)
        if qos:
            body += self._next_id().to_bytes(2, "big")
        self._write(0x30 | (qos << 1) | int(retain), body + payload)

    def poll(self, timeout: float = 1.0) -> Received | None:
        """Wait for one PUBLISH; answers keepalive and QoS 1 acknowledgements on the way."""
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() - self.last_sent > self.keepalive / 2:
                self._write(0xC0, b"")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            kind, flags, data = self._read_packet(min(remaining, self.keepalive / 2))
            if kind == 3:
                received_at = time.time()
                qos = (flags >> 1) & 0x3
                size = int.from_bytes(data[:2], "big")
                topic = data[2:2 + size].decode("utf-8", "replace")
                offset = 2 + size
                if qos:
                    packet_id = data[offset:offset + 2]
                    offset += 2
                    if qos == 1:
                        self._write(0x40, packet_id)
                    else:  # QoS 2: acknowledge receipt so the broker does not stall
                        self._write(0x50, packet_id)
                return Received(received_at, topic, data[offset:], qos, bool(flags & 1))
            if kind == 6:  # PUBREL for QoS 2
                self._write(0x70, data[:2])

    def close(self) -> None:
        try:
            self._write(0xE0, b"")
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


def password_from_environment() -> str | None:
    """Read the broker password from RAILCAN_MQTT_PASSWORD so it never appears in shell history."""
    return os.environ.get("RAILCAN_MQTT_PASSWORD") or None


def payload_timestamp(payload) -> float | None:
    """Unix seconds from a payload's timestamp/time/ts field (ISO 8601, seconds or milliseconds)."""
    if not isinstance(payload, dict):
        return None
    for key in ("timestamp", "time", "ts", "t"):
        if key in payload:
            return parse_time(payload[key])
    return None


def parse_time(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        if not math.isfinite(value):
            return None
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                return parse_time(float(text))
            except ValueError:
                return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None
