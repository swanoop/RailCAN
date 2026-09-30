# Usage

Run `python -m railcan --help` for the command list. If installed with `python -m pip install -e .`, the shorter `railcan` command is available.

## Dashboard

`python -m railcan ui` opens a dashboard on loopback only. `--no-browser` leaves it running without opening a browser; `--port 0` picks a free local port. A custom profile can be supplied with `--profile`.

1. Select a scenario, duration, seed, and time multiplier.
2. Start, pause, or resume the simulation.
3. Select a message row to inspect its latest decoded signals.
4. Inject a timed fault, or clear all current and scheduled fault windows.
5. Export a complete trace and the matching DBC/profile.

The dashboard displays model state and the latest received simulated CAN frames. A message becomes **STALE** after more than 2.1 nominal periods without a new frame. This is a simple message-gap indication, not an ECU health assessment. The chart retains the most recent 72 simulated seconds; the raw panel retains 80 frames. Recordings are streamed to temporary files and preserve the full run. Runs are limited to 3600 simulated seconds. Reset creates a new recording; closing the server deletes session recordings.

At 10×, ten simulated seconds elapse in roughly one wall-clock second. The message schedule and exported timestamps remain in simulated time. The nominal bus-load card describes 1× traffic, not measured hardware utilisation. Local API writes require the per-session token and accept only local origins. No adapter transmission is exposed in the dashboard.

## Offline generation

```bash
python -m railcan simulate --scenario normal --duration 120 --seed 42 --out service.log
python -m railcan simulate --duration 30 --out service.csv
python -m railcan simulate --duration 30 --out service.jsonl
python -m railcan simulate --duration 30 --out service.pcap
```

Without `--out` or `--socketcan`, the command writes candump frames to standard output and a summary to standard error. Use `--realtime --rate 0.5` for slow-motion output. Offline generation does not wait between frames.

Frames are scheduled on the half-open interval `[0, duration)`, quantised to 10 ms. All message streams begin at zero. The default one-second run therefore has 62 frames. Same-time frames are ordered by CAN ID; this is deterministic ordering, not a simulation of bus arbitration. Fractional durations may finish at the next tick boundary.

Candump, CSV, and JSONL timestamps are relative simulated seconds. PCAP uses the same values by default, so Wireshark's absolute timestamps start in 1970. Set `--epoch` to a Unix timestamp if you need a different PCAP time origin. Formats are inferred from the filename or chosen with `--format`.

## Fault files

```json
[
  {"kind": "stuck_speed", "start_s": 20, "duration_s": 10, "message": "Motion"},
  {"kind": "speed_spike", "start_s": 40, "duration_s": 2, "message": "Motion", "value": 160},
  {"kind": "message_dropout", "start_s": 50, "duration_s": 5, "message": "Traction"}
]
```

`--faults` replaces the scenario's preset fault windows. Windows include their start and exclude their end. A window between ticks is observed at the next scheduled tick/message. Physical-state faults affect the train model; frame faults affect the emitted data.

| Kind | Effect |
|---|---|
| `emergency_brake` | Applies emergency deceleration and inhibits traction |
| `door_obstruction` | Holds open doors while at a station, preventing departure |
| `hvac_overheat` | Raises cabin temperature during the window |
| `low_voltage` | Reduces DC supply and battery voltage |
| `stuck_speed` | Freezes the reported speed while the model continues |
| `speed_spike` | Substitutes a fixed reported speed; default 160 km/h |
| `message_dropout` | Omits selected messages while source counters continue |
| `checksum_error` | Inverts the selected message's XOR checksum byte |
| `counter_freeze` | Freezes the selected message's alive counter |

Frame faults default to `Motion`, except dropout which defaults to `Traction`. Provide `message` to select a custom message. Sensor faults require that message to contain a `speed_kph` source. Counters and sensor readings return to their current values after the fault; counters may jump at recovery. At most 100 fault events are accepted per session.

## Replay and inspection

```bash
python -m railcan replay service.log --out service.pcap
python -m railcan replay service.pcap --out service.jsonl
python -m railcan inspect service.pcap --json
python -m railcan replay service.log --socketcan vcan0 --rate 0.5
```

Replay validates the input before transmitting. Pacing is relative to the first frame, so epoch timestamps do not cause a wait from 1970. Original timestamps and data are preserved in conversions. Output must differ from input. Only Classical data frames are accepted.

Inspection uses the selected profile to check the demonstrator checksum and alive counter. Counter expectations account for missing nominal time slots and modulo-256 wrap. Missing-frame estimates infer internal gaps only; they cannot identify an entirely absent stream or missing frames before its first/after its last observation. Unknown IDs are counted but not decoded. Real OEM traffic needs its own decoder and checksum algorithm.

## SocketCAN setup

Virtual example:

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
python -m railcan simulate --socketcan vcan0 --duration 10
```

For an isolated physical bench, a typical setup is:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 250000
sudo ip link set can0 up
python -m railcan simulate --socketcan can0 --allow-hardware --duration 10
```

SocketCAN transmission is always wall-clock paced. An accelerated generation request is rejected if its profile estimate would exceed 80% nominal bus load. RailCAN never runs interface-configuration commands itself. Host scheduling is best effort: Python and a general-purpose operating system do not guarantee hard real-time CAN timing.
