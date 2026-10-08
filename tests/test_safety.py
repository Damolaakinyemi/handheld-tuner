import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from tuner import cli
from tuner.check import (
    FAIL, PASS, WARN, CheckResult, evaluate_presentmon_output, evaluate_ryzenadj_info,
    format_report, missing_resolutions,
)
from tuner.controller import Controller
from tuner.log import COLUMNS, CsvLog
from tuner.model import LEGION_GO, Goal, Sample, Settings
from tuner.restore import RestoreState, RestoreStore
from tuner.runner import run
from tuner.sim import SimGame
from tuner.windows import limits_to_mw, ryzenadj_limit_args

DEV = LEGION_GO

INFO_OK = """\
| Name           | Value   | Parameter       |
|----------------|---------|-----------------|
| STAPM LIMIT    | 15.000  | stapm-limit     |
| STAPM VALUE    | 11.250  |                 |
| PPT LIMIT FAST | 20.000  | fast-limit      |
| PPT LIMIT SLOW | 15.000  | slow-limit      |
| THM VALUE CORE | 67.500  | thm-value-core  |
"""


class CountingSim(SimGame):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.failed_applies = []

    def apply(self, settings):
        actual = super().apply(settings)
        if actual != settings:
            self.failed_applies.append(settings)
        return actual


class DryRunTests(unittest.TestCase):
    def test_dry_run_changes_nothing_but_reports_what_it_would_do(self):
        goal = Goal(target_fps=60)
        controller = Controller(goal, DEV)
        start = controller.settings
        game = SimGame(DEV, start, scenes=((600, 1.0),))
        applied = []
        original_apply = game.apply
        game.apply = lambda s: applied.append(s) or original_apply(s)

        result = run(controller, game, 200, dry_run=True)

        self.assertEqual(applied, [])
        self.assertEqual(game.settings, start)
        self.assertEqual(controller.settings, start)
        proposals = [d for _, d in result.decisions if d.changed]
        self.assertTrue(proposals)
        self.assertTrue(all(d.reason.startswith("would change") for d in proposals))

    def test_log_has_a_row_per_second_and_survives_extras(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "log.csv"
            log = CsvLog(path, DEV)
            controller = Controller(Goal(), DEV)
            run(controller, SimGame(DEV, controller.settings), 30, log=log)
            log.close()
            with open(path) as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 30)
        self.assertEqual(list(rows[0].keys()), COLUMNS)
        self.assertEqual(rows[0]["res_w"], "1920")
        self.assertTrue(any(r["decision"] for r in rows))


class RefusedChangeTests(unittest.TestCase):
    def test_report_failure_blocks_the_dimension_and_resyncs(self):
        c = Controller(Goal(), DEV)
        wanted = replace(c.settings, res_index=1)
        c.report_failure(wanted, c.settings)
        self.assertIsNone(c._res(c.settings, -1))
        self.assertEqual(c.settings.res_index, 2)

    def test_controller_works_around_refused_resolution(self):
        # heavy scene needs lower resolution, but the "display" refuses every lower mode
        goal = Goal(target_fps=60)
        controller = Controller(goal, DEV)
        game = CountingSim(DEV, controller.settings, scenes=((600, 1.6),), reject_res={0, 1})
        result = run(controller, game, 600)
        self.assertEqual(len(game.failed_applies), 1, "should stop asking once refused")
        self.assertEqual(game.settings.res_index, 2)
        self.assertGreaterEqual(game.settings.tdp_w, 20)  # fell back to spending power instead

    def test_controller_stops_at_refused_tdp(self):
        controller = Controller(Goal(target_fps=60), DEV)
        game = CountingSim(DEV, controller.settings, scenes=((600, 1.3),), max_tdp=18)
        run(controller, game, 600)
        self.assertLessEqual(game.settings.tdp_w, 18)
        self.assertLessEqual(len(game.failed_applies), 1)

    def test_failed_starting_settings_are_reported(self):
        controller = Controller(Goal(), DEV)  # starts at 1920x1200
        game = SimGame(DEV, Settings(15, 0, 60), reject_res={2})  # device is really at 1280x800
        notes = []
        run(controller, game, 5, on_note=notes.append)
        self.assertEqual(len(notes), 1)
        self.assertEqual(controller.settings.res_index, 0)


