"""CAN interfaces: Linux SocketCAN, optional python-can adapters and CAN-over-IP gateways.

Every interface offers send(frame), recv(timeout) and close(). Physical buses require an
explicit opt-in; virtual buses (vcan*, python-can "virtual") and loopback gateways do not.

Interface specifications:
  vcan0 | can0                     Linux SocketCAN (bare name, as in earlier releases)
  socketcan:can0                   Linux SocketCAN
  pythoncan:<interface>:<channel>  any python-can backend, e.g. pythoncan:pcan:PCAN_USBBUS1,
                                   pythoncan:kvaser:0, pythoncan:slcan:COM5, pythoncan:gs_usb:0
  udp:<host>:<port>[:<local_port>] CAN-to-Ethernet gateway, 13-byte frames over UDP
  tcp:<host>:<port>                CAN-to-Ethernet gateway, 13-byte frames over TCP
"""

from __future__ import annotations

import errno
import ipaddress
import json
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace

from .frame import Frame
from .profile import finite
from .traces import socketcan_packet

CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG = 0x80000000, 0x40000000, 0x20000000
CAN_ERR_MASK = 0x1FFFFFFF
SOL_CAN_RAW, CAN_RAW_ERR_FILTER = 101, 2
ERROR_CLASSES = {0x1: "tx_timeout", 0x2: "arbitration_lost", 0x4: "controller", 0x8: "protocol",
                 0x10: "transceiver", 0x20: "no_ack", 0x40: "bus_off", 0x80: "bus_error", 0x100: "restarted"}
CONTROLLER_STATES = {0x01: "rx_overflow", 0x02: "tx_overflow", 0x04: "rx_warning", 0x08: "tx_warning",
                     0x10: "rx_passive", 0x20: "tx_passive", 0x40: "active"}
SEND_RETRY_S = 0.1


@dataclass(frozen=True)
class BusEvent:
    """A non-data observation from the bus: an error frame or an adapter status change."""
    timestamp: float
    kind: str
    detail: dict = field(default_factory=dict)

    def record(self) -> dict:
        return {"timestamp": round(self.timestamp, 6), "event": self.kind, **self.detail}


@dataclass
class Stats:
    sent: int = 0
    dropped: int = 0
    retries: int = 0
    received: int = 0
    events: int = 0

    def summary(self) -> str:
        parts = [f"{self.sent:,} sent"] if self.sent or not self.received else []
        if self.received:
            parts.append(f"{self.received:,} received")
        if self.dropped:
            parts.append(f"{self.dropped:,} DROPPED (transmit queue full)")
        if self.retries:
            parts.append(f"{self.retries:,} send retries")
        if self.events:
            parts.append(f"{self.events:,} bus events")
        return " · ".join(parts)


class CANInterface:
    """Base class; subclasses implement _send and _recv."""
    name = "interface"

    def __init__(self):
        self.stats = Stats()

    def send(self, frame: Frame) -> bool:
        """Send a frame. A full transmit queue is retried briefly, then counted as a drop."""
        deadline = time.monotonic() + SEND_RETRY_S
        while True:
            try:
                self._send(frame)
                self.stats.sent += 1
                return True
            except _QueueFull:
                if time.monotonic() >= deadline:
                    self.stats.dropped += 1
                    return False
                self.stats.retries += 1
                time.sleep(.001)

    def recv(self, timeout: float = 1.0) -> Frame | BusEvent | None:
        item = self._recv(timeout)
        if isinstance(item, Frame):
            self.stats.received += 1
        elif isinstance(item, BusEvent):
            self.stats.events += 1
        return item

    def _send(self, frame: Frame) -> None:
        raise NotImplementedError

    def _recv(self, timeout: float):
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _QueueFull(Exception):
    pass


def _error_event(timestamp: float, can_id: int, data: bytes) -> BusEvent:
    classes = [name for bit, name in ERROR_CLASSES.items() if can_id & bit]
    detail = {"classes": classes, "data_hex": data.hex().upper()}
    if can_id & 0x4 and len(data) > 1:
        detail["controller"] = [name for bit, name in CONTROLLER_STATES.items() if data[1] & bit]
    if len(data) == 8:
        detail["tx_errors"], detail["rx_errors"] = data[6], data[7]
    return BusEvent(timestamp, "error_frame", detail)


