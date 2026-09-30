# Release validation · 0.1.0

Validated on Linux with Python 3.12.14. The source includes a Linux/Windows GitHub Actions matrix; those remote CI jobs have not run in a repository yet.

| Check | Result |
|---|---|
| Standard-library test suite | 46 tests passed |
| Signed signal encoding | Known byte-level vector verified |
| Deterministic generation | Same seed gives identical frames |
| Journey model | Door/traction interlock, speed bounds, nondecreasing distance and station cycles checked |
| Fault windows | Dropout, corruption, counter wrap/freeze, sensor recovery and emergency braking checked |
| Trace readers and writers | All four formats round-trip, including extended IDs and short/empty payloads |
| Independent DBC decoder | cantools 44.1.0 decoded 7,440 frames; all signals match |
| Independent PCAP decoder | Scapy 2.7.0 read 3,720 frames; IDs, bytes and timestamps match |
| Dashboard browser test | Chromium: start/pause/resume, signal inspection, visible sensor freeze, timed checksum corruption, reset and downloads passed |
| Browser-downloaded recordings | All four formats agree on the same 1,798-frame paused capture |
| Responsive layout | 390 px viewport checked; no horizontal page overflow |
| Browser errors | No JavaScript or console errors during the browser test |
| Local API | Token, Host/Origin checks, controls, invalid input and snapshot exports checked |
| Installable wheel | Built and installed in a clean virtual environment; entry point generated 62 frames in one simulated second |

Physical CAN hardware and a real `vcan` interface were not available for transmission tests. SocketCAN packet layout, pacing, interface opt-in and failure cleanup are covered by tests. Scapy's interface/route enumeration was disabled for the offline PCAP check because this environment blocks netlink access; packet decoding itself was unchanged.

The supplied screenshot is an actual running dashboard, paused during the frozen-speed scenario. Synthetic vehicle data and a custom demonstrator checksum are intentional. OEM compatibility, CANopen conformance and safety certification are not claimed.
