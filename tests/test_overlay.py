import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from overlay.client import EventReader, parse_sse
from overlay.input import BACK, DEFAULT_CHORD, LEFT_THUMB, RIGHT_THUMB, HoldTrigger, chord_pressed
from overlay.model import CHIP_HIGHLIGHT_SECONDS, SPARK_POINTS, TOAST_SECONDS, OverlayModel, friendly
from tests.test_service import FakeBackend
from tuner.controller import Controller
from tuner.model import LEGION_GO, Goal
from tuner.profiles import ProfileStore
from tuner.runner import run
from tuner.service import TunerServer, TunerService
from tuner.sim import SimGame

DEVICE = {"base_power_w": 4.0, "battery_wh": 49.2}
GOAL = {"fps": 60, "hours": 2.5, "prefer": "quality", "max_temp": 85.0}


def state(st="running", **kw):
    return {"type": "state", "state": st, "error": kw.get("error"), "dry_run": kw.get("dry_run", False),
            "goal": kw.get("goal", GOAL), "device": DEVICE}


def sample(fps=60, power=8.0, tdp=8, res="1600x1000", wh=40.0, temp=51):
    return {"type": "sample", "fps": fps, "fps_low": fps * 0.9, "power_w": power, "temp_c": temp,
            "battery_wh": wh, "gpu_util": 0.8, "settings": {"tdp_w": tdp, "resolution": res}}


def decision(reason, changed=True, applied=True):
    return {"type": "decision", "reason": reason, "changed": changed, "applied": applied}


def model(*events, now=0.0, **kw):
    m = OverlayModel(**kw)
    m.apply({"type": "_online"}, now)
    for e in events:
        m.apply(e, now)
    return m


class ViewTests(unittest.TestCase):
    def test_offline_and_idle_are_dim_and_hideable(self):
        m = OverlayModel()
        v = m.view(0)
        self.assertEqual((v.visible, v.headline, v.tone), (True, "Tuner offline", "muted"))
        self.assertFalse(OverlayModel(quiet=True).view(0).visible)

        idle = model(state("idle"))
        self.assertEqual(idle.view(0).headline, "No game being tuned")
        self.assertFalse(model(state("idle"), quiet=True).view(0).visible)

    def test_running_headline_and_tone(self):
        self.assertEqual(model(state(), sample(60, 7.4)).view(0).headline, "60 fps · 7.4 W")
        self.assertEqual(model(state(), sample(60)).view(0).tone, "ok")
        self.assertEqual(model(state(), sample(55)).view(0).tone, "warn")
        self.assertEqual(model(state()).view(0).headline, "Starting…")

    def test_paused_dry_run_and_error(self):
        paused = model(state("paused"), sample()).view(0)
        self.assertEqual((paused.headline, paused.tone), ("Paused · 60 fps · 8.0 W", "muted"))
        self.assertTrue(model(state(dry_run=True), sample()).view(0).headline.startswith("Dry run · "))
        err = model(state("error", error="PresentMon went away"), quiet=True).view(0)
        self.assertEqual((err.visible, err.tone, err.toast), (True, "error", "PresentMon went away"))

    def test_unreachable_target_shows_warning_until_resolved(self):
        m = model(state(), sample(60), decision("target unreachable at 44fps, best effort", changed=False))
        self.assertEqual(m.view(0).tone, "warn")
        m.apply(decision("on target", changed=False), 1)
        self.assertEqual(m.view(1).tone, "ok")

    def test_going_offline_clears_stale_readings(self):
        m = model(state(), sample())
        m.apply({"type": "_offline", "reason": "refused"}, 1)
        self.assertEqual(m.view(1).headline, "Tuner offline")
        m.apply(state(), 2)  # service came back and reported a fresh state
        self.assertEqual(m.view(2).headline, "Starting…")

    def test_idle_state_resets_everything(self):
        m = model(state(), sample(), decision("trimming power"))
        m.apply(state("idle"), 1)
        self.assertEqual(m.view(1).headline, "No game being tuned")
        self.assertEqual(len(m.spark), 0)