class SocketCANInterface(CANInterface):
    def __init__(self, channel: str = "vcan0", receive_errors: bool = True):
        super().__init__()
        if sys.platform != "linux" or not hasattr(socket, "AF_CAN"):
            raise ValueError("SocketCAN requires Linux; use pythoncan:<interface>:<channel> on Windows/macOS")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", channel):
            raise ValueError("Invalid SocketCAN interface name")
        self.channel = self.name = channel
        self.socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        if receive_errors:
            try:
                self.socket.setsockopt(SOL_CAN_RAW, CAN_RAW_ERR_FILTER, struct.pack("=I", CAN_ERR_MASK))
            except OSError:
                pass
        try:
            self.socket.bind((channel,))
        except OSError as exc:
            self.socket.close()
            raise ValueError(f"Cannot open {channel}: {exc}. Configure the interface first; see docs/USAGE.md") from exc

    def _send(self, frame: Frame) -> None:
        packet = socketcan_packet(frame)
        try:
            self.socket.settimeout(1.0)
            if self.socket.send(packet) != len(packet):
                raise OSError("Incomplete SocketCAN send")
        except OSError as exc:
            if exc.errno in (errno.ENOBUFS, errno.EAGAIN) or isinstance(exc, socket.timeout):
                raise _QueueFull from exc
            raise

    def _recv(self, timeout: float):
        self.socket.settimeout(timeout)
        try:
            packet = self.socket.recv(16)
        except TimeoutError:
            return None
        now = time.time()
        can_id, length = struct.unpack("=IB", packet[:5])
        data = packet[8:8 + min(length, 8)]
        if can_id & CAN_ERR_FLAG:
            return _error_event(now, can_id & CAN_ERR_MASK, data)
        if can_id & CAN_RTR_FLAG:
            return BusEvent(now, "remote_frame", {"can_id": can_id & CAN_ERR_MASK})
        extended = bool(can_id & CAN_EFF_FLAG)
        return Frame(now, can_id & (CAN_ERR_MASK if extended else 0x7FF), data, extended)

    def close(self) -> None:
        self.socket.close()


class PythonCANInterface(CANInterface):
    def __init__(self, interface: str, channel: str, bitrate: int | None = None):
        super().__init__()
        try:
            import can  # type: ignore
        except ImportError as exc:
            raise ValueError("pythoncan: interfaces need python-can: python -m pip install \"railcan[hardware]\"") from exc
        self.can = can
        self.name = f"{interface}:{channel}"
        options = {"interface": interface, "channel": channel}
        if bitrate and interface != "virtual":
            options["bitrate"] = bitrate
        try:
            self.bus = can.Bus(**options)
        except Exception as exc:  # python-can raises backend-specific types
            raise ValueError(f"Cannot open {self.name}: {exc}") from exc

    def _send(self, frame: Frame) -> None:
        message = self.can.Message(arbitration_id=frame.can_id, data=frame.data,
                                   is_extended_id=frame.extended)
        try:
            self.bus.send(message, timeout=.05)
        except self.can.CanOperationError as exc:
            raise _QueueFull from exc

    def _recv(self, timeout: float):
        message = self.bus.recv(timeout)
        if message is None:
            return None
        now = time.time()
        if message.is_error_frame:
            return _error_event(now, message.arbitration_id & CAN_ERR_MASK, bytes(message.data))
        if message.is_remote_frame:
            return BusEvent(now, "remote_frame", {"can_id": message.arbitration_id})
        if getattr(message, "is_fd", False) or len(message.data) > 8:
            return BusEvent(now, "unsupported_frame", {"can_id": message.arbitration_id, "reason": "CAN FD"})
        return Frame(now, message.arbitration_id, bytes(message.data), bool(message.is_extended_id))

    def close(self) -> None:
        self.bus.shutdown()


def gateway_packet(frame: Frame) -> bytes:
    """Encode RailCAN's documented 13-byte CAN-over-IP frame.

    Byte 0: bit 7 extended, bit 6 remote, bits 0–3 DLC. Bytes 1–4: big-endian ID. Bytes 5–12: data.
    """
    info = (0x80 if frame.extended else 0) | len(frame.data)
    return bytes([info]) + frame.can_id.to_bytes(4, "big") + frame.data.ljust(8, b"\0")


