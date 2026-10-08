"""Runs the real WindowsBackend against a fake Windows, so its safety logic is tested on any machine."""
import tempfile
import unittest
from pathlib import Path

from tuner.model import LEGION_GO, Settings
from tuner.restore import RestoreState, RestoreStore
from tuner.windows import (
    WindowsBackend, limits_to_mw, restore_state, startup_problems, telemetry_problems,
)

DEV = LEGION_GO
HEADER = "Application,ProcessID,MsBetweenPresents,MsInPresentAPI\n"


class FakeProcess:
    def __init__(self):
        self.stdout = iter(())  # the tests feed frames in by hand
        self.terminated = False

    def terminate(self):
        self.terminated = True


class FakeSystem:
    """A pretend Windows machine: RyzenAdj, a display, a battery, and a clock the test controls."""

    def __init__(self):
        self.admin = True
        self.existing = {"ryzenadj.exe", "PresentMon.exe"}
        self.limits = {"stapm": 15000, "fast": 20000, "slow": 15000}  # mW
        self.power, self.temp = 11.25, 67.5
        self.show_limit = self.show_temp = True
        self.mode = (2560, 1600)
        self.supported = {(1280, 800), (1600, 1000), (1920, 1200), (2560, 1600)}
        self.pct = 80
        self.now = 0.0
        self.sleeps, self.commands, self.hooks = [], [], []
        self.write_exit = 0       # exit code of RyzenAdj write commands
        self.write_sticks = True  # False: exits 0 but the limit does not change
        self.write_lag = 0        # info reads before a write shows up
        self._pending = None
        self.process = None

    # -- the seam
    def which(self, name):
        return name if name in self.existing else None

    def is_admin(self):
        return self.admin

    def run(self, cmd, timeout=10):
        self.commands.append(list(cmd))
        if cmd[1:] == ["--info"]:
            return 0, self._info()
        if self.write_exit != 0:
            return self.write_exit, ""
        new = {}
        for arg in cmd[1:]:
            key, value = arg[2:].split("=")
            new[{"stapm-limit": "stapm", "fast-limit": "fast", "slow-limit": "slow"}[key]] = int(value)
        if self.write_sticks:
            self._pending = [new, self.write_lag]
            if self.write_lag == 0:
                self._settle()
        return 0, ""

    def start_presentmon(self, cmd):
        self.presentmon_cmd = cmd
        self.process = FakeProcess()
        return self.process

    def current_mode(self):
        return self.mode

    def set_resolution(self, w, h):
        if (w, h) in self.supported:
            self.mode = (w, h)
            return True
        return False

    def battery_percent(self):
        return self.pct

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps.append(seconds)

    def install_exit_hooks(self, cleanup):
        self.hooks.append(cleanup)

    # -- helpers
    def _settle(self):
        self.limits.update(self._pending[0])
        self._pending = None

    def _info(self):
        if self._pending:
            if self._pending[1] <= 0:
                self._settle()
            else:
                self._pending[1] -= 1
        rows = [("STAPM VALUE", self.power), ("PPT LIMIT FAST", self.limits["fast"] / 1000),
                ("PPT LIMIT SLOW", self.limits["slow"] / 1000)]
        if self.show_limit:
            rows.insert(0, ("STAPM LIMIT", self.limits["stapm"] / 1000))
        if self.show_temp:
            rows.append(("THM VALUE CORE", self.temp))
        return "| Name | Value | Parameter |\n|---|---|---|\n" + "".join(f"| {k} | {v:.3f} | x |\n" for k, v in rows)

    def writes(self):
        return [c for c in self.commands if c[1:] != ["--info"]]


class BackendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem()
        self.store = RestoreStore(Path(self.tmp.name) / "restore.json")
        self.notes = []

    def make(self, **kw):
        kw.setdefault("restore_store", self.store)
        return WindowsBackend(DEV, "game.exe", "PresentMon.exe", "ryzenadj.exe",
                              notify=self.notes.append, system=self.system, **kw)


