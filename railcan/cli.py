"""Command-line entry point; simulation itself needs only Python's stdlib."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from .profile import BUILTIN_PROFILES, Profile, export_dbc
from .profile import load_profile as resolve_profile
from .simulator import SCENARIOS, Fault, Simulator
from .traces import candump_line, inspect_trace, read_trace, write_trace

FORMATS = ["candump", "csv", "jsonl", "pcap"]
PROFILE_HELP = "Profile JSON file, or a built-in profile: " + ", ".join(BUILTIN_PROFILES)
INTERFACE_HELP = ("CAN interface: vcan0, socketcan:can0, pythoncan:<interface>:<channel> "
                  "(e.g. pythoncan:pcan:PCAN_USBBUS1), udp:<host>:<port>[:<local>], tcp:<host>:<port>")


def add_interface(command: argparse.ArgumentParser, transmit: bool = True) -> None:
    command.add_argument("--interface", "-i", metavar="SPEC", help=INTERFACE_HELP)
    if transmit:
        command.add_argument("--socketcan", metavar="CHANNEL", help="Same as --interface socketcan:CHANNEL")
    command.add_argument("--allow-hardware", action="store_true",
                         help="Permit a physical bus or remote gateway; use an isolated CAN test bench")
    command.add_argument("--bitrate", type=int, help="Adapter bitrate for pythoncan interfaces (default: profile bitrate)")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="railcan", description="Synthetic telemetry and Classical CAN traffic lab")
    root.add_argument("--version", action="version", version=f"RailCAN {__version__}")
    commands = root.add_subparsers(dest="command")

    ui = commands.add_parser("ui", help="Open the local live dashboard")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--profile", help=PROFILE_HELP)

    simulate = commands.add_parser("simulate", help="Generate a CAN recording, optionally transmitting it")
    simulate.add_argument("--scenario", choices=SCENARIOS, default="normal")
    simulate.add_argument("--duration", type=float, default=120, help="Simulated seconds, 0.01–86400")
    simulate.add_argument("--seed", type=int, default=42)
    simulate.add_argument("--profile", help=PROFILE_HELP)
    simulate.add_argument("--faults", type=Path, help="JSON fault list; replaces the selected scenario's fault windows")
    simulate.add_argument("--out", type=Path)
    simulate.add_argument("--format", choices=FORMATS)
    simulate.add_argument("--epoch", type=float, default=0, help="PCAP timestamp origin in Unix seconds; default 0")
    simulate.add_argument("--realtime", action="store_true", help="Pace output in wall-clock time")
    simulate.add_argument("--rate", type=float, default=1, help="Wall-clock multiplier, 0.1–10; offline generation ignores pacing")
    add_interface(simulate)

    replay = commands.add_parser("replay", help="Replay or convert a recorded Classical CAN trace")
    replay.add_argument("input", type=Path)
    replay.add_argument("--input-format", choices=FORMATS)
    replay.add_argument("--out", type=Path)
    replay.add_argument("--format", choices=FORMATS)
    replay.add_argument("--profile", help=PROFILE_HELP)
    replay.add_argument("--realtime", action="store_true")
    replay.add_argument("--rate", type=float, default=1)
    add_interface(replay)

    monitor = commands.add_parser("monitor", help="Receive, decode and record live CAN traffic without originating data frames")
    add_interface(monitor, transmit=False)
    monitor.add_argument("--profile", help=PROFILE_HELP)
    monitor.add_argument("--out", type=Path, help="Recording of received frames (.log, .csv, .jsonl, .pcap)")
    monitor.add_argument("--format", choices=FORMATS)
    monitor.add_argument("--events", type=Path, help="JSONL file for error frames and other bus events")
    monitor.add_argument("--duration", type=float, help="Stop after this many seconds (default: until Ctrl+C)")
    monitor.add_argument("--decode", action="store_true", help="Print every frame with decoded signals")
    monitor.add_argument("--quiet", action="store_true", help="No periodic status line")

    listen = commands.add_parser("mqtt-listen", help="Record telemetry published to an MQTT broker (JSON or MessagePack)")
    listen.add_argument("--host", required=True)
    listen.add_argument("--port", type=int)
    listen.add_argument("--topic", action="append", required=True, help="Topic filter; repeat for several")
    listen.add_argument("--qos", type=int, choices=[0, 1], default=1)
    listen.add_argument("--tls", action="store_true", help="Use TLS (port 8883 unless --port)")
    listen.add_argument("--cafile", help="CA bundle for the broker certificate")
    listen.add_argument("--cert", help="Client certificate (PEM) for mutual TLS")
    listen.add_argument("--key", help="Client private key (PEM) for mutual TLS")
    listen.add_argument("--username", help="User name; the password is read from RAILCAN_MQTT_PASSWORD")
    listen.add_argument("--client-id")
    listen.add_argument("--encoding", choices=["auto", "json", "msgpack"], default="auto")
    listen.add_argument("--out", type=Path, required=True, help="JSONL output: one record per message with receipt time")
    listen.add_argument("--duration", type=float, help="Stop after this many seconds (default: until Ctrl+C)")
    listen.add_argument("--count", type=int, help="Stop after this many messages")

    verify = commands.add_parser("verify", help="Compare a sent recording with what arrived (CAN or telemetry)")
    verify.add_argument("sent", type=Path, help="Recording made while transmitting")
    verify.add_argument("received", type=Path, help="CAN recording, or telemetry JSONL with --telemetry")
    verify.add_argument("--sent-format", choices=FORMATS)
    verify.add_argument("--received-format", choices=FORMATS)
    verify.add_argument("--telemetry", action="store_true", help="The received file holds decoded telemetry records")
    verify.add_argument("--profile", help=PROFILE_HELP)
    verify.add_argument("--probe-signal", default="ProbeSequence", help="Sequence signal in the profile (telemetry mode)")
    verify.add_argument("--probe-field", help="Name of the probe signal in the received telemetry, if different")
    verify.add_argument("--time-source", choices=["arrival", "sampled"], default="arrival",
                        help="Latency from receipt time (arrival) or the payload's own timestamp (sampled)")
    verify.add_argument("--compare", action="append", default=[], metavar="SIGNAL[=FIELD]",
                        help="Check a signal's values against the received field; repeat for several")
    verify.add_argument("--window", type=float, default=2.0, help="Time window for value checks, seconds")
    verify.add_argument("--settle", type=float, default=0.0,
                        help="Do not count frames sent in the last N seconds as missing (still in flight)")
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--max-loss", type=float, help="Exit 2 if the loss percentage exceeds this")
    verify.add_argument("--max-p95-ms", type=float, help="Exit 2 if p95 latency exceeds this many milliseconds")

    inspect = commands.add_parser("inspect", help="Summarise frame counts, gaps, counters, checksums and DM1 faults")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--profile", help=PROFILE_HELP)
    inspect.add_argument("--format", choices=FORMATS)
    inspect.add_argument("--json", action="store_true")

    dbc = commands.add_parser("dbc", help="Export the CAN signal definitions as a DBC")
    dbc.add_argument("--profile", help=PROFILE_HELP)
    dbc.add_argument("--out", type=Path, default=Path("railcan.dbc"))

    profile = commands.add_parser("profile", help="Save an editable profile")
    profile.add_argument("--template", choices=sorted(BUILTIN_PROFILES), default="emu")
    profile.add_argument("--out", type=Path, default=Path("train-profile.json"))

    validate = commands.add_parser("validate", help="Check a custom profile before running it")
    validate.add_argument("input", type=Path)
    commands.add_parser("scenarios", help="List built-in journey and fault scenarios")
    commands.add_parser("profiles", help="List built-in profiles")
    return root


def load_profile(args) -> Profile:
    return resolve_profile(getattr(args, "profile", None))


def interface_spec(args) -> str | None:
    spec = getattr(args, "interface", None)
    channel = getattr(args, "socketcan", None)
    if spec and channel:
        raise ValueError("Use either --interface or --socketcan, not both")
    return spec or (f"socketcan:{channel}" if channel else None)


def open_bus(args, profile: Profile):
    from .transport import bench_warnings, open_interface
    spec = interface_spec(args)
    if spec is None:
        return None
    for warning in bench_warnings(spec, profile.bitrate):
        print(f"RailCAN warning: {warning}", file=sys.stderr)
    return open_interface(spec, args.allow_hardware, args.bitrate or profile.bitrate)


def channel_label(spec: str | None) -> str:
    if not spec:
        return "vcan0"
    name = spec.split(":", 1)[-1] if spec.startswith("socketcan:") else spec
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in name)
    return safe[:32] or "can"


def emit(frames, args, profile: Profile, transmitting: bool) -> int:
    if args.out:
        return write_trace(frames, args.out, profile, args.format,
                           channel=channel_label(interface_spec(args)), epoch=getattr(args, "epoch", 0))
    if args.format and args.format != "candump":
        raise ValueError("--out is required for CSV, JSONL and PCAP")
    count = 0
    for frame in frames:
        if not transmitting:
            sys.stdout.write(candump_line(frame))
        count += 1
    return count


def parse_compare(items: list[str]) -> dict[str, str]:
    result = {}
    for item in items:
        signal, _, field = item.partition("=")
        if not signal:
            raise ValueError("--compare expects SIGNAL or SIGNAL=FIELD")
        result[signal] = field or signal
    return result


def run_monitor(args) -> None:
    from .j1939 import PGN_DM1, BAMReassembler, decode_dm1, pgn_of
    from .transport import BusEvent
    profile = load_profile(args)
    if not interface_spec(args):
        raise ValueError("monitor needs --interface")
    if args.duration is not None and not 0 < args.duration <= 7 * 86400:
        raise ValueError("Duration must be between 0 and 604800 seconds")
    events_file = args.events.open("w", encoding="utf-8") if args.events else None
    reassembler = BAMReassembler()
    counts = {"frames": 0, "events": 0}
    last_status = time.monotonic()
    started = time.monotonic()

    def report_event(event: BusEvent) -> None:
        counts["events"] += 1
        record = event.record()
        if events_file:
            events_file.write(json.dumps(record, separators=(",", ":")) + "\n")
            events_file.flush()
        classes = ", ".join(record.get("classes", [])) or record.get("event")
        print(f"[{record['timestamp']:.3f}] BUS EVENT {classes}"
              + (f" controller={','.join(record['controller'])}" if record.get("controller") else ""),
              file=sys.stderr)

    def frames():
        nonlocal last_status
        try:
            with open_bus(args, profile) as bus:
                print(f"Monitoring {bus.name} (receive only). Press Ctrl+C to stop.", file=sys.stderr)
                while args.duration is None or time.monotonic() - started < args.duration:
                    item = bus.recv(.25)
                    now = time.monotonic()
                    if not args.quiet and now - last_status >= 1:
                        last_status = now
                        print(f"\r{counts['frames']:,} frames · {counts['events']:,} bus events · "
                              f"{now - started:.0f}s", end="", file=sys.stderr, flush=True)
                    if item is None:
                        continue
                    if isinstance(item, BusEvent):
                        report_event(item)
                        continue
                    counts["frames"] += 1
                    if args.decode:
                        record = item.record(profile)
                        text = " ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                                        for k, v in record["signals"].items())
                        sys.stdout.write(candump_line(item, bus.name.replace(":", "_")[:32]).rstrip()
                                         + f"  {record['message']} {text}\n")
                    if item.extended:
                        payload = item.data if pgn_of(item.can_id) == PGN_DM1 and len(item.data) == 8 else None
                        done = None if payload else reassembler.feed(item.timestamp, item.can_id, item.data)
                        if done and done[0] == PGN_DM1:
                            payload = done[2]
                        if payload is not None:
                            dtcs = decode_dm1(payload)["dtcs"]
                            if dtcs and args.decode:
                                print("  DM1 " + ", ".join(f"SPN {d['spn']} FMI {d['fmi']} OC {d['oc']}" for d in dtcs))
                    yield item
        except KeyboardInterrupt:
            return

    try:
        if args.out:
            count = write_trace(frames(), args.out, profile, args.format, channel=channel_label(interface_spec(args)))
        else:
            count = sum(1 for _ in frames())
    finally:
        if events_file:
            events_file.close()
    print(f"\nReceived {count:,} frames · {counts['events']:,} bus events"
          + (f" · saved {args.out}" if args.out else ""), file=sys.stderr)


def run_listen(args) -> None:
    from .mqtt import MQTTClient, decode_payload, json_safe, password_from_environment
    if args.duration is not None and not 0 < args.duration <= 30 * 86400:
        raise ValueError("Duration must be positive and at most 30 days")
    if args.count is not None and args.count < 1:
        raise ValueError("--count must be at least 1")
    if (args.cert or args.key or args.cafile) and not args.tls:
        raise ValueError("--cafile, --cert and --key need --tls")
    client = MQTTClient(args.host, args.port, tls=args.tls, cafile=args.cafile, certfile=args.cert,
                        keyfile=args.key, username=args.username, password=password_from_environment(),
                        client_id=args.client_id)
    received = 0
    started = time.monotonic()
    try:
        for topic in args.topic:
            client.subscribe(topic, args.qos)
        print(f"Subscribed to {', '.join(args.topic)} on {args.host}. Press Ctrl+C to stop.", file=sys.stderr)
        with args.out.open("w", encoding="utf-8") as handle:
            while (args.duration is None or time.monotonic() - started < args.duration) and \
                    (args.count is None or received < args.count):
                try:
                    message = client.poll(.5)
                except KeyboardInterrupt:
                    break
                if message is None:
                    continue
                payload, encoding = decode_payload(message.payload, args.encoding)
                record = {"received_at": round(message.received_at, 6), "topic": message.topic,
                          "qos": message.qos, "retain": message.retain, "encoding": encoding,
                          "bytes": len(message.payload), "payload": json_safe(payload)}
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                handle.flush()
                received += 1
    finally:
        client.close()
    print(f"Recorded {received:,} messages to {args.out}", file=sys.stderr)


def run_verify(args) -> int:
    from .verify import (
        format_report,
        telemetry_samples,
        verify_frames,
        verify_telemetry,
    )
    profile = load_profile(args)
    if args.settle < 0 or args.window <= 0:
        raise ValueError("--settle must be nonnegative and --window positive")
    sent = read_trace(args.sent, args.sent_format)
    if args.telemetry:
        report = verify_telemetry(sent, profile, telemetry_samples(args.received),
                                  probe_signal=args.probe_signal, probe_field=args.probe_field,
                                  time_source=args.time_source, compare=parse_compare(args.compare),
                                  window_s=args.window, settle_s=args.settle)
    else:
        if args.compare or args.probe_field:
            raise ValueError("--compare and --probe-field apply to --telemetry comparisons")
        report = verify_frames(sent, read_trace(args.received, args.received_format), profile, args.settle)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    failed = []
    totals = report["totals"]
    counted = totals["sent"] - totals["excluded_in_flight"]
    if args.max_loss is not None and counted and totals["missing"] / counted * 100 > args.max_loss:
        failed.append(f"loss {totals['missing'] / counted * 100:.3f}% > {args.max_loss:g}%")
    if args.max_p95_ms is not None:
        latency = report.get("latency_s")
        if not latency:
            failed.append("no latency measurement for --max-p95-ms")
        elif latency["p95"] * 1000 > args.max_p95_ms:
            failed.append(f"p95 latency {latency['p95'] * 1000:.1f} ms > {args.max_p95_ms:g} ms")
    if failed:
        print("FAIL: " + "; ".join(failed), file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        command = args.command or "ui"
        if command == "ui":
            from .dashboard import serve
            port = getattr(args, "port", 8765)
            if not 0 <= port <= 65535:
                raise ValueError("Port must be between 0 and 65535")
            serve(load_profile(args), port, not getattr(args, "no_browser", False))
        elif command == "simulate":
            from .transport import paced
            profile = load_profile(args)
            faults = None
            if args.faults:
                items = json.loads(args.faults.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    raise ValueError("Fault file must contain a JSON list")
                faults = [Fault(**item) for item in items]
            sim = Simulator(profile, args.scenario, args.seed, faults)
            # Evaluate argument and profile errors before opening a CAN interface.
            if not .01 <= args.duration <= 86400:
                raise ValueError("Duration must be between 0.01 and 86400 seconds")
            if not .1 <= args.rate <= 10:
                raise ValueError("Rate must be between 0.1 and 10")
            transmitting = interface_spec(args) is not None
            if transmitting and profile.nominal_bus_load_percent * args.rate > 80:
                raise ValueError("Requested pacing would exceed 80% nominal bus load")
            with ExitStack() as stack:
                bus = stack.enter_context(open_bus(args, profile)) if transmitting else None
                frames = paced(sim.frames(args.duration), args.rate, args.realtime or bus is not None, bus)
                count = emit(frames, args, profile, transmitting)
            print(f"Generated {count:,} frames · {args.duration:g}s · {args.scenario} · seed {args.seed}"
                  + (f" · {bus.name}: {bus.stats.summary()}" if bus else ""), file=sys.stderr)
            if bus and bus.stats.dropped:
                return 3
        elif command == "replay":
            from .transport import paced
            profile = load_profile(args)
            if args.out and args.input.resolve() == args.out.resolve():
                raise ValueError("Replay output must differ from the input file")
            # Validate the complete input before sending any frames to a bus.
            summary = inspect_trace(read_trace(args.input, args.input_format), profile)
            transmitting = interface_spec(args) is not None
            with ExitStack() as stack:
                bus = stack.enter_context(open_bus(args, profile)) if transmitting else None
                frames = paced(read_trace(args.input, args.input_format), args.rate, args.realtime or bus is not None, bus)
                count = emit(frames, args, profile, transmitting)
            print(f"Replayed {count:,} frames · recorded span {summary['span_s']:g}s"
                  + (f" · {bus.name}: {bus.stats.summary()}" if bus else ""), file=sys.stderr)
            if bus and bus.stats.dropped:
                return 3
        elif command == "monitor":
            run_monitor(args)
        elif command == "mqtt-listen":
            run_listen(args)
        elif command == "verify":
            return run_verify(args)
        elif command == "inspect":
            summary = inspect_trace(read_trace(args.input, args.format), load_profile(args))
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                print(f"{summary['frames']:,} frames · {summary['span_s']:g}s recorded span")
                print(f"{'CAN ID':<10} {'MESSAGE':<20} {'FRAMES':>8} {'BAD XOR':>8} {'COUNTER':>8} {'MISSING*':>9}")
                for row in summary["messages"]:
                    print(f"{row['can_id']:<10} {row['message'][:20]:<20} {row['frames']:>8} {row['checksum_errors']:>8} "
                          f"{row['counter_anomalies']:>8} {row['missing_frames_estimate']:>9}")
                print("* Estimates internal gaps using the profile; leading/trailing missing frames are not inferred.")
                j1939 = summary.get("j1939")
                if j1939:
                    print(f"J1939 DM1: {j1939['dm1_broadcasts']:,} broadcasts · {j1939['bam_incomplete']} incomplete BAM transfers")
                    for dtc in j1939["dtcs"]:
                        print(f"  SPN {dtc['spn']} FMI {dtc['fmi']}: {dtc['broadcasts']} broadcasts, "
                              f"{dtc['first_s']:.2f}–{dtc['last_s']:.2f}s")
        elif command == "dbc":
            args.out.write_text(export_dbc(load_profile(args)), encoding="utf-8")
            print(f"Saved DBC: {args.out}")
        elif command == "profile":
            BUILTIN_PROFILES[args.template]().save(args.out)
            print(f"Saved editable {args.template} profile: {args.out}")
        elif command == "validate":
            profile = Profile.load(args.input)
            print(f"Valid: {profile.name} · {len(profile.messages)} messages · {profile.frames_per_second:g} frames/s "
                  f"· {profile.nominal_bus_load_percent:.3f}% nominal bus load")
        elif command == "scenarios":
            for name, details in SCENARIOS.items():
                print(f"{name:<20} {details['description']}")
        elif command == "profiles":
            for name, build in BUILTIN_PROFILES.items():
                profile = build()
                print(f"{name:<8} {profile.name:<20} {len(profile.messages)} messages · "
                      f"{profile.frames_per_second:g} frames/s · {profile.nominal_bus_load_percent:.2f}% at "
                      f"{profile.bitrate // 1000} kbit/s")
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Output was piped into a command that exited early (for example `| head`).
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0
    except (OSError, ValueError, TypeError, KeyError, ConnectionError) as exc:
        print(f"RailCAN: {exc}", file=sys.stderr)
        return 1
