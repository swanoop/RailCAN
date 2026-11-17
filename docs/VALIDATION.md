# Release validation · 0.2.0

Validated locally on Linux with Python 3.12. Remote GitHub Actions status is not claimed here.

| Check | Result |
|---|---|
| Test suite | 83 tests and 56 subtests passed |
| Coverage | 82% total line coverage |
| Signed signal encoding | Known byte-level vectors verified |
| Deterministic generation | Same seed gives identical frames |
| Journey model | Door/traction interlock, speed bounds, nondecreasing distance and station cycles checked |
| Fault windows | Dropout, corruption, counter wrap/freeze, sensor recovery and emergency braking checked |
| Trace readers and writers | Candump, CSV, JSONL and PCAP round-trip, including extended IDs and short or empty payloads |
| J1939 | PGNs, 29-bit IDs, DTC packing, DM1 and TP.BAM reassembly covered by tests |
| J1939 scenario smoke test | `multi_dtc` produced 174 frames in 2 seconds, including two DM1 broadcasts and no incomplete BAM transfers |
| Local dashboard and API | Controls, validation, snapshot exports and worker error paths covered by tests |
| Package build | Source distribution and wheel built successfully |
| Installed command | `railcan --version` reported 0.2.0; built-in profiles generated their expected rates |
| Optional python-can transport | Two virtual-bus nodes exchanged an extended 29-bit J1939 frame |
| Static security scan | No medium- or high-severity Bandit findings |

Physical CAN adapters, a real SocketCAN or `vcan` interface, a commercial CAN-over-IP gateway,
and a live MQTT/TLS broker were not available for this validation. Unit tests cover packet
layout, pacing, explicit hardware opt-in, queue handling, MQTT framing and TLS configuration,
but those tests do not replace bench validation with the intended devices and drivers.

A transmit-side recording proves that the selected interface accepted a frame. It does not, by
itself, prove acknowledgement or physical-bus delivery. Use a second CAN adapter as the independent
on-bus reference capture. The included vehicle data and checksum are synthetic demonstrators. OEM
compatibility, compatibility with an unspecified gateway protocol, CANopen conformance, IEC 61375
conformance and safety certification are not claimed.
