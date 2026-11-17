"""End-to-end comparison of what was sent with what arrived somewhere else.

Two comparisons are supported:

* CAN to CAN: a sent recording against a recording made by another node, a gateway's
  CAN output or a capture downstream. Frames are matched by ID and payload.
* CAN to telemetry: a sent recording against decoded telemetry records (for example
  the JSONL written by ``railcan mqtt-listen``). A sequence-number probe signal gives an
  exact match for every sample, so loss, duplicates, reordering, missing blocks and
  latency can be measured; other signals can be checked for value fidelity.
"""

from __future__ import annotations

import bisect
import json
import math
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from pathlib import Path

from .frame import Frame
from .mqtt import parse_time, payload_timestamp
from .profile import Profile

WALL_CLOCK_THRESHOLD = 1e9  # timestamps above this are treated as Unix seconds


def percentiles(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(fraction):
        index = (len(ordered) - 1) * fraction
        low, high = math.floor(index), math.ceil(index)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)

    return {"min": round(ordered[0], 6), "mean": round(sum(ordered) / len(ordered), 6),
            "p50": round(pick(.5), 6), "p95": round(pick(.95), 6), "p99": round(pick(.99), 6),
            "max": round(ordered[-1], 6), "samples": len(ordered)}


def missing_blocks(missing: list[tuple[int, float]], step: int = 1) -> list[dict]:
    """Group missing (sequence, time) pairs into contiguous blocks."""
    blocks = []
    for seq, when in sorted(missing):
        if blocks and seq - blocks[-1]["to_seq"] == step:
            blocks[-1].update(to_seq=seq, to_time=when, count=blocks[-1]["count"] + 1)
        else:
            blocks.append({"from_seq": seq, "to_seq": seq, "from_time": when, "to_time": when, "count": 1})
    for block in blocks:
        block["duration_s"] = round(block["to_time"] - block["from_time"], 6)
        block["from_time"], block["to_time"] = round(block["from_time"], 6), round(block["to_time"], 6)
    return blocks


# ------------------------------------------------------------------ CAN vs CAN


def verify_frames(sent: Iterable[Frame], received: Iterable[Frame], profile: Profile | None = None,
                  settle_s: float = 0.0) -> dict:
    sent = list(sent)
    received = list(received)
    wall = bool(sent and received and sent[0].timestamp > WALL_CLOCK_THRESHOLD
                and received[0].timestamp > WALL_CLOCK_THRESHOLD)
    cutoff = (sent[-1].timestamp - settle_s) if sent else 0
    pending: dict[tuple, deque] = defaultdict(deque)
    for index, frame in enumerate(sent):
        pending[(frame.can_id, frame.extended, frame.data)].append(index)
    matched_keys = set()
    per_id: dict[tuple, dict] = {}

    def row(frame):
        key = (frame.can_id, frame.extended)
        if key not in per_id:
            name = "Unknown"
            if profile is not None:
                message = profile.lookup(frame.can_id, frame.extended)
                name = message.name if message is not None else frame.record(profile)["message"]
            per_id[key] = {"can_id": frame.hex_id, "message": name, "sent": 0, "received": 0, "matched": 0,
                           "missing": 0, "duplicates": 0, "unexpected": 0, "reordered": 0,
                           "_latency": [], "_last": -1, "_missing": []}
        return per_id[key]

    for frame in sent:
        row(frame)["sent"] += 1
    for frame in received:
        entry = row(frame)
        entry["received"] += 1
        key = (frame.can_id, frame.extended, frame.data)
        if pending[key]:
            index = pending[key].popleft()
            matched_keys.add(key)
            entry["matched"] += 1
            if index < entry["_last"]:
                entry["reordered"] += 1
            entry["_last"] = max(entry["_last"], index)
            if wall:
                entry["_latency"].append(frame.timestamp - sent[index].timestamp)
        elif key in matched_keys:
            entry["duplicates"] += 1
        else:
            entry["unexpected"] += 1
    in_flight = 0
    for key, indices in pending.items():
        for index in indices:
            frame = sent[index]
            if frame.timestamp > cutoff:
                in_flight += 1
                continue
            entry = row(frame)
            entry["missing"] += 1
            entry["_missing"].append(index)
    latency_all = []
    for entry in per_id.values():
        latency_all.extend(entry["_latency"])
        entry["latency_s"] = percentiles(entry.pop("_latency"))
        indices = sorted(entry.pop("_missing"))
        entry["missing_blocks"] = len(missing_blocks([(i, sent[i].timestamp) for i in indices])) if indices else 0
        entry.pop("_last")
    rows = [per_id[key] for key in sorted(per_id)]
    totals = {name: sum(r[name] for r in rows) for name in
              ("sent", "received", "matched", "missing", "duplicates", "unexpected", "reordered")}
    sent_counted = totals["sent"] - in_flight
    return {"mode": "can", "totals": {**totals, "excluded_in_flight": in_flight,
                                      "delivery_ratio": round(totals["matched"] / sent_counted, 6) if sent_counted else None},
            "latency_s": percentiles(latency_all),
            "latency_note": None if wall else "Latency needs Unix timestamps on both sides (record while transmitting).",
            "negative_latency": sum(1 for value in latency_all if value < 0),
            "messages": rows}