class LastingConditionTests(unittest.TestCase):
    def test_loading_screen_says_so_instead_of_showing_zero_fps(self):
        m = model(state(), sample(fps=0, power=3.2), decision("waiting for frames from the game", changed=False))
        v = m.view(0)
        self.assertEqual((v.headline, v.tone), ("Waiting for the game…", "muted"))
        m.apply(sample(fps=60), 1)
        self.assertEqual(m.view(1).headline, "60 fps · 8.0 W")

    def test_unreadable_temperature_stays_visible_until_resolved(self):
        m = model(state(), sample(fps=44), decision("temperature unreadable, not raising power", changed=False))
        self.assertEqual(m.view(0).toast, "Can't read the temperature. Keeping power steady")
        self.assertEqual(m.view(60).toast, "Can't read the temperature. Keeping power steady", "not a 2.5 s toast")
        m.apply(decision("on target", changed=False), 61)
        self.assertIsNone(m.view(61).toast)

    def test_unreachable_target_stays_visible_and_a_change_clears_it(self):
        m = model(state(), sample(fps=44), decision("target unreachable at 44fps, best effort", changed=False))
        self.assertEqual(m.view(30).toast, "Can't reach the target. Doing its best")
        m.apply(decision("52fps below target, lowering resolution"), 31)
        self.assertEqual(m.view(31).toast, "Below target. Lowering resolution")
        self.assertIsNone(m.view(31 + TOAST_SECONDS + 1).toast)

    def test_a_new_toast_wins_over_a_lasting_condition(self):
        m = model(state(), sample(), decision("temperature unreadable, not raising power", changed=False))
        m.apply({"type": "note", "message": "saved profile for demo.exe"}, 5)
        self.assertEqual(m.view(5).toast, "Saved your settings for this game")
        self.assertEqual(m.view(5 + TOAST_SECONDS + 0.1).toast, "Can't read the temperature. Keeping power steady")

    def test_going_idle_clears_the_condition(self):
        m = model(state(), sample(), decision("temperature unreadable, not raising power", changed=False))
        m.apply(state("idle"), 1)
        self.assertIsNone(m.view(1).toast)


class ToastTests(unittest.TestCase):
    def test_applied_change_toasts_then_expires(self):
        m = model(state(), sample(), decision("52fps below target, lowering resolution"))
        self.assertEqual(m.view(0).toast, "Below target. Lowering resolution")
        self.assertEqual(m.view(TOAST_SECONDS - 0.1).toast, "Below target. Lowering resolution")
        self.assertIsNone(m.view(TOAST_SECONDS + 0.1).toast)

    def test_unapplied_change_says_would(self):
        m = model(state(dry_run=True), sample(),
                  decision("would change: 79% GPU busy, trimming power", applied=False))
        self.assertEqual(m.view(0).toast, "Would: Spare headroom. Trimming power")

    def test_refused_change_and_notes(self):
        m = model(state(), sample(), decision("could not apply (x), staying put", changed=False))
        self.assertEqual(m.view(0).toast, "Hardware refused a change")
        m.apply({"type": "note", "message": "saved profile for demo.exe"}, 5)
        self.assertEqual(m.view(5).toast, "Saved your settings for this game")

    def test_holds_do_not_toast(self):
        m = model(state(), sample(), decision("on target", changed=False))
        self.assertIsNone(m.view(0).toast)

    def test_every_controller_reason_has_friendly_wording(self):
        """Guards against the controller's messages drifting away from the overlay's wording."""
        dev = LEGION_GO
        reasons = set()
        runs = [
            (Goal(60, 2.5, 85.0, "quality"), ((120, 1.0), (90, 1.45), (120, 0.8)), {}),
            (Goal(40, 4.0, 85.0, "battery"), ((300, 1.0),), {}),
            (Goal(60, None, 60.0, "quality"), ((300, 1.0),), {}),   # runs hot
            (Goal(240, None, 85.0, "quality"), ((300, 1.0),), {}),  # unreachable
            (Goal(60, None, 85.0, "quality"), ((300, 1.6),), {"reject_res": {0, 1}}),  # refused changes
            (Goal(60, None, 85.0, "quality"), ((300, 1.0),), {"blackouts": [(0, 60)]}),  # no frames
            (Goal(60, None, 85.0, "quality"), ((300, 3.0),), {"hide_temp": True}),  # blind to heat
        ]
        for goal, scenes, faults in runs:
            c = Controller(goal, dev)
            result = run(c, SimGame(dev, c.settings, scenes=scenes, **faults), 400)
            reasons |= {d.reason for _, d in result.decisions}
        self.assertGreater(len(reasons), 8)
        for reason in sorted(reasons):
            self.assertNotEqual(friendly(reason), reason, f"no friendly wording for: {reason}")
            self.assertNotEqual(friendly("would change: " + reason), "would change: " + reason)
        self.assertIn("waiting for frames from the game", reasons)
        self.assertIn("temperature unreadable, not raising power", reasons)


