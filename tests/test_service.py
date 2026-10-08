import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tuner.controller import Controller
from tuner.model import LEGION_GO, Goal
from tuner.profiles import ProfileStore
from tuner.service import TunerServer, TunerService
from tuner.sim import PacedBackend, SimGame

DEV = LEGION_GO
TICK = 0.003


class FakeBackend(PacedBackend):
    def __init__(self, die_after=None):
        super().__init__(SimGame(DEV, Controller.default_settings(Goal(), DEV)), TICK)
        self.closed = False
        self.applies = 0
        self.die_after = die_after
        self.samples = 0

    def sample(self):
        self.samples += 1
        if self.die_after is not None and self.samples > self.die_after:
            raise OSError("PresentMon went away")
        return super().sample()

    def apply(self, settings):
        self.applies += 1
        return super().apply(settings)

    def close(self):
        self.closed = True


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backends = []
        self.factory_error = None
        self.die_after = None

        def factory(game, dry_run):
            if self.factory_error:
                raise RuntimeError(self.factory_error)
            b = FakeBackend(self.die_after)
            self.backends.append(b)
            return b

        tmp = Path(self.tmp.name)
        self.service = TunerService(DEV, factory, ProfileStore(tmp / "profiles.json"))
        self.server = TunerServer(self.service, "127.0.0.1", 0, tmp / "token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.service.stop_session()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.close()
        self.tmp.cleanup()

    # -- helpers

    def call(self, method, path, body=None, headers=None, token=True, raw=None, host=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {self.server.token}"
        if host is not None:
            h["Host"] = host
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=payload, headers=h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def start(self, **extra):
        body = {"game": "demo.exe", "fps": 60, "hours": 2.5, **extra}
        status, data = self.call("POST", "/v1/session", body)
        self.assertEqual(status, 200, data)
        return data

    def wait_for(self, predicate, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("timed out waiting for condition")

    def status(self):
        return self.call("GET", "/v1/status")[1]


class SecurityTests(ServiceCase):
    def test_health_is_open_everything_else_needs_the_token(self):
        self.assertEqual(self.call("GET", "/v1/health", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/v1/status", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/v1/status", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.call("GET", "/v1/status", headers={"Authorization": "Bearer é"})[0], 401)
        self.assertEqual(self.call("GET", "/v1/status", headers={"Authorization": "Basic abc"})[0], 401)
        self.assertEqual(self.call("GET", "/v1/status")[0], 200)

    def test_query_token_only_works_for_the_event_stream(self):
        status, _ = self.call("GET", f"/v1/status?token={self.server.token}", token=False)
        self.assertEqual(status, 401)

    def test_rejects_foreign_host_headers(self):
        for host in ("evil.com", "127.0.0.1.evil.com", "evil.com:80", ""):
            self.assertEqual(self.call("GET", "/v1/health", token=False, host=host)[0], 403, host)
        for host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}", "localhost"):
            self.assertEqual(self.call("GET", "/v1/health", token=False, host=host)[0], 200, host)

    def test_posts_must_be_json(self):
        self.assertEqual(self.call("POST", "/v1/session", raw=b"game=x", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call("POST", "/v1/session", raw=b"game=x",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 415)
        self.assertEqual(self.call("POST", "/v1/session", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/v1/session", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.call("POST", "/v1/session", raw=b"x" * 9000)[0], 413)

    def test_no_cors_headers_are_sent(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        resp.read()
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_only_binds_to_loopback(self):
        with self.assertRaises(ValueError):
            TunerServer(self.service, "0.0.0.0", 0, Path(self.tmp.name) / "t2")

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_token_file_is_private(self):
        mode = stat.S_IMODE(os.stat(self.server.token_path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.server.token_path.read_text(), self.server.token)

    def test_token_file_removed_on_close(self):
        path = self.server.token_path
        self.assertTrue(path.exists())
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.close()
        self.assertFalse(path.exists())


class ValidationTests(ServiceCase):
    def test_bad_input_is_rejected_with_400(self):
        bad = [
            {"game": "--evil"}, {"game": "..\\x.exe"}, {"game": "a/b.exe"}, {"game": ""}, {"game": 5}, {},
            {"game": "a.exe", "fps": 9999}, {"game": "a.exe", "fps": 60.5}, {"game": "a.exe", "fps": True},
            {"game": "a.exe", "fps": "60"}, {"game": "a.exe", "hours": -1}, {"game": "a.exe", "hours": 0},
            {"game": "a.exe", "prefer": "turbo"}, {"game": "a.exe", "max_temp": 500},
            {"game": "a.exe", "dry_run": "yes"}, {"game": "a.exe", "surprise": 1},
        ]
        for body in bad:
            status, data = self.call("POST", "/v1/session", body)
            self.assertEqual(status, 400, (body, data))
        self.assertEqual(self.status()["state"], "idle")
        self.assertEqual(self.backends, [])

    def test_goal_endpoint_validation(self):
        self.start()
        self.assertEqual(self.call("POST", "/v1/goal", {})[0], 400)
        self.assertEqual(self.call("POST", "/v1/goal", {"game": "other.exe"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/goal", {"fps": 5})[0], 400)

    def test_unknown_routes_and_history_param(self):
        self.assertEqual(self.call("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.call("GET", "/v1/history?n=abc")[0], 400)
        self.assertEqual(self.call("GET", "/v1/history?n=5")[0], 200)

    def test_actions_without_a_session_conflict(self):
        for path in ("/v1/pause", "/v1/resume"):
            self.assertEqual(self.call("POST", path)[0], 409)
        self.assertEqual(self.call("POST", "/v1/goal", {"fps": 40})[0], 409)
        self.assertEqual(self.call("DELETE", "/v1/session")[0], 200)  # stopping nothing is fine


class LifecycleTests(ServiceCase):
    def test_start_tune_change_goal_pause_resume_stop(self):
        self.start()
        snap = self.wait_for(lambda: (s := self.status())["sample"] and s)
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["game"], "demo.exe")
        self.assertEqual(snap["device"]["name"], "Lenovo Legion Go")

        # it actually tunes: settings leave the default and the loop converges
        default = Controller.default_settings(Goal(), DEV)
        self.wait_for(lambda: self.status()["converged"])
        self.assertNotEqual(self.status()["settings"]["res_index"] + self.status()["settings"]["tdp_w"],
                            default.res_index + default.tdp_w)
        self.assertGreater(self.status()["projected_hours"], 0)

        status, snap = self.call("POST", "/v1/goal", {"fps": 40})
        self.assertEqual(status, 200)
        self.assertEqual(snap["goal"]["fps"], 40)
        self.assertEqual(snap["goal"]["hours"], 2.5)  # untouched fields are kept
        self.wait_for(lambda: self.status()["settings"]["fps_cap"] == 40)

        status, snap = self.call("POST", "/v1/pause")
        self.assertEqual((snap["state"], snap["paused"]), ("paused", True))
        before = self.backends[0].applies
        seen = self.status()["sample"]["ts"]
        self.wait_for(lambda: self.status()["sample"]["ts"] > seen + 0.2)
        self.assertEqual(self.backends[0].applies, before, "paused sessions must not apply changes")

        self.assertEqual(self.call("POST", "/v1/resume")[1]["state"], "running")
        status, snap = self.call("DELETE", "/v1/session")
        self.assertEqual(snap["state"], "idle")
        self.assertTrue(self.backends[0].closed, "stopping must close the backend so settings are restored")

    def test_starting_a_new_session_replaces_the_old_one(self):
        self.start()
        self.wait_for(lambda: self.status()["sample"])
        self.start(game="other.exe")
        self.assertTrue(self.backends[0].closed)
        self.assertEqual(self.status()["game"], "other.exe")
        self.assertFalse(self.backends[1].closed)

    def test_dry_run_never_applies_anything(self):
        self.start(dry_run=True)
        snap = self.wait_for(lambda: (s := self.status())["last_decision"] and s)
        self.assertTrue(snap["dry_run"])
        self.assertFalse(snap["last_decision"]["applied"])
        self.assertEqual(self.backends[0].applies, 0)
        self.assertEqual(self.call("POST", "/v1/pause")[0], 409)
        self.assertEqual(self.call("POST", "/v1/resume")[0], 409)

    def test_profile_saved_once_converged_and_listed(self):
        self.start()
        self.wait_for(lambda: self.call("GET", "/v1/profiles")[1]["profiles"], timeout=20)
        profiles = self.call("GET", "/v1/profiles")[1]["profiles"]
        self.assertIn("demo.exe|60fps|quality|2.5h", profiles)

    def test_history_returns_recent_samples(self):
        self.start()
        self.wait_for(lambda: len(self.call("GET", "/v1/history?n=20")[1]["samples"]) >= 5)
        samples = self.call("GET", "/v1/history?n=3")[1]["samples"]
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0]["type"], "sample")


class FailureTests(ServiceCase):
    def test_factory_failure_reports_error_and_recovers(self):
        self.factory_error = "this only runs on Windows"
        status, data = self.call("POST", "/v1/session", {"game": "a.exe"})
        self.assertEqual(status, 500)
        self.assertIn("Windows", data["error"])
        snap = self.status()
        self.assertEqual((snap["state"], snap["error"]), ("error", "this only runs on Windows"))
        self.assertEqual(self.call("POST", "/v1/goal", {"fps": 40})[0], 409)

        self.factory_error = None
        self.start()
        self.assertEqual(self.status()["state"], "running")
        self.assertIsNone(self.status()["error"])

    def test_backend_dying_mid_run_ends_session_cleanly(self):
        self.die_after = 30
        self.start()
        snap = self.wait_for(lambda: (s := self.status())["state"] == "error" and s)
        self.assertIn("PresentMon went away", snap["error"])
        self.assertTrue(self.backends[0].closed, "settings must be restored even when the backend dies")


class StreamTests(ServiceCase):
    def read_events(self, n, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", f"/v1/events?token={token or self.server.token}")
        resp = conn.getresponse()
        if resp.status != 200:
            return resp.status, []
        events, kind = [], None
        while len(events) < n:
            line = resp.fp.readline().decode().strip()
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                events.append((kind, json.loads(line[5:])))
        conn.close()
        return 200, events

    def test_stream_starts_with_state_then_samples_and_decisions(self):
        self.start()
        status, events = self.read_events(60)
        self.assertEqual(status, 200)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "state")
        self.assertIn("sample", kinds)
        self.assertIn("decision", kinds)
        sample = next(e for k, e in events if k == "sample")
        self.assertEqual(set(sample) >= {"fps", "power_w", "temp_c", "settings"}, True)

    def test_stream_requires_the_token(self):
        status, _ = self.read_events(1, token="wrong")
        self.assertEqual(status, 401)


class ShutdownTests(ServiceCase):
    def test_shutdown_endpoint_stops_the_server_and_session_can_be_restored(self):
        self.start()
        self.wait_for(lambda: self.status()["sample"])
        self.assertEqual(self.call("POST", "/v1/shutdown")[0], 200)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.service.stop_session()
        self.assertTrue(self.backends[0].closed)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.server.port), timeout=1).close()
            self.server.close()
            socket.create_connection(("127.0.0.1", self.server.port), timeout=1)


if __name__ == "__main__":
    unittest.main()
