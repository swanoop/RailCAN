import socket
import sys
import unittest
from unittest.mock import Mock, patch

from railcan.frame import Frame
from railcan.transport import SocketCANSink, paced


class TransportTests(unittest.TestCase):
    def test_pacing_uses_relative_timestamps(self):
        frames = [Frame(1700000000, 1, b"a"), Frame(1700000001, 1, b"b")]
        with patch("railcan.transport.time.monotonic", return_value=100), patch("railcan.transport.time.sleep") as sleep:
            self.assertEqual(list(paced(frames, speed=2)), frames)
            sleep.assert_called_once_with(.5)

    def test_bad_pacing_rate(self):
        for value in [0, float("nan"), 20]:
            with self.assertRaises(ValueError):
                list(paced([], value))

    @unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "AF_CAN"), "Linux SocketCAN API required")
    def test_physical_interface_requires_opt_in(self):
        with patch("railcan.transport.socket.socket") as create_socket:
            with self.assertRaisesRegex(ValueError, "allow-hardware"):
                SocketCANSink("can0")
            create_socket.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "AF_CAN"), "Linux SocketCAN API required")
    def test_send_and_bind_failure_cleanup(self):
        sock = Mock()
        sock.send.return_value = 16
        with patch("railcan.transport.socket.socket", return_value=sock):
            with SocketCANSink("vcan0") as sink:
                sink.send(Frame(0, 0x100, b"abcd"))
            self.assertEqual(len(sock.send.call_args.args[0]), 16)
            sock.close.assert_called_once()
        sock = Mock()
        sock.bind.side_effect = OSError("No such device")
        with patch("railcan.transport.socket.socket", return_value=sock), self.assertRaises(ValueError):
            SocketCANSink("vcan0")
        sock.close.assert_called_once()


class PythonCANTests(unittest.TestCase):
    def fake_can(self):
        import types
        module = types.ModuleType("can")

        class CanOperationError(Exception):
            pass

        class Message:
            def __init__(self, arbitration_id=0, data=b"", is_extended_id=False, is_error_frame=False,
                         is_remote_frame=False):
                self.arbitration_id, self.data, self.is_extended_id = arbitration_id, data, is_extended_id
                self.is_error_frame, self.is_remote_frame, self.is_fd = is_error_frame, is_remote_frame, False

        class Bus:
            def __init__(self, **options):
                self.options, self.sent, self.inbox, self.full = options, [], [], 0

            def send(self, message, timeout=None):
                if self.full:
                    self.full -= 1
                    raise CanOperationError("buffer full")
                self.sent.append(message)

            def recv(self, timeout=None):
                return self.inbox.pop(0) if self.inbox else None

            def shutdown(self):
                self.closed = True

        module.CanOperationError, module.Message, module.Bus = CanOperationError, Message, Bus
        return module

    def test_send_receive_errors_and_bitrate(self):
        from railcan.transport import BusEvent, open_interface
        can = self.fake_can()
        with patch.dict(sys.modules, {"can": can}):
            bus = open_interface("pythoncan:pcan:PCAN_USBBUS1", allow_hardware=True, bitrate=250000)
            self.assertEqual(bus.bus.options, {"interface": "pcan", "channel": "PCAN_USBBUS1", "bitrate": 250000})
            bus.bus.full = 2
            self.assertTrue(bus.send(Frame(0, 0x18FEEE00, b"\x01", True)))
            self.assertEqual(bus.stats.retries, 2)
            self.assertTrue(bus.bus.sent[0].is_extended_id)
            bus.bus.inbox = [can.Message(0x123, b"\x05", False),
                             can.Message(0x20 | 0x4, bytes([0, 0x10, 0, 0, 0, 0, 128, 3]), is_error_frame=True)]
            frame, event = bus.recv(0), bus.recv(0)
            self.assertEqual((frame.can_id, frame.data, frame.extended), (0x123, b"\x05", False))
            self.assertIsInstance(event, BusEvent)
            self.assertEqual(event.detail["classes"], ["controller", "no_ack"])
            self.assertEqual(event.detail["controller"], ["rx_passive"])
            self.assertEqual(event.detail["tx_errors"], 128)
            bus.close()

    def test_missing_python_can_is_explained(self):
        from railcan.transport import open_interface
        with patch.dict(sys.modules, {"can": None}), self.assertRaisesRegex(ValueError, "railcan\\[hardware\\]"):
            open_interface("pythoncan:virtual:test")