class PanelTests(unittest.TestCase):
    def test_panel_contents(self):
        m = model(state(), *[sample(power=8.0, wh=40.0) for _ in range(6)], decision("77% GPU busy, trimming power"),
                  start_expanded=True)
        p = m.view(10).panel
        self.assertEqual((p.fps, p.status, p.tdp, p.res), ("60", "On target", "8 W", "1600×1000"))
        self.assertEqual(p.detail, "8.0 W · 51 °C · 3.3 h left")  # 40 Wh / (8 W + 4 W base)
        self.assertEqual(p.goal, "Goal 60 fps · 2.5 h")
        self.assertEqual(p.decision, "Spare headroom. Trimming power")
        self.assertIsNone(p.badge)

    def test_panel_needs_a_sample_and_labels_modes(self):
        self.assertIsNone(model(state(), start_expanded=True).view(0).panel)
        self.assertEqual(model(state("paused"), sample(), start_expanded=True).view(0).panel.badge, "PAUSED")
        self.assertEqual(model(state(dry_run=True), sample(), start_expanded=True).view(0).panel.badge, "DRY RUN")
        battery = dict(GOAL, prefer="battery")
        self.assertIn("save power", model(state(goal=battery), sample(), start_expanded=True).view(0).panel.goal)

    def test_changed_chip_highlights_briefly(self):
        m = model(state(), sample(tdp=10), start_expanded=True)
        self.assertFalse(m.view(0).panel.tdp_changed, "the first reading is not a change")
        m.apply(sample(tdp=8), 5.0)
        self.assertTrue(m.view(5.5).panel.tdp_changed)
        self.assertFalse(m.view(5.0 + CHIP_HIGHLIGHT_SECONDS + 0.1).panel.tdp_changed)
        m.apply(sample(tdp=8, res="1280x800"), 20.0)
        panel = m.view(20.5).panel
        self.assertEqual((panel.res_changed, panel.tdp_changed), (True, False))

    def test_toggle_and_spark_limit(self):
        m = model(state(), *[sample(power=float(i)) for i in range(50)])
        self.assertEqual(len(m.spark), SPARK_POINTS)
        self.assertIsNone(m.view(0).panel)
        m.toggle()
        self.assertIsNotNone(m.view(0).panel)
        m.toggle()
        self.assertFalse(m.view(0).expanded)


class InputTests(unittest.TestCase):
    def test_hold_trigger_fires_once_per_hold(self):
        t = HoldTrigger(hold=0.5)
        self.assertFalse(t.update(True, 0.0))
        self.assertFalse(t.update(True, 0.3))
        self.assertTrue(t.update(True, 0.6))
        self.assertFalse(t.update(True, 5.0), "keeps holding: no repeat")
        self.assertFalse(t.update(False, 5.1))
        self.assertFalse(t.update(True, 6.0))
        self.assertTrue(t.update(True, 6.5), "released and held again: fires again")

    def test_brief_taps_never_fire(self):
        t = HoldTrigger(hold=0.5)
        for start in (0.0, 1.0, 2.0):
            self.assertFalse(t.update(True, start))
            self.assertFalse(t.update(True, start + 0.2))
            self.assertFalse(t.update(False, start + 0.3))

    def test_chord_needs_every_button(self):
        self.assertTrue(chord_pressed(LEFT_THUMB | RIGHT_THUMB))
        self.assertTrue(chord_pressed(LEFT_THUMB | RIGHT_THUMB | BACK))
        self.assertFalse(chord_pressed(LEFT_THUMB))
        self.assertFalse(chord_pressed(RIGHT_THUMB | BACK))
        self.assertFalse(chord_pressed(0))
        self.assertEqual(DEFAULT_CHORD, LEFT_THUMB | RIGHT_THUMB)