class PreflightTests(BackendCase):
    def assertRefused(self, fragment, **kw):
        with self.assertRaises(RuntimeError) as ctx:
            self.make(**kw)
        self.assertIn(fragment, str(ctx.exception))
        self.assertIsNone(self.system.process, "PresentMon must not start when pre-flight fails")
        self.assertFalse(self.store.exists())
        self.assertEqual(self.system.writes(), [], "nothing may be changed when pre-flight fails")

    def test_needs_an_elevated_terminal(self):
        self.system.admin = False
        self.assertRefused("administrator")

    def test_needs_both_tools_on_disk(self):
        self.system.existing.discard("ryzenadj.exe")
        self.assertRefused("RyzenAdj")
        self.system.existing = {"ryzenadj.exe"}
        self.assertRefused("PresentMon")

    def test_every_problem_is_listed_at_once(self):
        self.system.admin = False
        self.system.existing = set()
        problems = startup_problems("ryzenadj.exe", "PresentMon.exe", self.system)
        self.assertEqual(len(problems), 3)

    def test_live_mode_refuses_when_blind_to_temperature(self):
        self.system.show_temp = False
        self.assertRefused("core temperature")
        self.system.show_temp = True
        self.system.temp = 0.0
        self.assertRefused("core temperature")

    def test_live_mode_refuses_when_the_tdp_limit_cannot_be_read(self):
        self.system.show_limit = False
        self.assertRefused("TDP limit")

    def test_dry_run_tolerates_missing_telemetry_and_never_writes(self):
        self.system.show_temp = False
        backend = self.make(read_only=True)
        self.assertFalse(self.store.exists())
        self.assertEqual(self.system.hooks, [])
        with self.assertRaises(RuntimeError):
            backend.apply(Settings(12, 1, 60))
        backend.close()
        self.assertEqual(self.system.writes(), [])
        self.assertTrue(self.system.process.terminated)

    def test_boost_ratio_is_bounded(self):
        for bad in (0.9, 1.5):
            with self.assertRaises(ValueError):
                self.make(boost_ratio=bad)

    def test_telemetry_problems_pure(self):
        good = {"STAPM LIMIT": 15.0, "STAPM VALUE": 9.0, "THM VALUE CORE": 60.0}
        self.assertEqual(telemetry_problems(good), [])
        self.assertEqual(len(telemetry_problems({})), 3)
        self.assertEqual(len(telemetry_problems({**good, "THM VALUE CORE": 0.0})), 1)
        self.assertEqual(telemetry_problems({"STAPM LIMIT": 15.0, "PPT VALUE SLOW": 9.0, "THM VALUE CORE": 60.0}), [])


class RestoreTests(BackendCase):
    def test_original_settings_are_saved_before_anything_changes(self):
        self.make()
        self.assertEqual(self.store.load(), RestoreState(15000, 20000, 15000, 2560, 1600))
        self.assertEqual(self.system.writes(), [])
        self.assertEqual(len(self.system.hooks), 1)

    def test_close_puts_everything_back_and_is_idempotent(self):
        backend = self.make()
        backend.apply(Settings(10, 0, 60))
        self.assertEqual((self.system.limits["stapm"], self.system.mode), (10000, (1280, 800)))

        backend.close()
        self.assertEqual(self.system.limits, {"stapm": 15000, "fast": 20000, "slow": 15000})
        self.assertEqual(self.system.mode, (2560, 1600))
        self.assertFalse(self.store.exists())
        self.assertTrue(self.system.process.terminated)

        commands = len(self.system.commands)
        backend.close()
        self.assertEqual(len(self.system.commands), commands)

    def test_the_exit_hook_restores_too(self):
        backend = self.make()
        backend.apply(Settings(12, 1, 60))
        self.system.hooks[0]()  # what Windows triggers when the console window is closed
        self.assertEqual(self.system.mode, (2560, 1600))
        self.assertEqual(self.system.limits["stapm"], 15000)

    def test_a_stale_restore_file_is_kept_not_overwritten(self):
        self.store.save(RestoreState(8000, 8000, 8000, 1280, 800))  # an earlier run died while tuned
        backend = self.make()
        self.assertEqual(self.store.load().stapm_mw, 8000)
        self.assertTrue(any("did not exit cleanly" in n for n in self.notes))
        backend.close()
        self.assertEqual(self.system.limits["stapm"], 8000, "restores the earlier originals")

    def test_failed_restore_keeps_the_file_and_says_so(self):
        backend = self.make()
        backend.apply(Settings(10, 0, 60))
        self.system.write_exit = 1
        self.system.supported = set()
        backend.close()
        self.assertTrue(self.store.exists())
        message = " ".join(self.notes)
        self.assertIn("TDP limits", message)
        self.assertIn("2560x1600", message)
        self.assertIn("python -m tuner restore", message)

    def test_restore_command_helper(self):
        self.system.limits = {"stapm": 8000, "fast": 8000, "slow": 8000}
        self.system.mode = (1280, 800)
        problems = restore_state("ryzenadj.exe", RestoreState(15000, 20000, 15000, 2560, 1600), self.system)
        self.assertEqual(problems, [])
        self.assertEqual(self.system.limits["fast"], 20000)
        self.assertEqual(self.system.mode, (2560, 1600))
        self.assertEqual(restore_state("ryzenadj.exe", RestoreState(), self.system), [])  # nothing known: no-op

    def test_limits_helper(self):
        self.assertEqual(limits_to_mw({"STAPM LIMIT": 15.0, "PPT LIMIT FAST": 20.0}), (15000, 20000, None))


