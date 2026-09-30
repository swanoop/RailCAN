import json
import struct
import tempfile
import unittest
from pathlib import Path

from railcan.frame import Frame
from railcan.simulator import Fault, Simulator
from railcan.traces import inspect_trace, read_trace, socketcan_packet, write_trace


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_formats_round_trip(self):
        frames = list(Simulator(faults=[Fault("checksum_error", .2, .1)]).frames(1))
        frames += [Frame(1.1, 0x1ABCDE, b"\x01\x02", True), Frame(1.2, 3, b"")]
        for extension in ["log", "csv", "jsonl", "pcap"]:
            with self.subTest(extension=extension):
                path = self.root / f"trace.{extension}"
                self.assertEqual(write_trace(frames, path), len(frames))
                self.assertEqual(list(read_trace(path)), frames)

    def test_pcap_byte_order_and_link_type(self):
        frame = Frame(.123456, 0x123, bytes(range(8)))
        path = self.root / "test.pcap"
        write_trace([frame], path)
        data = path.read_bytes()
        self.assertEqual(struct.unpack("<I", data[20:24])[0], 227)
        self.assertEqual(data[40:44], b"\0\0\x01\x23")
        self.assertEqual(data[44], 8)
        self.assertEqual(data[48:56], bytes(range(8)))
        self.assertEqual(len(socketcan_packet(frame)), 16)

    def test_nanosecond_big_endian_pcap(self):
        path = self.root / "big.pcap"
        frame = Frame(2.5, 0x123, b"abc")
        path.write_bytes(struct.pack(">IHHIIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 227)
                         + struct.pack(">IIII", 2, 500000000, 16, 16)
                         + socketcan_packet(frame, True))
        self.assertEqual(list(read_trace(path)), [frame])

    def test_pcap_epoch(self):
        path = self.root / "epoch.pcap"
        write_trace([Frame(.5, 1, b"abc")], path, epoch=1700000000)
        self.assertEqual(next(read_trace(path)).timestamp, 1700000000.5)

    def test_truncated_pcap_rejected(self):
        path = self.root / "bad.pcap"
        write_trace([Frame(0, 1, b"abc")], path)
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, "Truncated"):
            list(read_trace(path))

    def test_unsupported_candump_and_unsorted_timestamps(self):
        path = self.root / "bad.log"
        for text in ["(0.0) vcan0 123##100\n", "(0.0) vcan0 123#R\n", "(1.0) vcan0 123#01\n(0.5) vcan0 123#02\n"]:
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    list(read_trace(path))

    def test_inspection_dropout_does_not_mislabel_counter(self):
        sim = Simulator(faults=[Fault("message_dropout", .2, .3)])
        summary = inspect_trace(sim.frames(1))
        row = next(row for row in summary["messages"] if row["can_id"] == "200")
        self.assertEqual(row["missing_frames_estimate"], 3)
        self.assertEqual(row["counter_anomalies"], 0)

    def test_counter_freeze_and_corruption_detected(self):
        sim = Simulator(faults=[Fault("counter_freeze", .2, .3), Fault("checksum_error", .6, .1)])
        summary = inspect_trace(sim.frames(1))
        row = next(row for row in summary["messages"] if row["can_id"] == "100")
        self.assertGreater(row["counter_anomalies"], 0)
        self.assertEqual(row["checksum_errors"], 2)

    def test_unknown_frames_not_assessed_with_demo_checksum(self):
        summary = inspect_trace([Frame(0, 0x123, b"12345678")])
        self.assertEqual(summary["messages"][0]["message"], "Unknown")
        self.assertEqual(summary["messages"][0]["checksum_errors"], 0)

    def test_malformed_jsonl_and_csv(self):
        path = self.root / "bad.jsonl"
        path.write_text(json.dumps({"timestamp": 0, "can_id": 256, "data_hex": "ZZ"})+"\n")
        with self.assertRaises(ValueError):
            list(read_trace(path))
        path = self.root / "bad.csv"
        path.write_text("timestamp,can_id_hex,extended,dlc,data_hex\n0,100,0,8,AB\n")
        with self.assertRaisesRegex(ValueError, "DLC"):
            list(read_trace(path))

    def test_empty_trace(self):
        self.assertEqual(inspect_trace([])["frames"], 0)