class SseParsingTests(unittest.TestCase):
    def test_parses_events_and_ignores_noise(self):
        lines = [b": keepalive\n", b"\n",
                 b"event: sample\n", b'data: {"type": "sample", "fps": 60}\n', b"\n",
                 b'data: {"type": "state",\r\n', b'data:  "state": "idle"}\r\n', b"\r\n",
                 b"data: not json\n", b"\n",
                 b"data: [1, 2]\n", b"\n",
                 b'data: {"no_type": 1}\n', b"\n",
                 b'data: {"type": "note", "message": "caf\xc3\xa9"}\n', b"\n"]
        events = list(parse_sse(lines))
        self.assertEqual([e["type"] for e in events], ["sample", "state", "note"])
        self.assertEqual(events[1]["state"], "idle")
        self.assertEqual(events[2]["message"], "café")

    def test_event_without_trailing_blank_line_is_not_emitted(self):
        self.assertEqual(list(parse_sse([b'data: {"type": "sample"}\n'])), [])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / "token"
        self.out = queue.Queue()
        self.reader = None
        self.stacks = []

    def tearDown(self):
        if self.reader:
            self.reader.stop()
        for service, server, thread in self.stacks:
            service.stop_session()
            server.shutdown()
            thread.join(timeout=5)
            server.close()
        self.tmp.cleanup()

    def start_server(self, port=0):
        service = TunerService(LEGION_GO, lambda game, dry: FakeBackend(), ProfileStore(Path(self.tmp.name) / "p.json"))
        server = TunerServer(service, "127.0.0.1", port, self.token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.stacks.append((service, server, thread))
        return service, server, thread

    def start_reader(self, port):
        self.reader = EventReader(self.out, port, self.token, retry_start=0.05, retry_max=0.2)
        self.reader.start()

    def collect(self, until, timeout=10.0):
        seen, end = [], time.time() + timeout
        while time.time() < end:
            try:
                e = self.out.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(e)
            if until(e):
                return seen
        self.fail(f"timed out; saw {[e['type'] for e in seen][-8:]}")

    def test_streams_events_from_a_running_service(self):
        service, server, _ = self.start_server()
        service.start_session("demo.exe", Goal(60, 2.5))
        self.start_reader(server.port)
        seen = self.collect(lambda e: e["type"] == "decision")
        kinds = [e["type"] for e in seen]
        self.assertEqual(kinds[0], "_online")
        self.assertEqual(kinds[1], "state")
        self.assertIn("sample", kinds)

        m = OverlayModel()
        for e in seen:
            m.apply(e, 0.0)
        self.assertRegex(m.view(0).headline, r"^\d+ fps · \d+\.\d W$")

    def test_reports_offline_without_a_token_or_service(self):
        self.start_reader(1)  # nothing listens here and there is no token file
        e = self.collect(lambda e: e["type"] == "_offline")[-1]
        self.assertTrue(e["reason"])

    def test_reconnects_with_the_new_token_after_a_restart(self):
        service, server, thread = self.start_server()
        port = server.port
        self.start_reader(port)
        self.collect(lambda e: e["type"] == "state")
        old_token = server.token

        service.stop_session()
        server.shutdown()
        thread.join(timeout=5)
        server.close()
        self.collect(lambda e: e["type"] == "_offline")

        service2, server2, _ = self.start_server(port)
        self.assertNotEqual(server2.token, old_token)
        service2.start_session("demo.exe", Goal(60, None))
        seen = self.collect(lambda e: e["type"] == "sample")
        self.assertIn("_online", [e["type"] for e in seen])


if __name__ == "__main__":
    unittest.main()
