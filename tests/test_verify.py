import io
import json
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from railcan.cli import main
from railcan.frame import Frame
from railcan.profile import j1939_profile
from railcan.simulator import Simulator
from railcan.traces import write_trace
from railcan.transport import (
    GatewayInterface,
    gateway_packet,
    open_interface,
    paced,
    parse_gateway_packet,
)
from railcan.verify import missing_blocks, verify_frames, verify_telemetry

T0 = 1_800_000_000.0


def sent_frames(seconds=30):
    return [replace(f, timestamp=T0 + f.timestamp) for f in Simulator(j1939_profile()).frames(seconds)]


def telemetry(sent, profile, drop=(), duplicate=(), delay=0.25, value_error=None):
    probe = next(m for m in profile.messages if m.name == "Probe")
    oil = next(m for m in profile.messages if m.name == "EFL_P1")
    latest_oil = None
    for frame in sent:
        if frame.can_id == oil.can_id:
            latest_oil = frame.decode(oil)["EngineOilPressure"]
        if frame.can_id != probe.can_id:
            continue
        seq = frame.decode(probe)["ProbeSequence"]
        if seq in drop:
            continue
        value = latest_oil + (value_error if value_error and seq == 5 else 0)
        payload = {"timestamp": frame.timestamp, "signals": [
            {"signalCode": "PROBE_SEQ", "value": seq}, {"signalCode": "OIL_P", "value": value}]}
        record = {"received_at": frame.timestamp + delay, "payload": payload}
        yield record
        if seq in duplicate:
            yield record


class VerifyFramesTests(unittest.TestCase):
    def test_perfect_delivery(self):
        sent = sent_frames(5)
        received = [replace(f, timestamp=f.timestamp + .002) for f in sent]
        report = verify_frames(sent, received, j1939_profile())
        self.assertEqual(report["totals"]["missing"], 0)
        self.assertEqual(report["totals"]["delivery_ratio"], 1)
        self.assertAlmostEqual(report["latency_s"]["p50"], .002, places=5)

    def test_loss_duplicates_reorder_and_settle(self):
        sent = sent_frames(5)
        received = [replace(f, timestamp=f.timestamp + .01) for f in sent]
        # Lose a probe frame: its sequence number makes the payload unique. Identical payloads
        # (a constant signal) cannot be told apart, which is what the probe is for.
        probe_id = next(m.can_id for m in j1939_profile().messages if m.name == "Probe")
        lost = received.pop([i for i, f in enumerate(received) if f.can_id == probe_id][1])
        copy = [i for i, f in enumerate(received) if f.can_id == probe_id][2]
        received.insert(copy + 1, received[copy])               # duplicate
        received[30], received[31] = received[31], received[30]  # swap two frames
        received = [f for f in received if f.timestamp < T0 + 4.5]  # tail still in flight
        report = verify_frames(sent, received, j1939_profile(), settle_s=.6)
        totals = report["totals"]
        self.assertEqual(totals["missing"], 1)
        self.assertEqual(totals["duplicates"], 1)
        self.assertGreater(totals["excluded_in_flight"], 0)
        row = next(r for r in report["messages"] if r["can_id"] == lost.hex_id)
        self.assertEqual(row["missing"], 1)

    def test_relative_timestamps_skip_latency(self):
        frames = list(Simulator(j1939_profile()).frames(1))
        report = verify_frames(frames, frames)
        self.assertIsNone(report["latency_s"])
        self.assertIn("Unix", report["latency_note"])

    def test_missing_blocks(self):
        blocks = missing_blocks([(5, 5.0), (6, 6.0), (7, 7.0), (12, 12.0)])
        self.assertEqual([(b["from_seq"], b["to_seq"], b["count"]) for b in blocks], [(5, 7, 3), (12, 12, 1)])
        self.assertEqual(blocks[0]["duration_s"], 2.0)


