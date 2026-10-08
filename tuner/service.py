"""Background service: runs the tuner loop and exposes it over a local HTTP API (docs/API.md).

The API can change TDP and display resolution, so it is locked down: loopback only, a random
bearer token per start (written to a user-only file), Host header checks against DNS rebinding,
JSON-only POSTs, strict input validation, and no CORS headers so web pages cannot call it.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .controller import Controller, Decision
from .model import Device, Goal, Sample, Settings
from .profiles import ProfileStore
from .runner import run

API_VERSION = "1"
DEFAULT_TOKEN_PATH = Path.home() / ".handheld-tuner" / "token"
GAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. \-]{0,63}$")  # an exe name, never starts with '-'
GOAL_KEYS = {"fps", "hours", "prefer", "max_temp"}
MAX_BODY = 8192
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class ValidationError(ValueError):
    pass


class Conflict(RuntimeError):
    pass


class BackendError(RuntimeError):
    pass


# -- validation ------------------------------------------------------------

def _number(body: dict, key: str, lo: float, hi: float) -> float:
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValidationError(f"{key} must be a number between {lo:g} and {hi:g}")
    return v


def parse_goal(body: dict, base: Optional[Goal] = None) -> Goal:
    """Builds a Goal from request JSON. Fields missing from `body` fall back to `base`."""
    unknown = set(body) - GOAL_KEYS - {"game", "dry_run"}
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
    base = base or Goal()
    fps, hours, prefer, max_temp = base.target_fps, base.runtime_hours, base.prefer, base.max_temp_c
    if "fps" in body:
        fps = int(_number(body, "fps", 15, 240))
        if fps != body["fps"]:
            raise ValidationError("fps must be a whole number")
    if "hours" in body:
        hours = None if body["hours"] is None else float(_number(body, "hours", 0.1, 24))
    if "prefer" in body:
        if body["prefer"] not in ("quality", "battery"):
            raise ValidationError("prefer must be 'quality' or 'battery'")
        prefer = body["prefer"]
    if "max_temp" in body:
        max_temp = float(_number(body, "max_temp", 50, 100))
    return Goal(target_fps=fps, runtime_hours=hours, max_temp_c=max_temp, prefer=prefer)


def parse_game(body: dict) -> str:
    game = body.get("game")
    if not isinstance(game, str) or not GAME_RE.match(game):
        raise ValidationError("game must be an exe name such as 'eldenring.exe'")
    return game


def settings_dict(device: Device, s: Settings) -> dict:
    w, h = device.resolutions[s.res_index]
    return {"tdp_w": s.tdp_w, "res_index": s.res_index, "resolution": f"{w}x{h}", "fps_cap": s.fps_cap}


def goal_dict(g: Goal) -> dict:
    return {"fps": g.target_fps, "hours": g.runtime_hours, "prefer": g.prefer, "max_temp": g.max_temp_c}


# -- events ----------------------------------------------------------------

class EventBus:
    """Fan-out to SSE clients. A slow client loses its oldest events, never blocks the tuner."""

    def __init__(self) -> None:
        self._subs: set = set()
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


# -- service ---------------------------------------------------------------

class TunerService:
    """Owns at most one tuning session. `backend_factory(game, dry_run)` returns a Backend
    (with close(), and optionally current_settings())."""

    def __init__(self, device: Device, backend_factory: Callable[[str, bool], Any],
                 profiles: ProfileStore, history: int = 600) -> None:
        self.device = device
        self.bus = EventBus()
        self._factory = backend_factory
        self._profiles = profiles
        self._ctl = threading.RLock()  # serialises start/stop/goal changes
        self._lock = threading.RLock()  # guards the state below
        self._history: Deque[dict] = deque(maxlen=history)
        self._thread: Optional[threading.Thread] = None
        self._controller: Optional[Controller] = None
        self._state = "idle"  # idle | running | paused | error
        self._error: Optional[str] = None
        self._game: Optional[str] = None
        self._goal: Optional[Goal] = None
        self._dry_run = False
        self._paused = False
        self._stop = False
        self._gen = 0
        self._started = 0.0
        self._last_sample: Optional[dict] = None
        self._last_decision: Optional[dict] = None
        self._notes: Deque[str] = deque(maxlen=20)
        self._saved: Optional[Settings] = None

    # -- control

    def start_session(self, game: str, goal: Goal, dry_run: bool = False) -> None:
        with self._ctl:
            self.stop_session()
            try:
                backend = self._factory(game, dry_run)
            except Exception as e:  # the factory talks to hardware and external tools
                with self._lock:
                    self._state, self._error = "error", str(e)
                self._publish_state()
                raise BackendError(str(e)) from e

            start = None
            current = getattr(backend, "current_settings", None)
            if dry_run and current:
                start = current()
            elif not dry_run:
                start = self._profiles.load(game, goal, self.device)

            with self._lock:
                self._game, self._goal, self._dry_run = game, goal, dry_run
                self._paused, self._stop, self._error = False, False, None
                self._gen += 1
                self._started = time.time()
                self._history.clear()
                self._notes.clear()
                self._last_sample = self._last_decision = None
                self._saved = None
                self._state = "running"
                thread = threading.Thread(target=self._main, args=(backend, start), daemon=True)
                self._thread = thread
            thread.start()
            self._publish_state()

    def set_goal(self, goal: Goal) -> None:
        with self._ctl:
            with self._lock:
                if self._state not in ("running", "paused"):
                    raise Conflict("no session is running")
                self._goal = goal
                self._gen += 1  # makes the loop rebuild its controller from the settings in effect
                self._saved = None

    def pause(self) -> None:
        self._set_paused(True)

    def resume(self) -> None:
        self._set_paused(False)

    def _set_paused(self, paused: bool) -> None:
        with self._ctl, self._lock:
            if self._state not in ("running", "paused"):
                raise Conflict("no session is running")
            if self._dry_run:
                raise Conflict("dry-run sessions never change anything; start a live session instead")
            self._paused = paused
            self._state = "paused" if paused else "running"
        self._publish_state()

    def stop_session(self) -> None:
        with self._ctl:
            with self._lock:
                thread = self._thread
                self._stop = True
            if thread is not None:
                thread.join(timeout=15)
            with self._lock:
                self._thread = None

    # -- reading state

    def snapshot(self) -> dict:
        with self._lock:
            c = self._controller if self._state != "idle" else None
            recent = list(self._history)[-5:]
            projected = None
            if recent and self._state in ("running", "paused"):
                power = sum(e["power_w"] for e in recent) / len(recent)
                projected = round(recent[-1]["battery_wh"] / (power + self.device.base_power_w), 2)
            return {
                "api": API_VERSION,
                "state": self._state,
                "error": self._error,
                "game": self._game,
                "dry_run": self._dry_run,
                "paused": self._paused,
                "goal": goal_dict(self._goal) if self._goal else None,
                "settings": settings_dict(self.device, c.settings) if c else None,
                "converged": bool(c and c.converged),
                "sample": self._last_sample,
                "projected_hours": projected,
                "last_decision": self._last_decision,
                "notes": list(self._notes),
                "session_seconds": int(time.time() - self._started) if self._state != "idle" else 0,
                "device": {
                    "name": self.device.name,
                    "tdp_min_w": self.device.tdp_min_w,
                    "tdp_max_w": self.device.tdp_max_w,
                    "tdp_step_w": self.device.tdp_step_w,
                    "resolutions": [f"{w}x{h}" for w, h in self.device.resolutions],
                    "battery_wh": self.device.battery_wh,
                },
            }

    def history(self, n: int) -> List[dict]:
        with self._lock:
            return list(self._history)[-n:] if n > 0 else []

    def profiles(self) -> dict:
        return self._profiles.all()

    # -- session thread

    def _main(self, backend, carry: Optional[Settings]) -> None:
        try:
            while True:
                with self._lock:
                    if self._stop:
                        break
                    goal, gen = self._goal, self._gen
                controller = Controller(goal, self.device, start=carry)
                with self._lock:
                    self._controller = controller
                self._publish_state()
                run(
                    controller, backend, 10 ** 9,
                    on_decision=lambda t, d, c=controller: self._on_decision(c, d),
                    on_note=self._note,
                    dry_run=self._dry_run,
                    on_sample=self._on_sample,
                    should_stop=lambda g=gen: self._stop or self._gen != g,
                    paused=lambda: self._paused,
                    keep_samples=False,
                )
                carry = controller.settings
        except Exception as e:  # a backend dying mid-run must end the session cleanly
            with self._lock:
                self._state, self._error = "error", str(e)
        finally:
            try:
                backend.close()
            except Exception as e:
                self._note(f"cleanup problem: {e}")
            with self._lock:
                if self._state != "error":
                    self._state = "idle"
                self._controller = None if self._state == "idle" else self._controller
            self._publish_state()

    def _on_sample(self, t: int, s: Sample, in_effect: Settings, _decision: Optional[Decision]) -> None:
        event = {
            "type": "sample", "ts": round(time.time(), 2),
            "t": int(time.time() - self._started),
            "fps": round(s.fps_avg, 1), "fps_low": round(s.fps_low, 1),
            "gpu_util": round(s.gpu_util, 3), "power_w": round(s.apu_power_w, 2),
            "temp_c": round(s.temp_c, 1), "battery_wh": round(s.battery_wh, 3),
            "settings": settings_dict(self.device, in_effect),
        }
        with self._lock:
            self._history.append(event)
            self._last_sample = event
        self.bus.publish(event)

    def _on_decision(self, controller: Controller, d: Decision) -> None:
        applied = d.changed and not (self._dry_run or self._paused)
        event = {
            "type": "decision", "ts": round(time.time(), 2),
            "changed": d.changed, "applied": applied, "reason": d.reason,
            "settings": settings_dict(self.device, d.settings),
            "converged": controller.converged,
            "stats": {"fps": round(d.stats.fps_avg, 1), "fps_low": round(d.stats.fps_low, 1),
                      "gpu_util": round(d.stats.gpu_util, 3), "power_w": round(d.stats.power_w, 2),
                      "temp_c": round(d.stats.temp_c, 1)},
        }
        with self._lock:
            self._last_decision = event
        self.bus.publish(event)
        self._maybe_save_profile(controller)

    def _maybe_save_profile(self, controller: Controller) -> None:
        if self._dry_run or self._paused or not controller.converged or self._saved == controller.settings:
            return
        with self._lock:
            tail = list(self._history)[-60:]
            game, goal = self._game, self._goal
        if len(tail) < 30:
            return
        self._profiles.save(game, goal, controller.settings,
                            sum(e["fps"] for e in tail) / len(tail),
                            sum(e["power_w"] for e in tail) / len(tail))
        self._saved = controller.settings
        self._note(f"saved profile for {game}")

    def _note(self, message: str) -> None:
        with self._lock:
            self._notes.append(message)
        self.bus.publish({"type": "note", "ts": round(time.time(), 2), "message": message})

    def _publish_state(self) -> None:
        self.bus.publish({"type": "state", **self.snapshot()})


# -- HTTP ------------------------------------------------------------------

def _make_handler(service: TunerService, token: str, on_shutdown: Callable[[], None]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "handheld-tuner"

        def log_message(self, *_args) -> None:
            pass

        # -- plumbing

        def _send(self, status: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, status: int, message: str) -> None:
            self._send(status, {"error": message})

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name in LOOPBACK_HOSTS

        def _authed(self, query: dict, allow_query_token: bool) -> bool:
            supplied = ""
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                supplied = header[7:]
            elif allow_query_token and "token" in query:
                supplied = query["token"][0]
            return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())

        def _body(self) -> Optional[dict]:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._fail(415, "send Content-Type: application/json")
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._fail(413, f"body must be 0 to {MAX_BODY} bytes")
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except ValueError:
                self._fail(400, "body is not valid JSON")
                return None
            if not isinstance(body, dict):
                self._fail(400, "body must be a JSON object")
                return None
            return body

        def _dispatch(self, method: str) -> None:
            if not self._host_ok():
                return self._fail(403, "unexpected Host header")
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            if method == "GET" and path == "/v1/health":
                return self._send(200, {"ok": True, "api": API_VERSION})
            if not self._authed(query, allow_query_token=(path == "/v1/events")):
                return self._fail(401, "missing or wrong bearer token")
            try:
                self._route(method, path, query)
            except ValidationError as e:
                self._fail(400, str(e))
            except Conflict as e:
                self._fail(409, str(e))
            except BackendError as e:
                self._fail(500, f"could not start: {e}")
            except Exception:
                self._fail(500, "internal error")

        def _route(self, method: str, path: str, query: dict) -> None:
            if method == "GET":
                if path == "/v1/status":
                    return self._send(200, service.snapshot())
                if path == "/v1/history":
                    try:
                        n = max(0, min(int(query.get("n", ["120"])[0]), 600))
                    except ValueError:
                        raise ValidationError("n must be a whole number")
                    return self._send(200, {"samples": service.history(n)})
                if path == "/v1/profiles":
                    return self._send(200, {"profiles": service.profiles()})
                if path == "/v1/events":
                    return self._stream()
            elif method == "POST":
                if path in ("/v1/pause", "/v1/resume", "/v1/shutdown"):
                    if path == "/v1/pause":
                        service.pause()
                    elif path == "/v1/resume":
                        service.resume()
                    else:
                        self._send(200, {"ok": True})
                        return on_shutdown()
                    return self._send(200, service.snapshot())
                if path in ("/v1/session", "/v1/goal"):
                    body = self._body()
                    if body is None:
                        return
                    if path == "/v1/session":
                        game = parse_game(body)
                        dry = body.get("dry_run", False)
                        if not isinstance(dry, bool):
                            raise ValidationError("dry_run must be true or false")
                        service.start_session(game, parse_goal(body), dry)
                    else:
                        if not body or set(body) - GOAL_KEYS:
                            raise ValidationError(f"send at least one of: {', '.join(sorted(GOAL_KEYS))}")
                        base = service.snapshot()["goal"]
                        current = Goal(base["fps"], base["hours"], base["max_temp"], base["prefer"]) if base else None
                        service.set_goal(parse_goal(body, current))
                    return self._send(200, service.snapshot())
            elif method == "DELETE" and path == "/v1/session":
                service.stop_session()
                return self._send(200, service.snapshot())
            self._fail(404, "no such endpoint")

        def _stream(self) -> None:
            q = service.bus.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self._write_event({"type": "state", **service.snapshot()})
                idle = 0
                while not getattr(self.server, "stopping", False):
                    try:
                        self._write_event(q.get(timeout=1))
                        idle = 0
                    except queue.Empty:
                        idle += 1
                        if idle >= 15:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            idle = 0
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                service.bus.unsubscribe(q)

        def _write_event(self, event: dict) -> None:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

    return Handler


class TunerServer:
    def __init__(self, service: TunerService, host: str = "127.0.0.1", port: int = 8765,
                 token_path: Optional[Path] = None) -> None:
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("the API only binds to loopback addresses")
        self.service = service
        self.token = secrets.token_urlsafe(32)
        self.token_path = Path(token_path) if token_path else DEFAULT_TOKEN_PATH
        self._write_token()
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(service, self.token, self.shutdown))
        self.httpd.daemon_threads = True
        self.httpd.stopping = False

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.httpd.stopping = True
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def close(self) -> None:
        self.httpd.server_close()
        try:
            self.token_path.unlink()
        except OSError:
            pass

    def _write_token(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token)
