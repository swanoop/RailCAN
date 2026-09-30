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