def parse_gateway_packet(packet: bytes, timestamp: float) -> Frame | BusEvent:
    if len(packet) != 13:
        raise ValueError("Gateway frames must be 13 bytes")
    info, can_id = packet[0], int.from_bytes(packet[1:5], "big")
    length = info & 0x0F
    if length > 8:
        return BusEvent(timestamp, "invalid_gateway_frame", {"data_hex": packet.hex().upper()})
    if info & 0x40:
        return BusEvent(timestamp, "remote_frame", {"can_id": can_id})
    extended = bool(info & 0x80)
    if can_id > (0x1FFFFFFF if extended else 0x7FF):
        return BusEvent(timestamp, "invalid_gateway_frame", {"data_hex": packet.hex().upper()})
    return Frame(timestamp, can_id, packet[5:5 + length], extended)


class GatewayInterface(CANInterface):
    def __init__(self, protocol: str, host: str, port: int, local_port: int | None = None):
        super().__init__()
        self.protocol, self.address = protocol, (host, port)
        self.name = f"{protocol}:{host}:{port}"
        self.buffer = b""
        try:
            if protocol == "udp":
                self.socket = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.bind(("", local_port or 0))
            else:
                self.socket = socket.create_connection(self.address, timeout=5)
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            raise ValueError(f"Cannot open {self.name}: {exc}") from exc

    def _send(self, frame: Frame) -> None:
        packet = gateway_packet(frame)
        try:
            if self.protocol == "udp":
                self.socket.sendto(packet, self.address)
            else:
                self.socket.settimeout(1.0)
                self.socket.sendall(packet)
        except (TimeoutError, BlockingIOError) as exc:
            raise _QueueFull from exc
        except OSError as exc:
            if exc.errno == errno.ENOBUFS:
                raise _QueueFull from exc
            raise

    def _recv(self, timeout: float):
        if len(self.buffer) >= 13:
            packet, self.buffer = self.buffer[:13], self.buffer[13:]
            return parse_gateway_packet(packet, time.time())
        self.socket.settimeout(timeout)
        try:
            if self.protocol == "udp":
                while True:
                    packet, _ = self.socket.recvfrom(4096)
                    if len(packet) % 13 == 0 and packet:
                        break
                # Gateways may pack several frames into one datagram.
                self.buffer += packet
            else:
                while len(self.buffer) < 13:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        raise ValueError(f"{self.name} closed the connection")
                    self.buffer += chunk
        except TimeoutError:
            if len(self.buffer) < 13:
                return None
        packet, self.buffer = self.buffer[:13], self.buffer[13:]
        return parse_gateway_packet(packet, time.time())

    def close(self) -> None:
        self.socket.close()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def open_interface(spec: str, allow_hardware: bool = False, bitrate: int | None = None) -> CANInterface:
    """Open an interface from its specification; see the module docstring."""
    if not isinstance(spec, str) or not spec:
        raise ValueError("Interface specification is empty")
    kind, _, rest = spec.partition(":")
    if not rest:
        kind, rest = "socketcan", spec
    if kind == "socketcan":
        virtual = rest.startswith("vcan")
        if not virtual and not allow_hardware:
            raise ValueError("Physical CAN output requires --allow-hardware and an isolated CAN test bench")
        return SocketCANInterface(rest)
    if kind == "pythoncan":
        backend, _, channel = rest.partition(":")
        if not backend or not channel:
            raise ValueError("Use pythoncan:<interface>:<channel>, e.g. pythoncan:pcan:PCAN_USBBUS1")
        if backend != "virtual" and not allow_hardware:
            raise ValueError("Physical CAN output requires --allow-hardware and an isolated CAN test bench")
        return PythonCANInterface(backend, channel, bitrate)
    if kind in {"udp", "tcp"}:
        match = re.fullmatch(r"\[?([^\]]+?)\]?:(\d{1,5})(?::(\d{1,5}))?", rest)
        if not match:
            raise ValueError(f"Use {kind}:<host>:<port>" + ("[:<local_port>]" if kind == "udp" else ""))
        host, port, local = match.group(1), int(match.group(2)), match.group(3)
        if not 1 <= port <= 65535 or (local and not 0 <= int(local) <= 65535):
            raise ValueError("Port must be 1–65535")
        if local and kind == "tcp":
            raise ValueError("A local port applies to UDP only")
        if not _is_loopback(host) and not allow_hardware:
            raise ValueError("A remote CAN gateway reaches a physical bus; add --allow-hardware for an isolated test bench")
        return GatewayInterface(kind, host, port, int(local) if local else None)
    raise ValueError(f"Unknown interface type '{kind}'; use socketcan:, pythoncan:, udp: or tcp:")


