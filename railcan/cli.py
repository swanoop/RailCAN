"""Command-line entry point; simulation itself needs only Python's stdlib."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from .dashboard import serve
from .profile import Profile, default_profile, export_dbc
from .simulator import SCENARIOS, Fault, Simulator
from .traces import candump_line, inspect_trace, read_trace, write_trace
from .transport import SocketCANSink, paced


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="railcan", description="Synthetic train telemetry and Classical CAN traffic lab")
    root.add_argument("--version", action="version", version=f"RailCAN {__version__}")
    commands = root.add_subparsers(dest="command")
    ui = commands.add_parser("ui", help="Open the local live dashboard")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--profile", type=Path)
    simulate = commands.add_parser("simulate", help="Generate a finite CAN recording")
    simulate.add_argument("--scenario", choices=SCENARIOS, default="normal")
    simulate.add_argument("--duration", type=float, default=120, help="Simulated seconds, 0.01–86400")
    simulate.add_argument("--seed", type=int, default=42)
    simulate.add_argument("--profile", type=Path)
    simulate.add_argument("--faults", type=Path, help="JSON fault list; replaces the selected scenario's fault windows")
    simulate.add_argument("--out", type=Path)
    simulate.add_argument("--format", choices=["candump", "csv", "jsonl", "pcap"])
    simulate.add_argument("--epoch", type=float, default=0, help="PCAP timestamp origin in Unix seconds; default 0")
    simulate.add_argument("--realtime", action="store_true", help="Pace output in wall-clock time")
    simulate.add_argument("--rate", type=float, default=1, help="Wall-clock multiplier, 0.1–10; offline generation ignores pacing")
    simulate.add_argument("--socketcan", metavar="CHANNEL", help="Optional Linux output, e.g. vcan0")
    simulate.add_argument("--allow-hardware", action="store_true", help="Permit physical CAN output on an isolated test bench")
    replay = commands.add_parser("replay", help="Replay or convert a recorded Classical CAN trace")
    replay.add_argument("input", type=Path)
    replay.add_argument("--input-format", choices=["candump", "csv", "jsonl", "pcap"])
    replay.add_argument("--out", type=Path)
    replay.add_argument("--format", choices=["candump", "csv", "jsonl", "pcap"])
    replay.add_argument("--profile", type=Path)
    replay.add_argument("--socketcan", metavar="CHANNEL")
    replay.add_argument("--allow-hardware", action="store_true")
    replay.add_argument("--realtime", action="store_true")
    replay.add_argument("--rate", type=float, default=1)
    inspect = commands.add_parser("inspect", help="Summarise frame counts, gaps, counters and checksum errors")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--profile", type=Path)
    inspect.add_argument("--format", choices=["candump", "csv", "jsonl", "pcap"])
    inspect.add_argument("--json", action="store_true")
    dbc = commands.add_parser("dbc", help="Export the CAN signal definitions as a DBC")
    dbc.add_argument("--profile", type=Path)
    dbc.add_argument("--out", type=Path, default=Path("railcan.dbc"))
    profile = commands.add_parser("profile", help="Save an editable default train and message profile")
    profile.add_argument("--out", type=Path, default=Path("train-profile.json"))
    validate = commands.add_parser("validate", help="Check a custom profile before running it")
    validate.add_argument("input", type=Path)
    commands.add_parser("scenarios", help="List built-in journey and fault scenarios")
    return root


def load_profile(args) -> Profile:
    return Profile.load(args.profile) if getattr(args, "profile", None) else default_profile()


def emit(frames, args, profile: Profile) -> int:
    if args.out:
        return write_trace(frames, args.out, profile, args.format,
                           channel=args.socketcan or "vcan0", epoch=getattr(args, "epoch", 0))
    if args.format and args.format != "candump":
        raise ValueError("--out is required for CSV, JSONL and PCAP")
    count = 0
    for frame in frames:
        if not args.socketcan:
            sys.stdout.write(candump_line(frame))
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        command = args.command or "ui"
        if command == "ui":
            port = getattr(args, "port", 8765)
            if not 0 <= port <= 65535:
                raise ValueError("Port must be between 0 and 65535")
            serve(load_profile(args), port, not getattr(args, "no_browser", False))
        elif command == "simulate":
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
            if args.socketcan and profile.nominal_bus_load_percent * args.rate > 80:
                raise ValueError("Requested pacing would exceed 80% nominal bus load")
            with ExitStack() as stack:
                sink = stack.enter_context(SocketCANSink(args.socketcan, args.allow_hardware)) if args.socketcan else None
                frames = paced(sim.frames(args.duration), args.rate, args.realtime or sink is not None, sink)
                count = emit(frames, args, profile)
            print(f"Generated {count:,} frames · {args.duration:g}s · {args.scenario} · seed {args.seed}", file=sys.stderr)
        elif command == "replay":
            profile = load_profile(args)
            if args.out and args.input.resolve() == args.out.resolve():
                raise ValueError("Replay output must differ from the input file")
            # Validate the complete input before sending any frames to a bus.
            summary = inspect_trace(read_trace(args.input, args.input_format), profile)
            with ExitStack() as stack:
                sink = stack.enter_context(SocketCANSink(args.socketcan, args.allow_hardware)) if args.socketcan else None
                frames = paced(read_trace(args.input, args.input_format), args.rate, args.realtime or sink is not None, sink)
                count = emit(frames, args, profile)
            print(f"Replayed {count:,} frames · recorded span {summary['span_s']:g}s", file=sys.stderr)
        elif command == "inspect":
            summary = inspect_trace(read_trace(args.input, args.format), load_profile(args))
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                print(f"{summary['frames']:,} frames · {summary['span_s']:g}s recorded span")
                print(f"{'CAN ID':<10} {'MESSAGE':<20} {'FRAMES':>8} {'BAD XOR':>8} {'COUNTER':>8} {'MISSING*':>9}")
                for row in summary["messages"]:
                    print(f"{row['can_id']:<10} {row['message']:<20} {row['frames']:>8} {row['checksum_errors']:>8} "
                          f"{row['counter_anomalies']:>8} {row['missing_frames_estimate']:>9}")
                print("* Estimates internal gaps using the profile; leading/trailing missing frames are not inferred.")
        elif command == "dbc":
            args.out.write_text(export_dbc(load_profile(args)), encoding="utf-8")
            print(f"Saved DBC: {args.out}")
        elif command == "profile":
            default_profile().save(args.out)
            print(f"Saved editable profile: {args.out}")
        elif command == "validate":
            profile = Profile.load(args.input)
            print(f"Valid: {profile.name} · {len(profile.messages)} messages · {profile.frames_per_second:g} frames/s "
                  f"· {profile.nominal_bus_load_percent:.3f}% nominal bus load")
        elif command == "scenarios":
            for name, details in SCENARIOS.items():
                print(f"{name:<20} {details['description']}")
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"RailCAN: {exc}", file=sys.stderr)
        return 1