class ApplyTests(BackendCase):
    def test_first_apply_sets_and_verifies_both_settings(self):
        backend = self.make()
        result = backend.apply(Settings(12, 1, 60))
        self.assertEqual(result, Settings(12, 1, 60))
        self.assertEqual(self.system.limits["stapm"], 12000)
        self.assertEqual(self.system.mode, (1600, 1000))

    def test_default_has_no_boost(self):
        self.make().apply(Settings(12, 1, 60))
        (write,) = self.system.writes()
        self.assertEqual(sorted(write[1:]), ["--fast-limit=12000", "--slow-limit=12000", "--stapm-limit=12000"])

    def test_boost_ratio_is_applied_to_the_fast_limit_only(self):
        self.make(boost_ratio=1.2).apply(Settings(12, 1, 60))
        (write,) = self.system.writes()
        self.assertIn("--fast-limit=14400", write)
        self.assertIn("--stapm-limit=12000", write)

    def test_first_apply_compares_the_exact_limit_not_the_snapped_one(self):
        backend = self.make()  # the device is at 15 W, which is between the 14 W and 16 W steps
        backend.apply(Settings(14, 3, 60))
        self.assertEqual(self.system.limits["stapm"], 14000, "15 W is not 14 W, so it must be written")

    def test_first_apply_within_tolerance_does_not_write(self):
        self.system.limits = {"stapm": 14200, "fast": 14200, "slow": 14200}
        backend = self.make()
        backend.apply(Settings(14, 3, 60))
        self.assertEqual(self.system.writes(), [])

    def test_unchanged_settings_cause_no_writes(self):
        backend = self.make()
        backend.apply(Settings(12, 1, 60))
        before = len(self.system.writes())
        backend.apply(Settings(12, 1, 60))
        backend.apply(Settings(12, 1, 60))
        self.assertEqual(len(self.system.writes()), before)

    def test_resolution_only_change_does_not_touch_the_power_limits(self):
        backend = self.make()
        backend.apply(Settings(12, 1, 60))
        before = len(self.system.writes())
        backend.apply(Settings(12, 2, 60))
        self.assertEqual(len(self.system.writes()), before)
        self.assertEqual(self.system.mode, (1920, 1200))

    def test_refused_power_write_reports_the_old_limit(self):
        backend = self.make()
        self.system.write_exit = 1
        result = backend.apply(Settings(12, 1, 60))
        self.assertEqual(result.tdp_w, 14, "15 W, the device's real limit, snapped to the grid")
        self.assertEqual(result.res_index, 1, "the resolution change still went through")

    def test_a_write_that_silently_does_nothing_is_caught_after_one_retry(self):
        backend = self.make()
        self.system.write_sticks = False
        result = backend.apply(Settings(12, 1, 60))
        self.assertEqual(result.tdp_w, 14)
        self.assertEqual(len(self.system.writes()), 2)

    def test_a_laggy_power_table_is_given_a_second_look(self):
        backend = self.make()
        self.system.write_lag = 1
        result = backend.apply(Settings(12, 1, 60))
        self.assertEqual(result.tdp_w, 12)
        self.assertEqual(len(self.system.writes()), 2)

    def test_refused_resolution_reports_the_real_one(self):
        backend = self.make()
        self.system.supported = {(2560, 1600)}
        result = backend.apply(Settings(12, 1, 60))
        self.assertEqual(result, Settings(12, 3, 60))

    def test_unknown_starting_mode_is_still_switched(self):
        self.system.mode = (1366, 768)
        self.system.supported.add((1366, 768))
        backend = self.make()
        result = backend.apply(Settings(12, 0, 60))  # nearest known mode to 1366x768 is 1280x800
        self.assertEqual(result.res_index, 0)
        result = backend.apply(Settings(12, 2, 60))
        self.assertEqual((result.res_index, self.system.mode), (2, (1920, 1200)))

    def test_unknown_starting_mode_and_a_refused_switch(self):
        self.system.mode = (1366, 768)
        self.system.supported = set()
        backend = self.make()
        result = backend.apply(Settings(12, 2, 60))
        self.assertEqual(result.res_index, 0, "reports the nearest known mode, not the one it wanted")

    def test_out_of_range_settings_never_reach_the_hardware(self):
        backend = self.make()
        for bad in (Settings(99, 1, 60), Settings(4, 1, 60), Settings(12, 9, 60), Settings(12, -1, 60)):
            with self.assertRaises(ValueError):
                backend.apply(bad)
        self.assertEqual(self.system.writes(), [])

    def test_state_is_tracked_across_failures(self):
        backend = self.make()
        backend.apply(Settings(12, 1, 60))
        self.system.write_exit = 1
        self.assertEqual(backend.apply(Settings(14, 1, 60)).tdp_w, 12)
        self.system.write_exit = 0
        self.assertEqual(backend.apply(Settings(14, 1, 60)).tdp_w, 14)