# ------------------------------------------------------------ CAN vs telemetry


def telemetry_samples(path: str | Path) -> Iterator[dict]:
    """Yield {arrival, sampled, signals} from JSONL telemetry records.

    Accepts the records written by ``railcan mqtt-listen`` ({received_at, payload}) or raw
    payload objects, one per line. A payload may hold one sample or a list of samples, and
    signals may be a list of {signalCode|name|signal|id, value} items, a mapping, or flat keys.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON on telemetry line {number}: {exc}") from exc
            arrival = parse_time(record.get("received_at")) if isinstance(record, dict) else None
            payload = record.get("payload", record) if isinstance(record, dict) and "received_at" in record else record
            for sample in _samples(payload):
                yield {"arrival": arrival, "sampled": payload_timestamp(sample) or payload_timestamp(payload),
                       "signals": extract_signals(sample), "line": number}


def _samples(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _samples(item)
    elif isinstance(payload, dict):
        for key in ("samples", "records", "data"):
            if isinstance(payload.get(key), list) and payload[key] and isinstance(payload[key][0], dict) \
                    and ("signals" in payload[key][0] or payload_timestamp(payload[key][0]) is not None):
                yield from _samples(payload[key])
                return
        yield payload


def extract_signals(sample) -> dict:
    if not isinstance(sample, dict):
        return {}
    signals = sample.get("signals")
    if isinstance(signals, list):
        result = {}
        for item in signals:
            if isinstance(item, dict):
                name = next((item[k] for k in ("signalCode", "name", "signal", "id", "code") if k in item), None)
                if name is not None and "value" in item:
                    result[str(name)] = item["value"]
        return result
    if isinstance(signals, dict):
        return {str(k): v for k, v in signals.items()}
    return {str(k): v for k, v in sample.items() if not isinstance(v, (dict, list))}


def _number(value):
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def verify_telemetry(sent: Iterable[Frame], profile: Profile, samples: Iterable[dict], *,
                     probe_signal: str = "ProbeSequence", probe_field: str | None = None,
                     time_source: str = "arrival", compare: dict[str, str] | None = None,
                     window_s: float = 2.0, settle_s: float = 0.0) -> dict:
    if time_source not in ("arrival", "sampled"):
        raise ValueError("time source must be arrival or sampled")
    probe_field = probe_field or probe_signal
    compare = compare or {}
    probe_message = next((m for m in profile.messages if any(s.name == probe_signal for s in m.signals)), None)
    if probe_message is None:
        raise ValueError(f"No message in profile {profile.name} carries signal {probe_signal}")
    compare_defs = {}
    for signal_name in compare:
        owner = next(((m, s) for m in profile.messages for s in m.signals if s.name == signal_name), None)
        if owner is None:
            raise ValueError(f"Unknown signal to compare: {signal_name}")
        compare_defs[signal_name] = owner
    sent_probe: dict[int, float] = {}
    series: dict[str, list[tuple[float, float]]] = {name: [] for name in compare}
    last_sent = None
    for frame in sent:
        last_sent = frame.timestamp
        message = profile.lookup(frame.can_id, frame.extended)
        if message is None or not frame.matches(message):
            continue
        decoded = None
        if message is probe_message:
            decoded = frame.decode(message)
            sent_probe.setdefault(int(decoded[probe_signal]), frame.timestamp)
        for name, (owner, _) in compare_defs.items():
            if owner is message:
                decoded = decoded or frame.decode(message)
                series[name].append((frame.timestamp, decoded[name]))
    if not sent_probe:
        raise ValueError(f"The sent recording has no {probe_message.name} frames; include the probe message")
    wall = next(iter(sent_probe.values())) > WALL_CLOCK_THRESHOLD
    cutoff = (last_sent or 0) - settle_s

    seen: set[int] = set()
    duplicates = unexpected = reordered = 0
    last_seq = None
    latency, no_time = [], 0
    fidelity = {name: {"field": field, "checked": 0, "matched": 0, "mismatched": 0, "examples": []}
                for name, field in compare.items()}
    sample_count = 0
    received_samples = []
    for sample in samples:
        sample_count += 1
        received_samples.append(sample)
        value = sample["signals"].get(probe_field)
        number = _number(value)
        if number is None:
            continue
        seq = int(number)
        if seq not in sent_probe:
            unexpected += 1
            continue
        if seq in seen:
            duplicates += 1
            continue
        seen.add(seq)
        if last_seq is not None and seq < last_seq:
            reordered += 1
        last_seq = seq if last_seq is None else max(last_seq, seq)
        when = sample[time_source]
        if when is None or not wall:
            no_time += 1
        else:
            latency.append(when - sent_probe[seq])
    # Offset to place received samples on the sending clock for value comparison.
    offset = percentiles(latency)["p50"] if latency else 0.0
    for sample in received_samples:
        for name, field in compare.items():
            if field not in sample["signals"]:
                continue
            value = _number(sample["signals"][field])
            stats = fidelity[name]
            stats["checked"] += 1
            sampled = sample["sampled"] if sample["sampled"] is not None else (
                sample["arrival"] - offset if sample["arrival"] is not None else None)
            points = series[name]
            if value is None or sampled is None or not points:
                stats["mismatched"] += 1
                if len(stats["examples"]) < 5:
                    stats["examples"].append({"line": sample["line"], "value": sample["signals"][field],
                                              "reason": "not numeric or no timestamp"})
                continue
            _, signal = compare_defs[name]
            tolerance = signal.scale / 2 + 1e-9
            times = [t for t, _ in points]
            low = bisect.bisect_left(times, sampled - window_s)
            high = bisect.bisect_right(times, sampled + window_s)
            if any(abs(points[i][1] - value) <= tolerance for i in range(low, high)):
                stats["matched"] += 1
            else:
                stats["mismatched"] += 1
                if len(stats["examples"]) < 5:
                    nearest = min(points, key=lambda p: abs(p[0] - sampled))
                    stats["examples"].append({"line": sample["line"], "received": value,
                                              "sent_nearest": nearest[1], "time_gap_s": round(nearest[0] - sampled, 3)})
    missing = [(seq, when) for seq, when in sent_probe.items() if seq not in seen and when <= cutoff]
    in_flight = sum(1 for seq, when in sent_probe.items() if seq not in seen and when > cutoff)
    counted = len(sent_probe) - in_flight
    blocks = missing_blocks(missing)
    return {"mode": "telemetry", "probe": {"message": probe_message.name, "signal": probe_signal,
                                           "field": probe_field, "period_s": probe_message.period_ms / 1000},
            "samples_read": sample_count,
            "totals": {"sent": len(sent_probe), "received": len(seen), "missing": len(missing),
                       "duplicates": duplicates, "unexpected": unexpected, "reordered": reordered,
                       "excluded_in_flight": in_flight,
                       "delivery_ratio": round(len(seen & {s for s, w in sent_probe.items() if w <= cutoff}) / counted, 6)
                       if counted else None},
            "missing_blocks": blocks,
            "longest_gap_s": max((b["duration_s"] + probe_message.period_ms / 1000 for b in blocks), default=0),
            "latency_s": percentiles(latency), "time_source": time_source,
            "latency_note": None if wall else "Latency needs a sent recording made while transmitting (Unix times).",
            "samples_without_time": no_time,
            "negative_latency": sum(1 for value in latency if value < 0),
            "fidelity": fidelity}


def format_report(report: dict) -> str:
    lines = []
    totals = report["totals"]
    ratio = totals.get("delivery_ratio")
    lines.append(f"Delivery: {totals['matched' if report['mode'] == 'can' else 'received']:,} of "
                 f"{totals['sent'] - totals['excluded_in_flight']:,} "
                 f"({ratio * 100:.3f}%)" if ratio is not None else "Delivery: nothing sent")
    lines.append(f"Missing {totals['missing']:,} · duplicates {totals['duplicates']:,} · "
                 f"reordered {totals['reordered']:,} · unexpected {totals['unexpected']:,}"
                 + (f" · {totals['excluded_in_flight']:,} still in flight (excluded)" if totals["excluded_in_flight"] else ""))
    latency = report.get("latency_s")
    if latency:
        lines.append(f"Latency ({report.get('time_source', 'arrival')}): p50 {latency['p50'] * 1000:.1f} ms · "
                     f"p95 {latency['p95'] * 1000:.1f} ms · p99 {latency['p99'] * 1000:.1f} ms · "
                     f"max {latency['max'] * 1000:.1f} ms · n={latency['samples']:,}")
    elif report.get("latency_note"):
        lines.append("Latency: " + report["latency_note"])
    if report.get("negative_latency"):
        lines.append(f"WARNING: {report['negative_latency']:,} negative latencies; the clocks are not synchronised.")
    if report["mode"] == "telemetry":
        blocks = report["missing_blocks"]
        lines.append(f"Missing blocks: {len(blocks)}" + (f" · longest gap {report['longest_gap_s']:g}s" if blocks else ""))
        for block in blocks[:10]:
            lines.append(f"  seq {block['from_seq']}–{block['to_seq']} ({block['count']} probes, "
                         f"{block['duration_s']:g}s from t={block['from_time']:.3f})")
        if len(blocks) > 10:
            lines.append(f"  … {len(blocks) - 10} more blocks (use --json)")
        for name, stats in report["fidelity"].items():
            lines.append(f"Values {name} → {stats['field']}: {stats['matched']:,}/{stats['checked']:,} match"
                         + (f", {stats['mismatched']:,} mismatched" if stats["mismatched"] else ""))
    else:
        lines.append(f"{'CAN ID':<10} {'MESSAGE':<16} {'SENT':>8} {'RECV':>8} {'MISSING':>8} {'BLOCKS':>7} "
                     f"{'DUP':>6} {'REORD':>6} {'P95 ms':>8}")
        for row in report["messages"]:
            p95 = f"{row['latency_s']['p95'] * 1000:.1f}" if row["latency_s"] else "—"
            lines.append(f"{row['can_id']:<10} {row['message'][:16]:<16} {row['sent']:>8} {row['received']:>8} "
                         f"{row['missing']:>8} {row['missing_blocks']:>7} {row['duplicates']:>6} "
                         f"{row['reordered']:>6} {p95:>8}")
    return "\n".join(lines)
