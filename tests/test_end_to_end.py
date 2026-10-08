"""The whole stack on a Mac: the real controller, runner, service and WindowsBackend, running against a
simulated Windows handheld whose power, heat, frames, display and battery respond to what is set."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from statistics import mean

from tests.test_windows_backend import HEADER, FakeSystem
from tuner.controller import Controller
from tuner.model import LEGION_GO, Goal, Settings
from tuner.profiles import ProfileStore
from tuner.restore import RestoreStore
from tuner.runner import run
from tuner.service import TunerServer, TunerService
from tuner.sim import SimGame
from tuner.windows import WindowsBackend, restore_state

DEV = LEGION_GO


class SimulatedMachine(FakeSystem):
    """FakeSystem plus a game. Time only moves when the backend sleeps, and each whole second
    played turns the current TDP and display mode into frames, power, heat and battery drain.
    No fps limiter exists on this path, so the game runs flat out, as it will on the Legion Go."""

    def __init__(self, scenes=((900, 1.0),), **sim_faults):
        super().__init__()
        self.game = SimGame(DEV, Settings(15, 3, 999), scenes=scenes, **sim_faults)
        self.backend = None
        self._owed = 0.0
        self.resolution_attempts = 0
        self.power_writes = 0
        self.fail_after = None
        self.seconds_played = 0

    def run(self, cmd, timeout=10):
        if cmd[1:] != ["--info"]:
            self.power_writes += 1
        return super().run(cmd, timeout)

    def set_resolution(self, w, h):
        if (w, h) != self.mode:
            self.resolution_attempts += 1
        return super().set_resolution(w, h)

    def sleep(self, seconds):
        super().sleep(seconds)
        self._owed += seconds
        while self._owed >= 1.0:
            self._owed -= 1.0
            self._play_second()

    def _play_second(self):
        if self.fail_after is not None and self.seconds_played >= self.fail_after:
            raise OSError("PresentMon went away")
        self.seconds_played += 1
        res = DEV.resolution_index(*self.mode)
        self.game.apply(Settings(self.limits["stapm"] // 1000, 0 if res is None else res, 999))
        s = self.game.sample()
        self.power = s.apu_power_w
        self.temp = s.temp_c if self.show_temp else 0.0
        self.pct = int(100 * s.battery_wh / DEV.battery_wh)
        frames = round(s.fps_avg)
        for _ in range(frames):
            self.backend._ingest(f"game.exe,1,{1000 / frames:.3f},0.2\n")
        time.sleep(0.0005)  # lets a service thread breathe


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RestoreStore(Path(self.tmp.name) / "restore.json")
        self.notes = []

    def machine_backend(self, machine, **kw):
        kw.setdefault("restore_store", self.store)
        backend = WindowsBackend(DEV, "game.exe", "PresentMon.exe", "ryzenadj.exe",
                                 notify=self.notes.append, system=machine, **kw)
        machine.backend = backend
        backend._ingest(HEADER)
        return backend

    def original(self, machine):
        return dict(machine.limits), machine.mode


class FullLoopTests(Case):
    def test_tunes_a_game_through_the_real_backend_and_restores_afterwards(self):
        machine = SimulatedMachine()
        before = self.original(machine)
        backend = self.machine_backend(machine)
        controller = Controller(Goal(60), DEV)
        result = run(controller, backend, 500)

        late = result.samples[-60:]
        self.assertGreaterEqual(mean(s.fps_avg for s in late), 60 * 0.97, "still hits the target")
        self.assertTrue(controller.converged)
        applied = controller.settings
        self.assertEqual(machine.limits["stapm"], applied.tdp_w * 1000, "the hardware really has the setting")
        self.assertEqual(machine.mode, DEV.resolutions[applied.res_index])
        self.assertNotEqual((machine.limits, machine.mode), before, "it actually changed something")

        backend.close()
        self.assertEqual(self.original(machine), before, "everything put back")
        self.assertFalse(self.store.exists())

    def test_headroom_is_found_without_an_fps_limiter(self):
        """Without a limiter the game runs flat out, so spare power shows only as fps above target."""
        machine = SimulatedMachine(scenes=((900, 0.5),))  # a light game
        backend = self.machine_backend(machine)
        controller = Controller(Goal(60), DEV)
        run(controller, backend, 500)
        start = Controller.default_settings(Goal(60), DEV)
        self.assertLess(machine.limits["stapm"], start.tdp_w * 1000, "trimmed power it did not need")

    def test_a_loading_screen_cannot_push_power_up(self):
        machine = SimulatedMachine(blackouts=[(0, 400)])
        backend = self.machine_backend(machine)
        before = self.original(machine)
        controller = Controller(Goal(60), DEV)
        result = run(controller, backend, 300)
        self.assertEqual(machine.power_writes, 1, "only the initial settings were applied")
        self.assertTrue(all(d.reason == "waiting for frames from the game" for _, d in result.decisions))
        backend.close()
        self.assertEqual(self.original(machine), before)

    def test_dry_run_changes_nothing_at_all(self):
        machine = SimulatedMachine()
        backend = self.machine_backend(machine, read_only=True)
        before = self.original(machine)
        controller = Controller(Goal(60), DEV, start=backend.current_settings())
        result = run(controller, backend, 200, dry_run=True)
        self.assertEqual(machine.power_writes, 0)
        self.assertEqual(machine.resolution_attempts, 0)
        self.assertEqual(self.original(machine), before)
        self.assertTrue(any(d.reason.startswith("would change") for _, d in result.decisions))
        self.assertFalse(self.store.exists())

    def test_refused_display_changes_are_not_retried_forever(self):
        machine = SimulatedMachine(scenes=((900, 1.6),))
        machine.supported = {(2560, 1600)}
        backend = self.machine_backend(machine)
        controller = Controller(Goal(60), DEV)
        run(controller, backend, 500)
        self.assertLessEqual(machine.resolution_attempts, 4, "gave up on the display after a few refusals")

    def test_refused_power_changes_are_not_retried_forever(self):
        machine = SimulatedMachine()
        backend = self.machine_backend(machine)
        machine.write_exit = 1
        controller = Controller(Goal(60), DEV)
        run(controller, backend, 500)
        self.assertLessEqual(machine.power_writes, 8, "stopped hammering RyzenAdj")
        self.assertEqual(machine.limits["stapm"], 15000)

    def test_silent_power_writes_are_caught(self):
        machine = SimulatedMachine()
        backend = self.machine_backend(machine)
        machine.write_sticks = False
        controller = Controller(Goal(60), DEV)
        run(controller, backend, 300)
        self.assertEqual(controller.settings.tdp_w, DEV.snap_tdp(15), "controller knows the TDP never moved")

    def test_hot_machine_backs_off_through_the_real_sensor_path(self):
        machine = SimulatedMachine(scenes=((900, 1.6),))
        backend = self.machine_backend(machine)
        controller = Controller(Goal(60, max_temp_c=60.0), DEV)
        result = run(controller, backend, 600)
        self.assertLessEqual(max(s.temp_c for s in result.samples[-120:]), 63.0)

    def test_battery_goal_is_respected_end_to_end(self):
        machine = SimulatedMachine(scenes=((900, 1.0),))
        backend = self.machine_backend(machine)
        goal = Goal(60, runtime_hours=3.0)
        controller = Controller(goal, DEV)
        result = run(controller, backend, 600)
        tail = result.samples[-60:]
        hours = tail[-1].battery_wh / (mean(s.apu_power_w for s in tail) + DEV.base_power_w)
        self.assertGreaterEqual(hours, 3.0 * 0.9)


class CrashSafetyTests(Case):
    def test_backend_dying_mid_run_leaves_settings_restorable(self):
        machine = SimulatedMachine()
        machine.fail_after = 150
        before = self.original(machine)
        backend = self.machine_backend(machine)
        controller = Controller(Goal(60), DEV)
        with self.assertRaises(OSError):
            try:
                run(controller, backend, 500)
            finally:
                backend.close()  # what the CLI and the service both do
        self.assertEqual(self.original(machine), before)

    def test_a_process_that_dies_without_cleanup_is_fixed_by_the_restore_command(self):
        machine = SimulatedMachine()
        before = self.original(machine)
        backend = self.machine_backend(machine)
        run(Controller(Goal(60), DEV), backend, 200)
        self.assertNotEqual(self.original(machine), before)
        self.assertTrue(self.store.exists(), "the restore point survives the crash")

        # the process is gone; `python -m tuner restore` runs later
        problems = restore_state("ryzenadj.exe", self.store.load(), machine)
        self.assertEqual(problems, [])
        self.assertEqual(self.original(machine), before)

    def test_the_next_run_after_a_crash_keeps_the_true_originals(self):
        machine = SimulatedMachine()
        before = self.original(machine)
        crashed = self.machine_backend(machine)
        run(Controller(Goal(60), DEV), crashed, 200)  # never closed: the "crash"
        tuned = self.original(machine)
        self.assertNotEqual(tuned, before)

        second_machine_view = self.machine_backend(machine)
        second_machine_view.close()
        self.assertEqual(self.original(machine), before, "restored the pre-crash originals, not the tuned ones")


class ServiceOnTheSimulatedMachineTests(Case):
    def test_service_session_tunes_and_stop_restores(self):
        machine = SimulatedMachine()
        before = self.original(machine)
        made = []

        def factory(game, dry_run):
            backend = self.machine_backend(machine, read_only=dry_run)
            made.append(backend)
            return backend

        service = TunerService(DEV, factory, ProfileStore(Path(self.tmp.name) / "profiles.json"))
        server = TunerServer(service, "127.0.0.1", 0, Path(self.tmp.name) / "token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (service.stop_session(), server.shutdown(), thread.join(5), server.close()))

        service.start_session("game.exe", Goal(60, 3.0))
        end = time.time() + 20
        while time.time() < end and not service.snapshot()["converged"]:
            time.sleep(0.02)
        snap = service.snapshot()
        self.assertTrue(snap["converged"], snap)
        self.assertGreaterEqual(snap["sample"]["fps"], 58)
        self.assertNotEqual(self.original(machine), before)

        service.stop_session()
        self.assertEqual(service.snapshot()["state"], "idle")
        self.assertEqual(self.original(machine), before, "stopping the session restored the machine")

    def test_a_sensor_failure_at_start_is_reported_not_run(self):
        machine = SimulatedMachine()
        machine.show_temp = False

        def factory(game, dry_run):
            return self.machine_backend(machine, read_only=dry_run)

        service = TunerService(DEV, factory, ProfileStore(Path(self.tmp.name) / "p.json"))
        with self.assertRaises(Exception) as ctx:
            service.start_session("game.exe", Goal(60))
        self.assertIn("core temperature", str(ctx.exception))
        self.assertEqual(service.snapshot()["state"], "error")
        self.assertEqual(machine.power_writes, 0)


if __name__ == "__main__":
    unittest.main()
