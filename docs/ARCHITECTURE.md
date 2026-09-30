# Architecture

RailCAN separates the train model, signal encoding, frame scheduling, recordings, and outputs. The core uses only Python's standard library.

| Module | Responsibility |
|---|---|
| `profile.py` | Immutable train/message/signal definitions, validation, JSON profiles, DBC export |
| `model.py` | Journey state machine, correlated telemetry, simplified thermal behaviour |
| `frame.py` | Little-endian packing, signed scaling, counters, demonstrator checksum |
| `simulator.py` | Integer 10 ms clock, message cycles, fault windows and seed handling |
| `traces.py` | Streaming candump/CSV/JSONL/PCAP readers, writers and trace inspection |
| `transport.py` | Native Linux SocketCAN and monotonic-clock pacing |
| `dashboard.py` | Local HTTP API, simulation worker, disk-backed session recordings |
| `web/` | Local HTML, CSS and JavaScript dashboard, without external assets |
| `cli.py` | User commands and actionable input-error reporting |

## Scheduling and fidelity

The simulation clock is independent of wall-clock time. All periods are integer multiples of 10 ms, preventing floating-point drift in message scheduling. A seed controls the small cabin-temperature fluctuations. CAN IDs order simultaneous frames deterministically; the simulator does not model electrical signalling, arbitration timing, bit stuffing, retransmission, bus-off, or hard real-time behaviour.

The model keeps doors and traction consistent and derives distance, acceleration, RPM and current from the journey. It is a traffic generator, not an engineering prediction of braking distances, traction energy, thermal performance, or railway safety function behaviour.

## Dashboard storage and controls

The worker writes JSONL frames to a temporary session file and retains bounded recent frames, chart samples, and events in memory. Exports snapshot the flushed frame count and stream the snapshot into a temporary download, allowing capture to continue. Reset uses a new file; old files survive until server exit so an in-progress export remains valid. All session files are removed at shutdown.

The server binds to `127.0.0.1`. It checks Host/Origin values, uses a per-session API token, serves only named static assets, and exposes no arbitrary filesystem route. SocketCAN is a CLI capability and is not controlled through the HTTP API.

## Extending the project

Add a model source in `model.py`, register it in `MODEL_SOURCES`, and add its signal to a JSON profile. Keep reserved trailer bytes free. Add meaningful coverage for its numeric encoding and effect on related signals. A different wire checksum or a real OEM layout requires an explicit codec extension; changing the DBC alone does not change the checksum algorithm. A complete CANopen implementation would require a separate protocol layer.