class RestoreTests(unittest.TestCase):
    def test_roundtrip_clear_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            store = RestoreStore(Path(d) / "r.json")
            self.assertFalse(store.exists())
            self.assertIsNone(store.load())
            state = RestoreState(15000, 20000, 15000, 2560, 1600)
            store.save(state)
            self.assertTrue(store.exists())
            self.assertEqual(store.load(), state)
            store.clear()
            self.assertFalse(store.exists())
            store.path.write_text("not json")
            self.assertIsNone(store.load())

    def test_limit_helpers(self):
        from tuner.windows import parse_ryzenadj_info

        self.assertEqual(limits_to_mw(parse_ryzenadj_info(INFO_OK)), (15000, 20000, 15000))
        self.assertEqual(limits_to_mw({}), (None, None, None))
        self.assertEqual(ryzenadj_limit_args(15000, None, 15000),
                         ["--stapm-limit=15000", "--slow-limit=15000"])


class DeviceTests(unittest.TestCase):
    def test_snap_and_lookup(self):
        self.assertEqual(DEV.snap_tdp(15), 14)
        self.assertEqual(DEV.snap_tdp(5), 8)
        self.assertEqual(DEV.snap_tdp(99), 30)
        self.assertEqual(DEV.resolution_index(1920, 1200), 2)
        self.assertIsNone(DEV.resolution_index(1366, 768))


class CheckTests(unittest.TestCase):
    def test_ryzenadj_evaluation(self):
        self.assertEqual(evaluate_ryzenadj_info(0, INFO_OK).status, PASS)
        self.assertEqual(evaluate_ryzenadj_info(1, "").status, FAIL)
        self.assertEqual(evaluate_ryzenadj_info(0, "garbage").status, FAIL)
        partial = evaluate_ryzenadj_info(0, "| STAPM VALUE | 9.0 | x |\n")
        self.assertEqual(partial.status, WARN)
        self.assertIn("THM VALUE CORE", partial.detail)

    def test_presentmon_evaluation(self):
        header = "Application,MsBetweenPresents\n"
        good = [header] + ["game.exe,16.7\n"] * 50
        self.assertEqual(evaluate_presentmon_output(good).status, PASS)
        self.assertEqual(evaluate_presentmon_output([]).status, FAIL)
        self.assertEqual(evaluate_presentmon_output(["a,b\n", "1,2\n"]).status, FAIL)
        self.assertEqual(evaluate_presentmon_output([header] + ["game.exe,16.7\n"] * 3).status, WARN)

    def test_missing_resolutions_and_report(self):
        modes = [(2560, 1600, 144), (1920, 1200, 144), (1280, 800, 60)]
        self.assertEqual(missing_resolutions(DEV, modes), [(1600, 1000)])
        text = format_report([CheckResult("x", PASS, "fine")], {"raw": "abc"})
        self.assertIn("[PASS] x: fine", text)
        self.assertIn("--- raw ---", text)


class CliOffWindowsTests(unittest.TestCase):
    def test_commands_fail_cleanly_without_windows(self):
        import sys
        if sys.platform == "win32":
            self.skipTest("meant for non-Windows hosts")
        with tempfile.TemporaryDirectory() as d:
            report = str(Path(d) / "r.txt")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["check", "--report", report]), 1)
                self.assertEqual(cli.main(["run", "--game", "x.exe", "--dry-run"]), 1)
            self.assertIn("only works on Windows", Path(report).read_text())


if __name__ == "__main__":
    unittest.main()