def socketcan_link_info(channel: str) -> dict | None:
    """Best-effort bitrate/state of a Linux CAN link, from `ip -details -json link show`."""
    if sys.platform != "linux" or not shutil.which("ip") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", channel):
        return None
    try:
        output = subprocess.run(["ip", "-details", "-json", "link", "show", "dev", channel],
                                capture_output=True, text=True, timeout=3, check=True).stdout
        link = json.loads(output)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    info = link.get("linkinfo", {}).get("info_data", {})
    return {"operstate": link.get("operstate"), "kind": link.get("linkinfo", {}).get("info_kind"),
            "state": info.get("state"), "bitrate": info.get("bittiming", {}).get("bitrate"),
            "restart_ms": info.get("restart_ms"), "txqueuelen": link.get("txqlen")}


def bench_warnings(spec: str, profile_bitrate: int) -> list[str]:
    """Checks worth printing before transmitting: bitrate mismatch, bus-off, short queue."""
    kind, _, rest = spec.partition(":")
    channel = spec if not rest else rest if kind == "socketcan" else None
    if not channel or channel.startswith("vcan"):
        return []
    info = socketcan_link_info(channel)
    if not info:
        return []
    warnings = []
    if info["bitrate"] and info["bitrate"] != profile_bitrate:
        warnings.append(f"{channel} runs at {info['bitrate']} bit/s but the profile assumes {profile_bitrate}")
    if info["state"] and info["state"] not in {"ERROR-ACTIVE"}:
        warnings.append(f"{channel} controller state is {info['state']}")
    if info["restart_ms"] == 0:
        warnings.append(f"{channel} has no automatic bus-off restart (ip link set {channel} type can restart-ms 100)")
    if info["txqueuelen"] is not None and info["txqueuelen"] < 100:
        warnings.append(f"{channel} txqueuelen is {info['txqueuelen']}; consider: ip link set {channel} txqueuelen 1000")
    return warnings


# Backwards-compatible name used by earlier releases and tests.
def SocketCANSink(channel: str = "vcan0", allow_hardware: bool = False) -> SocketCANInterface:
    if sys.platform != "linux" or not hasattr(socket, "AF_CAN"):
        raise ValueError("SocketCAN output requires Linux; offline generation works on all platforms")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", channel):
        raise ValueError("Invalid SocketCAN interface name")
    if not channel.startswith("vcan") and not allow_hardware:
        raise ValueError("Physical CAN output requires --allow-hardware and an isolated CAN test bench")
    return SocketCANInterface(channel)


def paced(frames: Iterable[Frame], speed: float = 1.0, realtime: bool = True,
          sink: CANInterface | None = None) -> Iterator[Frame]:
    """Pace frames in wall-clock time and optionally transmit them.

    When a sink is given, each yielded frame carries its actual Unix send time, so a recording
    made while transmitting can later be compared with what another node received.
    Frames dropped because the transmit queue stayed full are not yielded.
    """
    if not .1 <= finite(speed, "speed") <= 10:
        raise ValueError("speed must be between 0.1 and 10")
    started, first = time.monotonic(), None
    wall_origin = time.time() - started  # monotonic-anchored Unix time never steps backwards
    for frame in frames:
        if first is None:
            first = frame.timestamp
        if realtime:
            delay = started + (frame.timestamp - first) / speed - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        if sink is not None:
            if not sink.send(frame):
                continue
            frame = replace(frame, timestamp=wall_origin + time.monotonic())
        yield frame