class VerifyTelemetryTests(unittest.TestCase):
    def samples(self, records):
        from railcan.verify import extract_signals, payload_timestamp
        for number, record in enumerate(records, 1):
            yield {"arrival": record["received_at"], "sampled": payload_timestamp(record["payload"]),
                   "signals": extract_signals(record["payload"]), "line": number}

    def test_probe_accounting_and_latency(self):
        profile = j1939_profile()
        sent = sent_frames(30)
        records = list(telemetry(sent, profile, drop={4, 5, 6, 20}, duplicate={10}))
        report = verify_telemetry(sent, profile, self.samples(records), probe_field="PROBE_SEQ",
                                  compare={"EngineOilPressure": "OIL_P"})
        totals = report["totals"]
        self.assertEqual((totals["sent"], totals["received"], totals["missing"], totals["duplicates"]), (30, 26, 4, 1))
        self.assertEqual([(b["from_seq"], b["to_seq"]) for b in report["missing_blocks"]], [(4, 6), (20, 20)])
        self.assertAlmostEqual(report["latency_s"]["p95"], .25, places=6)
        fidelity = report["fidelity"]["EngineOilPressure"]
        self.assertEqual((fidelity["matched"], fidelity["mismatched"]), (27, 0))

    def test_value_mismatch_detected(self):
        profile = j1939_profile()
        sent = sent_frames(10)
        report = verify_telemetry(sent, profile, self.samples(telemetry(sent, profile, value_error=50)),
                                  probe_field="PROBE_SEQ", compare={"EngineOilPressure": "OIL_P"},
                                  time_source="sampled")
        self.assertEqual(report["fidelity"]["EngineOilPressure"]["mismatched"], 1)

    def test_cli_end_to_end_and_thresholds(self):
        profile = j1939_profile()
        sent = sent_frames(20)
        with tempfile.TemporaryDirectory() as folder:
            sent_path, rx_path = Path(folder) / "sent.log", Path(folder) / "rx.jsonl"
            write_trace(sent, sent_path, profile)
            rx_path.write_text("".join(json.dumps(r) + "\n" for r in telemetry(sent, profile, drop={3})))
            base = ["verify", str(sent_path), str(rx_path), "--telemetry", "--profile", "j1939",
                    "--probe-field", "PROBE_SEQ", "--compare", "EngineOilPressure=OIL_P"]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(base), 0)
                self.assertEqual(main(base + ["--max-loss", "1"]), 2)
                self.assertEqual(main(base + ["--max-p95-ms", "100"]), 2)
            self.assertIn("Missing blocks: 1", out.getvalue())
            self.assertIn("FAIL: loss 5.000%", err.getvalue())


class GatewayTests(unittest.TestCase):
    def test_packet_format(self):
        frame = Frame(0, 0x18FEEE00, b"\x01\x02", True)
        packet = gateway_packet(frame)
        self.assertEqual(packet.hex().upper(), "8218FEEE000102000000000000")
        self.assertEqual(parse_gateway_packet(packet, 0), frame)
        self.assertEqual(parse_gateway_packet(bytes([0x42]) + bytes(12), 0).kind, "remote_frame")

    def test_remote_gateway_requires_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow-hardware"):
            open_interface("udp:192.0.2.10:4001")
        with self.assertRaisesRegex(ValueError, "allow-hardware"):
            open_interface("pythoncan:pcan:PCAN_USBBUS1")
        with self.assertRaisesRegex(ValueError, "Unknown interface"):
            open_interface("serial:/dev/ttyUSB0")

    def test_udp_loopback_send_and_receive(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        port = receiver.getsockname()[1]
        receiver.close()
        rx = open_interface(f"udp:127.0.0.1:9:{port}")
        tx = open_interface(f"udp:127.0.0.1:{port}")
        frames = list(Simulator(j1939_profile()).frames(.2))
        sent = list(paced(frames, 10, realtime=False, sink=tx))
        got = [rx.recv(1) for _ in frames]
        tx.close()
        rx.close()
        self.assertEqual([(f.can_id, f.data) for f in got], [(f.can_id, f.data) for f in frames])
        self.assertTrue(all(f.timestamp > 1e9 for f in sent))
        self.assertEqual(tx.stats.sent, len(frames))

    def test_tcp_stream_reassembles_frames(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        frames = [Frame(0, 0x123, bytes([i]), False) for i in range(5)]

        def serve():
            conn, _ = server.accept()
            data = b"".join(gateway_packet(f) for f in frames)
            conn.sendall(data[:20])
            conn.sendall(data[20:])
            conn.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        bus = open_interface(f"tcp:127.0.0.1:{server.getsockname()[1]}")
        got = [bus.recv(2) for _ in frames]
        bus.close()
        server.close()
        self.assertEqual([f.data for f in got], [f.data for f in frames])

    def test_full_queue_is_counted_not_fatal(self):
        class Full(GatewayInterface):
            def __init__(self):
                from railcan.transport import Stats
                self.stats = Stats()
                self.name = "full"

            def _send(self, frame):
                from railcan.transport import _QueueFull
                raise _QueueFull

        from railcan import transport
        original = transport.SEND_RETRY_S
        transport.SEND_RETRY_S = .005
        try:
            bus = Full()
            out = list(paced([Frame(0, 1, b"")], realtime=False, sink=bus))
        finally:
            transport.SEND_RETRY_S = original
        self.assertEqual(out, [])
        self.assertEqual(bus.stats.dropped, 1)
        self.assertIn("DROPPED", bus.stats.summary())


if __name__ == "__main__":
    unittest.main()
