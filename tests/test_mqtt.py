import json
import math
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from railcan.cli import main
from railcan.mqtt import (
    MQTTClient,
    decode_payload,
    msgpack_pack,
    msgpack_unpack,
    parse_time,
)


def remaining(value):
    out = bytearray()
    while True:
        byte, value = value % 128, value // 128
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


class FakeBroker:
    """Accepts one client, acknowledges CONNECT/SUBSCRIBE, then sends the queued PUBLISH packets."""

    def __init__(self, messages, qos=1, refuse=0):
        self.messages, self.qos, self.refuse = messages, qos, refuse
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = []
        self.connect_body = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def read_packet(self, conn):
        header = conn.recv(1)
        if not header:
            return None, b""
        length, multiplier = 0, 1
        while True:
            byte = conn.recv(1)[0]
            length += (byte & 0x7F) * multiplier
            multiplier *= 128
            if not byte & 0x80:
                break
        body = b""
        while len(body) < length:
            body += conn.recv(length - len(body))
        return header[0], body

    def run(self):
        conn, _ = self.server.accept()
        with conn:
            header, self.connect_body = self.read_packet(conn)
            conn.sendall(bytes([0x20, 2, 0, self.refuse]))
            if self.refuse:
                return
            header, body = self.read_packet(conn)
            conn.sendall(bytes([0x90, 3]) + body[:2] + bytes([self.qos]))
            for number, (topic, payload) in enumerate(self.messages, 1):
                raw_topic = topic.encode()
                variable = len(raw_topic).to_bytes(2, "big") + raw_topic
                if self.qos:
                    variable += number.to_bytes(2, "big")
                packet = variable + payload
                conn.sendall(bytes([0x30 | (self.qos << 1)]) + remaining(len(packet)) + packet)
            conn.settimeout(2)
            try:
                while True:
                    header, body = self.read_packet(conn)
                    if header is None:
                        break
                    self.received.append((header, body))
            except OSError:
                pass

    def close(self):
        self.server.close()


class MessagePackTests(unittest.TestCase):
    def test_round_trip(self):
        value = {"timestamp": "2026-01-01T00:00:00Z", "version": "1.0",
                 "signals": [{"signalCode": "A", "value": 88.25, "dataType": "float"},
                             {"signalCode": "B", "value": True}, {"signalCode": "C", "value": -70000},
                             {"signalCode": "D", "value": None}, {"signalCode": "E", "value": "x" * 40}],
                 "list": list(range(20)), "big": 2**40, "neg": -5}
        self.assertEqual(msgpack_unpack(msgpack_pack(value)), value)

    def test_timestamp_extension_and_errors(self):
        self.assertEqual(msgpack_unpack(b"\xd6\xff" + (1700000000).to_bytes(4, "big")), 1700000000.0)
        for bad in (b"\xc1", b"\x92\x01", b"\x01\x02"):
            with self.assertRaises(ValueError):
                msgpack_unpack(bad)

    def test_payload_detection(self):
        self.assertEqual(decode_payload(b'{"a": 1}'), ({"a": 1}, "json"))
        self.assertEqual(decode_payload(msgpack_pack({"a": 1})), ({"a": 1}, "msgpack"))
        self.assertEqual(decode_payload(b"\xc1\xc1")[1], "raw")

    def test_time_parsing(self):
        self.assertEqual(parse_time("2026-02-10T21:35:00Z"), 1770759300.0)
        self.assertEqual(parse_time(1770759300000), 1770759300.0)
        self.assertIsNone(parse_time("soon"))
        self.assertIsNone(parse_time(math.nan))


class MQTTTests(unittest.TestCase):
    def test_subscribe_receive_and_acknowledge(self):
        payload = msgpack_pack({"timestamp": "2026-01-01T00:00:00Z", "signals": [{"signalCode": "S", "value": 1}]})
        broker = FakeBroker([("bench/t", payload), ("bench/t", b'{"x": 2}')])
        client = MQTTClient("127.0.0.1", broker.port, username="u", password="p")
        self.assertEqual(client.subscribe("bench/#", 1), 1)
        first, second = client.poll(2), client.poll(2)
        client.close()
        broker.thread.join(3)
        broker.close()
        self.assertEqual(first.topic, "bench/t")
        self.assertEqual(decode_payload(first.payload)[0]["signals"][0]["value"], 1)
        self.assertEqual(decode_payload(second.payload)[0], {"x": 2})
        acks = [body for header, body in broker.received if header == 0x40]
        self.assertEqual(acks, [b"\x00\x01", b"\x00\x02"])
        self.assertIn(b"\x00\x01u\x00\x01p", broker.connect_body)

    def test_refused_connection(self):
        broker = FakeBroker([], refuse=5)
        with self.assertRaisesRegex(ValueError, "not authorised"):
            MQTTClient("127.0.0.1", broker.port)
        broker.close()

    def test_cli_records_jsonl(self):
        payload = msgpack_pack({"signals": {"ProbeSequence": 7}})
        broker = FakeBroker([("a", payload)] * 3, qos=0)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "rx.jsonl"
            code = main(["mqtt-listen", "--host", "127.0.0.1", "--port", str(broker.port),
                         "--topic", "a", "--qos", "0", "--out", str(out), "--count", "3"])
            broker.close()
            self.assertEqual(code, 0)
            records = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["encoding"], "msgpack")
        self.assertEqual(records[0]["payload"]["signals"]["ProbeSequence"], 7)
        self.assertGreater(records[0]["received_at"], 1e9)


if __name__ == "__main__":
    unittest.main()
