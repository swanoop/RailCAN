# Architecture

RailCAN separates the train model, signal encoding, frame scheduling, recordings, and outputs. The core uses only Python's standard library.

| Module | Responsibility |
|---|---|
| `profile.py` | Immutable message/signal definitions, built-in EMU and J1939 profiles, waveform sources, validation, DBC export |
| `j1939.py` | 29-bit priority/PGN/address identifiers, DM1 encoding, TP.BAM transmit and reassembly |
| `model.py` | Journey state machine, correlated telemetry, on-board engine, simplified thermal behaviour |
| `frame.py` | Little-endian packing, signed scaling, counters, demonstrator checksum |
| `simulator.py` | Integer 10 ms clock, message cycles, waveforms, sequence probes, DM1/BAM scheduling, fault windows, seeds |
| `traces.py` | Streaming candump/CSV/JSONL/PCAP readers, writers and trace inspection |
| `transport.py` | SocketCAN, optional python-can adapters, UDP/TCP CAN gateways; send retry and drop counting, receive, error frames, pacing |
| `mqtt.py` | Standard-library MQTT 3.1.1 subscriber/publisher, MessagePack codec, payload timestamps |
| `verify.py` | Sent-versus-received comparison for CAN recordings and decoded telemetry |
| `dashboard.py` | Local HTTP API, simulation worker, disk-backed session recordings |
| `web/` | Local HTML, CSS and JavaScript dashboard, without external assets |
| `cli.py` | User commands and actionable input-error reporting |

## Scheduling and fidelity

The simulation clock is independent of wall-clock time. All periods are integer multiples of 10 ms, preventing floating-point drift in message scheduling. A seed controls the small cabin-temperature fluctuations. CAN IDs order simultaneous frames deterministically; the simulator does not model electrical signalling, arbitration timing, bit stuffing, retransmission, bus-off, or hard real-time behaviour.

The model keeps doors and traction consistent and derives distance, acceleration, RPM and current from the journey. It is a traffic generator, not an engineering prediction of braking distances, traction energy, thermal performance, or railway safety function behaviour.

## Dashboard storage and controls

The worker writes JSONL frames to a temporary session file and retains bounded recent frames, chart samples, and events in memory. Exports snapshot the flushed frame count and stream the snapshot into a temporary download, allowing capture to continue. Reset uses a new file; old files survive until server exit so an in-progress export remains valid. All session files are removed at shutdown.

## Interfaces and verification

All interfaces share one small contract: `send(frame)` returns whether the frame was accepted,
`recv(timeout)` returns a `Frame`, a `BusEvent` (error frame, remote frame) or `None`, and
`close()`. A full transmit queue is retried for 100 ms and then counted as a drop, never
raised, so a long bench run reports losses instead of stopping. While transmitting, `paced()`
stamps each accepted frame with its interface-acceptance time on a monotonic-anchored Unix clock.
That recording is the source-side reference for `verify`; an independent receive-side recording
is required to establish what appeared on the bus or reached a downstream system.

`verify` matches CAN frames by ID and payload. Identical payloads (a constant signal) cannot be
told apart, so per-message counts are exact but latency and loss for such frames are
approximate; the probe message's sequence number makes matching exact for both CAN and
decoded telemetry.

The server binds to `127.0.0.1`. It checks Host/Origin values, uses a per-session API token, serves only named static assets, and exposes no arbitrary filesystem route. SocketCAN is a CLI capability and is not controlled through the HTTP API.

## Extending the project

Add a model source in `model.py`, register it in `MODEL_SOURCES`, and add its signal to a JSON profile, or use a waveform source without touching the model. Keep trailer bytes free on messages that use `counter_xor`. Add meaningful coverage for its numeric encoding and effect on related signals. A different wire checksum or a real OEM layout requires an explicit codec extension; changing the DBC alone does not change the checksum algorithm. A complete CANopen implementation would require a separate protocol layer.
