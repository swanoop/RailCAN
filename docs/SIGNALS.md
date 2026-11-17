# Synthetic CAN signal layout

All default IDs are standard 11-bit IDs. Every message is eight bytes long. Signals use Intel/little-endian DBC bit numbering: bit 0 is the least significant bit of byte 0. Physical value = raw value × scale + offset; all default offsets are zero. Signed values use two's complement. Encoding rounds to the nearest representable value using Python's round-to-even rule and rejects out-of-range physical values.

## Shared trailer

| Field | Start bit | Length | Meaning |
|---|---:|---:|---|
| AliveCounter | 48 | 8 | Per-message counter, starts at 0, wraps at 255 |
| Checksum | 56 | 8 | XOR of the four little-endian CAN ID bytes and payload bytes 0–6 |

The checksum uses the arbitration ID only, without SocketCAN EFF/RTR/error flags. It is a simple application-level integrity demonstration, not the CAN wire CRC or a railway safety checksum. The adapter/kernel handles physical CAN framing and CRC.

## Motion · `0x100` · 50 ms

| Signal | Start | Bits | Scale | Signed | Unit |
|---|---:|---:|---:|---|---|
| SpeedKph | 0 | 16 | 0.01 | No | km/h |
| Acceleration | 16 | 16 | 0.001 | Yes | m/s² |
| OperatingMode | 32 | 8 | 1 | No | enumeration |
| EmergencyBrake | 40 | 1 | 1 | No | boolean |

Modes: 0 station, 1 accelerating, 2 cruising, 3 braking, 4 emergency, 5 depot cruise. Unused bits are zero.

## Remaining messages

| Message | Signal | Start | Bits | Scale | Signed | Unit |
|---|---|---:|---:|---:|---|---|
| Position | DistanceM | 0 | 32 | 0.1 | No | m |
| Position | TargetSpeed | 32 | 16 | 0.01 | No | km/h |
| Traction | MotorRPM | 0 | 16 | 1 | No | rpm |
| Traction | TractionDemand | 16 | 16 | 0.1 | No | % |
| Traction | MotorTemp | 32 | 16 | 0.1 | Yes | °C |
| Braking | BrakePipe | 0 | 16 | 0.001 | No | bar |
| Braking | BrakeCylinder | 16 | 16 | 0.001 | No | bar |
| Braking | BrakeDemand | 32 | 8 | 0.5 | No | % |
| Braking | Emergency | 40 | 1 | 1 | No | boolean |
| Braking | ParkingBrake | 41 | 1 | 1 | No | boolean |
| Braking | BrakeFault | 42 | 1 | 1 | No | boolean |
| Doors | LeftDoorMask | 0 | 8 | 1 | No | bitmask |
| Doors | RightDoorMask | 8 | 8 | 1 | No | bitmask |
| Doors | DoorsLocked | 16 | 1 | 1 | No | boolean |
| Doors | TractionInterlock | 17 | 1 | 1 | No | boolean |
| Doors | Obstruction | 18 | 1 | 1 | No | boolean |
| Doors | DoorFault | 19 | 1 | 1 | No | boolean |
| Doors | PassengerCount | 24 | 16 | 1 | No | people |
| Doors | StationIndex | 40 | 8 | 1 | No | zero-based index |
| Power | DCVoltage | 0 | 16 | 0.1 | No | V |
| Power | DCCurrent | 16 | 16 | 0.1 | Yes | A |
| Power | BatteryVoltage | 32 | 16 | 0.01 | No | V |
| HVAC | CabinTemp | 0 | 16 | 0.1 | Yes | °C |
| HVAC | AmbientTemp | 16 | 16 | 0.1 | Yes | °C |
| HVAC | HVACState | 32 | 8 | 1 | No | 0 idle, 1 heat, 2 cool, 3 fault |
| HVAC | Setpoint | 40 | 8 | 0.5 | No | °C |
| Diagnostics | Uptime | 0 | 32 | 0.1 | No | s |
| Diagnostics | FaultCode | 32 | 16 | 1 | No | enumeration |

Each door mask represents eight illustrative doors on one side of the consist: `0xFF` all open, `0x00` all closed. This is an aggregate demonstration, not per-car CAN addressing. The decorative three-car train in the UI is illustrative.

Fault codes: 0 none, 1 emergency brake, 2 obstruction, 3 HVAC fault, 4 low voltage, 5 frozen speed, 6 missing messages, 7 checksum corruption, 8 frozen counter, 9 speed spike. When several faults overlap, the lowest listed code takes priority. Faults are deliberately disclosed in Diagnostics to provide ground truth for testing.

## References and scope

Railway CAN deployments can use higher-layer protocols such as CANopen. IEC 61375-3-3 addresses CANopen consist networks; RailCAN's synthetic raw frames do not implement that specification. Actual interoperability requires the target system's protocol, object dictionary/message definitions, and validation.

Primary references:

