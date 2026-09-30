"""Loopback-only, dependency-free dashboard and bounded live session."""

from __future__ import annotations

import itertools
import json
import secrets
import tempfile
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .profile import Profile, TICK_MS, default_profile, export_dbc, finite, integer
from .simulator import FAULT_KINDS, SCENARIOS, Fault, Simulator
from .traces import read_trace, write_trace


class Session:
    def __init__(self, profile: Profile | None = None):
        self.profile = profile or default_profile()
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.temp = tempfile.TemporaryDirectory(prefix="railcan-")
        self.capture = None
        self.closed = threading.Event()
        self.history = deque(maxlen=360)
        self.events = deque(maxlen=20)
        self.frames_log = deque(maxlen=80)
        self.latest = {}
        self.config = {"scenario": "normal", "seed": 42, "duration": 180, "rate": 1}
        self._reset(self.config)
        self.worker = threading.Thread(target=self._run, name="RailCAN-simulation", daemon=True)
        self.worker.start()

    def _config(self, supplied: dict) -> dict:
        if not isinstance(supplied, dict):
            raise ValueError("config must be an object")
        if set(supplied) - {"scenario", "seed", "duration", "rate"}:
            raise ValueError("Unknown config key")
        config = {**self.config, **supplied}
        if config["scenario"] not in SCENARIOS:
            raise ValueError("Unknown scenario")
        integer(config["seed"], "seed")
        if not 1 <= finite(config["duration"], "duration") <= 3600:
            raise ValueError("Duration must be between 1 and 3600 seconds")
        if not .1 <= finite(config["rate"], "rate") <= 10:
            raise ValueError("Time multiplier must be between 0.1 and 10")
        # Validate fault targets before resetting an existing capture.
        Simulator(self.profile, config["scenario"], config["seed"])
        return config

    def _reset(self, supplied: dict) -> None:
        config = self._config(supplied)
        if self.capture is not None:
            self.capture.close()
        self.sim = Simulator(self.profile, config["scenario"], config["seed"])
        self.config = config
        self.capture_path = Path(self.temp.name) / f"session-{secrets.token_hex(6)}.jsonl"
        self.capture = self.capture_path.open("w", encoding="utf-8")
        self.history.clear()
        self.frames_log.clear()
        self.latest.clear()
        self.events.clear()
        self.frame_count = 0
        self.checksum_errors = 0
        self.last_faults = ()
        self.error = None
        self.status = "ready"
        self.events.append({"time_s": 0, "text": "Session ready. Choose a scenario and start."})

    def control(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("Control request must be a JSON object")
        with self.lock:
            action = data.get("action")
            if action == "start":
                self._reset(data.get("config", {}))
                self.status = "running"
                self.events.append({"time_s": 0, "text": "Simulation started."})
            elif action == "pause":
                if self.status != "running":
                    raise ValueError("Only a running session can be paused")
                self.status = "paused"
            elif action == "resume":
                if self.status != "paused":
                    raise ValueError("Only a paused session can be resumed")
                self.status = "running"
            elif action == "reset":
                self._reset(data.get("config", {}))
            elif action == "rate":
                self.config = self._config({"rate": data.get("rate")})
            elif action == "fault":
                if self.status not in {"running", "paused"}:
                    raise ValueError("Start a session before injecting a fault")
                fault = Fault(data.get("kind"), self.sim.elapsed_ms / 1000,
                              data.get("duration", 10), data.get("message"), data.get("value"))
                if fault.duration_s > 3600:
                    raise ValueError("Fault duration cannot exceed 3600 seconds")
                self.sim.add_fault(fault)
                self.events.append({"time_s": self.sim.timestamp, "text": f"Scheduled {fault.kind} for {fault.duration_s:g}s."})
            elif action == "clear_faults":
                self.sim.clear_faults()
                self.events.append({"time_s": self.sim.timestamp, "text": "All fault windows cleared."})
            else:
                raise ValueError("Unknown control action")
            return self.state()

    def _run(self) -> None:
        deadline = time.monotonic()
        while not self.closed.is_set():
            with self.lock:
                if self.status != "running":
                    delay = .025
                    deadline = time.monotonic()
                elif self.sim.elapsed_ms >= round(self.config["duration"] * 1000):
                    self.status = "complete"
                    self.capture.flush()
                    self.events.append({"time_s": self.config["duration"], "text": "Capture complete. Export a recording or start again."})
                    delay = .025
                else:
                    try:
                        frames = self.sim.tick()
                    except (ValueError, KeyError, ArithmeticError) as exc:
                        self.status = "error"
                        self.error = str(exc)
                        self.events.append({"time_s": self.sim.timestamp, "text": "Simulation stopped: " + self.error})
                        continue
                    for frame in frames:
                        record = frame.record(self.profile)
                        self.capture.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
                        self.frame_count += 1
                        self.checksum_errors += int(record["checksum_valid"] is False)
                        self.latest[frame.can_id] = record
                        self.frames_log.append(record)
                    if (self.sim.elapsed_ms - TICK_MS) % 200 == 0:
                        reported_speed = None
                        for message in self.profile.messages:
                            record = self.latest.get(message.can_id)
                            if record:
                                for signal in message.signals:
                                    if signal.source == "speed_kph":
                                        reported_speed = record["signals"].get(signal.name)
                                        break
                        self.history.append({"time_s": self.sim.timestamp,
                                             "speed_kph": self.sim.model.telemetry["speed_kph"],
                                             "reported_speed_kph": reported_speed,
                                             "traction_pct": self.sim.model.telemetry["traction_pct"],
                                             "brake_pct": self.sim.model.telemetry["brake_pct"]})
                    faults = tuple(fault.kind for fault in self.sim.active_faults)
                    if faults != self.last_faults:
                        self.events.append({"time_s": self.sim.timestamp,
                                            "text": "Active: " + ", ".join(faults) if faults else "Fault windows ended."})
                        self.last_faults = faults
                    deadline += TICK_MS / 1000 / self.config["rate"]
                    if deadline < time.monotonic() - 1:
                        # Yield after a slow host; do not attempt an unlimited catch-up burst.
                        deadline = time.monotonic()
                    delay = max(0, deadline - time.monotonic())
            self.closed.wait(delay)

    def state(self) -> dict:
        with self.lock:
            return {**self.sim.snapshot(), "status": self.status, "error": self.error, "config": dict(self.config),
                    "frame_count": self.frame_count, "checksum_errors": self.checksum_errors,
                    "history": list(self.history), "events": list(self.events),
                    "latest": list(self.latest.values()), "frames": list(reversed(self.frames_log)),
                    "nominal_fps": self.profile.frames_per_second,
                    "nominal_bus_load_percent": round(self.profile.nominal_bus_load_percent, 3)}

    def schema(self) -> dict:
        compatible = {}
        for name, description in SCENARIOS.items():
            try:
                Simulator(self.profile, name)
            except ValueError:
                continue
            compatible[name] = description
        return {"version": __version__, "profile": self.profile.to_dict(),
                "scenarios": compatible, "faults": list(FAULT_KINDS)}

    def export(self, format: str) -> Path:
        if format not in {"log", "csv", "jsonl", "pcap", "dbc", "profile"}:
            raise ValueError("Unknown export format")
        path = Path(self.temp.name) / f"export-{secrets.token_hex(6)}.{format if format != 'profile' else 'json'}"
        if format == "dbc":
            path.write_text(export_dbc(self.profile), encoding="utf-8")
        elif format == "profile":
            self.profile.save(path)
        else:
            with self.lock:
                self.capture.flush()
                capture_path, count = self.capture_path, self.frame_count
            # Read only the flushed snapshot, even if simulation continues or resets.
            write_trace(itertools.islice(read_trace(capture_path), count), path, self.profile)
        return path

    def close(self) -> None:
        self.closed.set()
        self.worker.join(timeout=2)
        with self.lock:
            self.capture.close()
        self.temp.cleanup()


def create_server(session: Session, port: int = 8765) -> ThreadingHTTPServer:
    assets = Path(__file__).parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _headers(self, status: int, content_type: str, length: int, filename: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()

        def _json(self, status: int, data: dict):
            payload = json.dumps(data, allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def _host_ok(self) -> bool:
            actual_port = self.server.server_address[1]
            valid = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            if self.headers.get("Host") not in valid:
                self._json(403, {"error": "Use the local dashboard address printed by RailCAN"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in valid}:
                self._json(403, {"error": "Cross-origin requests are not allowed"})
                return False
            return True

        def _authorized(self) -> bool:
            values = parse_qs(urlsplit(self.path).query)
            supplied = self.headers.get("X-RailCAN-Token", values.get("token", [""])[0])
            if not secrets.compare_digest(supplied, session.token):
                self._json(403, {"error": "Dashboard token required"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            route = urlsplit(self.path).path
            if route in {"/", "/app.js", "/style.css"}:
                filename, content_type = {"/": ("index.html", "text/html; charset=utf-8"),
                                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                          "/style.css": ("style.css", "text/css; charset=utf-8")}[route]
                payload = (assets / filename).read_bytes()
                if route == "/":
                    payload = payload.replace(b"__RAILCAN_TOKEN__", session.token.encode())
                self._headers(200, content_type, len(payload))
                self.wfile.write(payload)
                return
            if not self._authorized():
                return
            if route == "/api/state":
                self._json(200, session.state())
            elif route == "/api/schema":
                self._json(200, session.schema())
            elif route == "/api/export":
                selected = parse_qs(urlsplit(self.path).query).get("format", ["log"])[0]
                try:
                    path = session.export(selected)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                try:
                    self._headers(200, "application/octet-stream", path.stat().st_size,
                                  f"railcan-capture.{path.suffix.lstrip('.')}")
                    with path.open("rb") as handle:
                        while chunk := handle.read(65536):
                            self.wfile.write(chunk)
                finally:
                    path.unlink(missing_ok=True)
            else:
                self._json(404, {"error": "Route not found"})

        def do_POST(self):
            if not self._host_ok() or not self._authorized():
                return
            if urlsplit(self.path).path != "/api/control":
                self._json(404, {"error": "Route not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 16384:
                    raise ValueError("JSON request must be 1–16384 bytes")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Content-Type must be application/json")
                data = json.loads(self.rfile.read(length))
                self._json(200, session.control(data))
            except (ValueError, TypeError, KeyError) as exc:
                self._json(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(profile: Profile | None = None, port: int = 8765, open_browser: bool = True) -> None:
    session = Session(profile)
    server = None
    try:
        server = create_server(session, port)
        address = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"RailCAN {__version__} · local simulation\nDashboard: {address}\nPress Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(address)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        print("\nRailCAN stopped.")
    finally:
        if server:
            server.server_close()
        session.close()
