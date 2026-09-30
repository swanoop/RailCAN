"""Optional Linux SocketCAN output with explicit physical-interface opt-in."""

from __future__ import annotations

import re
import socket
import sys
import time
from typing import Iterable, Iterator

from .frame import Frame
from .profile import finite
from .traces import socketcan_packet


class SocketCANSink:
    def __init__(self, channel: str = "vcan0", allow_hardware: bool = False):
        if sys.platform != "linux" or not hasattr(socket, "AF_CAN"):
            raise ValueError("SocketCAN output requires Linux; offline generation works on all platforms")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", channel):
            raise ValueError("Invalid SocketCAN interface name")
        if not channel.startswith("vcan") and not allow_hardware:
            raise ValueError("Physical CAN output requires --allow-hardware and an isolated CAN test bench")
        self.channel = channel
        self.socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.socket.settimeout(1.0)
        try:
            self.socket.bind((channel,))
        except OSError as exc:
            self.socket.close()
            raise ValueError(f"Cannot open {channel}: {exc}. Configure the interface first; see docs/USAGE.md") from exc

    def send(self, frame: Frame) -> None:
        packet = socketcan_packet(frame)
        if self.socket.send(packet) != len(packet):
            raise OSError("Incomplete SocketCAN send")

    def close(self) -> None:
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def paced(frames: Iterable[Frame], speed: float = 1.0, realtime: bool = True,
          sink: SocketCANSink | None = None) -> Iterator[Frame]:
    if not .1 <= finite(speed, "speed") <= 10:
        raise ValueError("speed must be between 0.1 and 10")
    started, first = time.monotonic(), None
    for frame in frames:
        if first is None:
            first = frame.timestamp
        if realtime:
            deadline = started + (frame.timestamp - first) / speed
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        if sink is not None:
            sink.send(frame)
        yield frame
