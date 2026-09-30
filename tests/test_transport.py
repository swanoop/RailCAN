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