class SampleTests(BackendCase):
    def feed(self, backend, *ms_values):
        backend._ingest(HEADER)
        for ms in ms_values:
            backend._ingest(f"game.exe,1,{ms},0.2\n")

    def test_paces_to_one_sample_per_second(self):
        backend = self.make()
        backend.sample()
        self.assertEqual(self.system.sleeps, [1.0])
        backend.sample()
        self.assertEqual(self.system.sleeps, [1.0, 1.0])

    def test_fps_power_temperature_and_battery(self):
        backend = self.make()
        self.feed(backend, *([16.667] * 59 + [40.0]))
        s = backend.sample()
        self.assertAlmostEqual(s.fps_avg, 1000 * 60 / (16.667 * 59 + 40.0), places=2)
        self.assertAlmostEqual(s.fps_low, 25.0)
        self.assertEqual((s.apu_power_w, s.temp_c), (11.25, 67.5))
        self.assertAlmostEqual(s.battery_wh, 49.2 * 0.8)

    def test_old_frames_age_out_and_silence_reads_as_zero_fps(self):
        backend = self.make()
        self.feed(backend, 16.667, 16.667)
        backend.sample()
        s = backend.sample()
        self.assertEqual(s.fps_avg, 0.0, "no frames in the last second: the controller will wait")

    def test_headroom_is_estimated_from_the_fps_cap(self):
        backend = self.make()
        backend.apply(Settings(12, 1, 60))
        self.feed(backend, *([1000 / 90] * 90))
        s = backend.sample()
        self.assertAlmostEqual(s.gpu_util, 60 / 90, places=2)

    def test_unreadable_temperature_is_reported_as_zero_in_dry_run(self):
        self.system.show_temp = False
        backend = self.make(read_only=True)
        self.assertEqual(backend.sample().temp_c, 0.0)

    def test_battery_keeps_its_last_known_value(self):
        backend = self.make()
        backend.sample()
        self.system.pct = None
        self.assertAlmostEqual(backend.sample().battery_wh, 49.2 * 0.8)

    def test_garbage_lines_are_ignored(self):
        backend = self.make()
        backend._ingest(HEADER)
        for line in ("game.exe,1,NA,0.2\n", "short\n", "game.exe,1,-5,0.2\n", "game.exe,1,16.7,0.2\n"):
            backend._ingest(line)
        self.assertEqual(len(backend._frames), 1)

    def test_current_settings_for_a_dry_run_start(self):
        self.system.mode = (1920, 1200)
        self.assertEqual(self.make(read_only=True).current_settings(), Settings(14, 2, 60))
        self.system.mode = (1366, 768)
        self.assertIsNone(self.make(read_only=True).current_settings())

    def test_presentmon_is_started_for_the_right_game(self):
        self.make()
        self.assertEqual(self.system.presentmon_cmd[:3], ["PresentMon.exe", "--process_name", "game.exe"])


if __name__ == "__main__":
    unittest.main()
