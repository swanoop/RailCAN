# Black-box testing a CAN-to-cloud pipeline

RailCAN can act as the known signal source at one end of a telemetry chain and as the
measuring instrument at the other. A typical chain is:

```
RailCAN ─► USB-CAN adapter ─► CAN bus ─► acquisition gateway ─► edge computer ─► router/VPN ─► broker or storage
   ▲                              │                                                               │
   └──────── sent recording       └── reference adapter: ACKs + records                            └── railcan mqtt-listen
```

Everything between the bus and the broker is treated as a black box. RailCAN records frames
accepted by its transmitting interface. A second adapter provides the independent on-bus
reference capture. With those two records, `railcan verify` can report what the chain lost,
duplicated, reordered or delayed, and whether decoded values arrived intact.

## 1. Wire the bus correctly

* **Two ACKing nodes.** A CAN frame needs at least one other node to acknowledge it. Many
  monitoring devices are receive-only (listen-only) and never ACK. With only RailCAN's adapter
  and such a device on the bus, every frame fails with an ACK error and is retransmitted; the
  device may see duplicates or nothing. Add a second adapter in normal mode (the reference
  adapter). It ACKs the traffic and records what was really on the wire.
* **Termination.** 120 Ω at each physical end of the bus, and nowhere else. Measure about
  60 Ω between CAN-H and CAN-L with power off.
* **Bitrate.** Every node must use the same bitrate. J1939 networks normally run at 250 kbit/s
  (some at 500 kbit/s). The profile's `bitrate` is used for load estimates and is passed to
  python-can adapters; for SocketCAN set it with `ip link`.

Linux SocketCAN set-up for two adapters:

```bash
for n in can0 can1; do
  sudo ip link set $n down
  sudo ip link set $n type can bitrate 250000 restart-ms 100
  sudo ip link set $n txqueuelen 1000
  sudo ip link set $n up
done
```

`restart-ms` recovers automatically from bus-off; a longer `txqueuelen` avoids queue-full drops
during bursts. RailCAN prints a warning before transmitting if the link bitrate differs from the
profile, the controller is not error-active, or these settings are missing.

## 2. Prove a receive-only device really is passive

```bash
# Terminal 1: watch for error frames on the transmitting adapter.
railcan monitor -i can0 --allow-hardware --events events.jsonl --quiet
# Terminal 2: transmit with the reference adapter disconnected.
railcan simulate --profile j1939 -i can0 --allow-hardware --duration 10
```

Turn on bus-error reporting first (`sudo ip link set can0 type can bitrate 250000 berr-reporting on`);
many drivers only report individual ACK errors with it. With only the device under test
connected, a passive device produces `no_ack` error events
and the transmitter's error counters climb (`tx_passive`). If frames are acknowledged without
the reference adapter, the device is not passive. Repeat across the device's power-up, reset
and power-loss to cover those transitions.

## 3. Generate traffic and keep the sent recording

```bash
railcan simulate --profile j1939 --scenario multi_dtc --duration 3600 \
  -i can0 --allow-hardware --out sent.log
```

When transmitting, every recorded frame carries the Unix time at which the interface accepted
it. That timestamp does not prove acknowledgement or physical-bus delivery. Frames the interface
could not accept (transmit queue still full after 100 ms) are counted as dropped, omitted from
the recording, and the command exits with status 3.

The J1939 profile includes a **probe** message (PGN 0xFF00) carrying a 32-bit sequence number
once a second. Configure the pipeline to forward it like any other signal; it is what makes
end-to-end matching exact.

## 4. Record what arrives

On the bus (reference adapter):

```bash
railcan monitor -i can1 --allow-hardware --profile j1939 --out bus.log --events bus-events.jsonl
```

At the broker:

```bash
export RAILCAN_MQTT_PASSWORD=...     # never put the password on the command line
railcan mqtt-listen --host broker.example --tls --cafile ca.pem --cert client.pem --key client.key \
  --username bench --topic 'telemetry/#' --out received.jsonl
```

Payloads may be JSON or MessagePack, and each record gets a `received_at` time. Run the
listener on a host whose clock is synchronised (NTP or PTP) with the transmitting host;
otherwise latency is meaningless. RailCAN warns when it sees negative latencies.

## 5. Compare

```bash
# CAN in versus CAN on the bus (adapter, wiring, gateway CAN side).
railcan verify sent.log bus.log --profile j1939

# CAN in versus telemetry out (the whole black box).
railcan verify sent.log received.jsonl --telemetry --profile j1939 \
  --probe-field PROBE_SEQ --compare EngineOilPressure=OIL_P --compare EngineSpeed=ENG_RPM \
  --settle 30 --max-loss 0.1 --max-p95-ms 60000
```

* `--probe-field` / `--compare SIGNAL=FIELD` map profile signal names to the names the pipeline
  publishes. Signal lists in the forms `[{signalCode|name|signal|id, value}]`, `{name: value}`
  and flat objects are understood; batched payloads with a `samples`/`records`/`data` list are
  expanded.
* `--time-source arrival` measures latency to receipt at the listener; `sampled` uses the
  payload's own `timestamp`.
* `--settle` stops frames sent in the final seconds from counting as missing while they are
  still in flight or buffered.
* The report lists **missing blocks** (contiguous runs of lost probe samples, with start time
  and duration), duplicates, reordering and latency percentiles. `--json` gives the full report;
  `--max-loss` and `--max-p95-ms` turn it into a pass/fail check (exit status 2).

## 6. Scenarios worth running

| Test | How |
|---|---|
| Baseline soak | `normal` for 24 h; expect 100 % delivery and stable latency |
| Link outage and backfill | Disconnect the WAN for 5 min, 1 h, longer. Probe sequence numbers show whether buffered data is backfilled without gaps or duplicates |
| Edge restart with data buffered | Restart the edge computer during an outage; check for lost or repeated blocks |
| Diagnostics end to end | `engine_overheat`, `low_oil_pressure`, `multi_dtc` (DM1 over TP.BAM) |
| Stale or frozen data | `message_dropout`, `sensor_fault` with `--faults` targeting a J1939 message |
| Bus load | Custom profile with shorter periods; watch drops and the gateway's behaviour near the limit |
| Value fidelity | `--compare` on every forwarded signal; waveform sources (`sine(...)`, `ramp(...)`) make scaling errors obvious |

## Safety

Use an isolated bench bus. Never connect RailCAN's transmitter to an operational vehicle
network: physical interfaces and remote gateways need `--allow-hardware` for that reason.
`railcan monitor` and `mqtt-listen` do not originate data frames. A CAN interface in normal mode
may still acknowledge frames at the controller level; use listen-only mode when passive capture
is required and the adapter supports it.