- [Linux kernel SocketCAN documentation](https://www.kernel.org/doc/html/latest/networking/can.html)
- [IEC 61375-3-3:2012 description](https://webstore.iec.ch/en/publication/5404)
- [CiA international standardisation](https://www.can-cia.org/cia-groups/international-standardization)
- [SocketCAN PCAP link type](https://www.tcpdump.org/linktypes/LINKTYPE_CAN_SOCKETCAN.html)

## Message options

Every message in a profile can also set:

| Key | Default | Meaning |
|---|---|---|
| `trailer` | `counter_xor` | `counter_xor` reserves bytes 6–7 for AliveCounter and Checksum; `none` gives signals the whole payload |
| `dlc` | `8` | Payload length 1–8 (must be 8 with `counter_xor`) |
| `fill` | `0` | Byte value for bits no signal uses; J1939 uses `255` (0xFF, "not available") |
| `pgn` | — | J1939 parameter group. Give `pgn`, `priority` (default 6), `source_address` (default 0) and, for PDU1 PGNs, `destination` (default 255) instead of `can_id`; `extended`, `trailer: none` and `fill: 255` then default automatically |

```json
{"name": "MyPGN", "pgn": "0xFF10", "priority": 6, "source_address": 66, "period_ms": 100,
 "signals": [{"name": "Level", "source": "sine(0,100,20)", "start": 0, "length": 16, "scale": 0.01,
              "maximum": 100, "unit": "%"}]}
```

## Signal sources

A signal's `source` is a model value, a train parameter, a waveform or a sequence number:

| Source | Value |
|---|---|
| `sine(low,high,period_s)` | Smooth oscillation starting at `low` |
| `ramp(low,high,period_s)` | Sawtooth from `low` to `high`, repeating |
| `triangle(low,high,period_s)` | Up and down between the limits |
| `square(low,high,period_s)` | `low` for the first half of each period, then `high` |
| `step(low,high,at_s)` | `low` until `at_s`, then `high` |
| `random(low,high)` | Seeded uniform noise |
| `const(value)` | Fixed value |
| `seq` | Per-message transmission count (0, 1, 2, …; wraps at 2³²) for end-to-end tracing |

Validation rejects a waveform whose range leaves the signal's `[minimum, maximum]`.

Engine model sources: `engine_speed_rpm`, `engine_load_pct`, `coolant_temp_c`, `oil_pressure_kpa`,
`fuel_rate_lph`, `engine_hours_h`, `engine_running`. Engine load follows traction demand plus a
constant auxiliary load.

## J1939 profile (`--profile j1939`)

29-bit identifiers, source address 0, no counter/checksum trailer, unused bits 0xFF. Scalings
follow the public SAE J1939-71 definitions of these parameters.

| Message | PGN | ID | Cycle | Signals |
|---|---|---|---:|---|
| EEC1 | 0xF004 | `0CF00400` | 20 ms | ActualEnginePercentTorque (byte 2, 1 %/bit, −125 % offset), EngineSpeed (bytes 3–4, 0.125 rpm/bit) |
| EEC2 | 0xF003 | `0CF00300` | 50 ms | EnginePercentLoadAtCurrentSpeed (byte 2, 1 %/bit) |
| LFE1 | 0xFEF2 | `18FEF200` | 100 ms | EngineFuelRate (bytes 0–1, 0.05 L/h per bit) |
| EFL_P1 | 0xFEEF | `18FEEF00` | 500 ms | EngineOilPressure (byte 3, 4 kPa/bit) |
| ET1 | 0xFEEE | `18FEEE00` | 1000 ms | EngineCoolantTemperature (byte 0, 1 °C/bit, −40 °C offset) |
| HOURS | 0xFEE5 | `18FEE500` | 1000 ms | EngineTotalHoursOfOperation (bytes 0–3, 0.05 h/bit) |
| VEP1 | 0xFEF7 | `18FEF700` | 1000 ms | BatteryPotential (bytes 4–5, 0.05 V/bit) |
| Probe | 0xFF00 | `18FF0000` | 1000 ms | ProbeSequence (bytes 0–3, `seq`) — proprietary B, for tracing |
| DM1 | 0xFECA | `18FECA00` | 1000 ms | Lamp status and active DTCs |

On a real network HOURS is usually sent on request; it is broadcast here for convenience.

### DM1 and TP.BAM

The `dm1` block of a profile turns fault windows into active diagnostic trouble codes:

```json
"dm1": {"source_address": 0, "period_ms": 1000, "priority": 6,
        "fault_dtcs": {"coolant_overtemp": {"spn": 110, "fmi": 0},
                       "low_oil_pressure": {"spn": 100, "fmi": 1}}}
```

A `dtc` fault adds any SPN/FMI directly: `{"kind": "dtc", "start_s": 10, "duration_s": 30, "spn": 190, "fmi": 2}`.
With no active DTC, DM1 is `00 FF 00 00 00 00 FF FF`. With one, it is a single frame with the
amber warning lamp on. With two or more, the payload is sent as a broadcast announce message
(TP.CM BAM, `1CECFF00`) followed by data packets (TP.DT, `1CEBFF00`) 50 ms apart; a new DM1 is
not started until the previous transfer has finished. DTCs use SPN conversion method 0 and an
occurrence count of 1. `railcan inspect` reassembles BAM transfers and lists the DTCs it saw.
