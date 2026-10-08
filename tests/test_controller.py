import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from statistics import mean

from tuner.controller import Controller
from tuner.model import LEGION_GO, Goal, Sample, Settings
from tuner.profiles import ProfileStore
from tuner.runner import run
from tuner.sim import SimGame

DEV = LEGION_GO


def tuned(goal, seconds=420, scenes=((120, 1.0), (90, 1.45), (120, 0.8)), start=None, battery_wh=None):
    controller = Controller(goal, DEV, start=start)
    game = SimGame(DEV, controller.settings, scenes=scenes, battery_wh=battery_wh)
    return controller, run(controller, game, seconds)


def tail(result, n=60):
    return result.samples[-n:]


class ControllerTests(unittest.TestCase):
    def test_hits_fps_target_when_reachable(self):
        c, r = tuned(Goal(target_fps=60))
        self.assertGreaterEqual(mean(s.fps_avg for s in tail(r)), 60 * 0.97)

    def test_respects_battery_budget(self):
        goal = Goal(target_fps=60, runtime_hours=3.0)
        c, r = tuned(goal, scenes=((600, 1.0),), seconds=600)
        wh = r.samples[-1].battery_wh
        projected = wh / (mean(s.apu_power_w for s in tail(r)) + DEV.base_power_w)
        self.assertGreaterEqual(projected, 3.0 * 0.95)

    def test_battery_preference_uses_less_power_than_quality(self):
        light = ((600, 0.45),)  # enough headroom that quality mode can raise resolution
        quality = tuned(Goal(prefer="quality"), scenes=light, seconds=600)
        battery = tuned(Goal(prefer="battery"), scenes=light, seconds=600)
        self.assertGreater(quality[0].settings.res_index, battery[0].settings.res_index)
        quality, battery = quality[1], battery[1]
        self.assertLess(
            mean(s.apu_power_w for s in tail(battery)),
            mean(s.apu_power_w for s in tail(quality)),
        )

    def test_backs_off_when_hot(self):
        goal = Goal(target_fps=60, max_temp_c=60.0)
        c, r = tuned(goal, scenes=((600, 1.0),), seconds=600)
        self.assertLessEqual(max(s.temp_c for s in tail(r, 120)), 60.0 + 3.0)

    def test_converges_without_endless_oscillation(self):
        c, r = tuned(Goal(), scenes=((900, 1.0),), seconds=900)
        self.assertTrue(c.converged)
        late = [d for t, d in r.decisions if t > 450 and d.changed]
        self.assertEqual(late, [])

    def test_unreachable_target_degrades_gracefully(self):
        c, r = tuned(Goal(target_fps=240), scenes=((300, 1.0),), seconds=300)
        self.assertEqual(c.settings.res_index, 0)
        # the 85C limit stops it short of the 30W maximum
        self.assertGreaterEqual(c.settings.tdp_w, 23)
        self.assertLessEqual(max(s.temp_c for s in tail(r, 120)), 85.0 + 3.0)

    def test_default_start_is_on_tdp_grid(self):
        s = Controller.default_settings(Goal(), DEV)
        self.assertEqual((s.tdp_w - DEV.tdp_min_w) % DEV.tdp_step_w, 0)

    def test_failed_probe_is_reverted_and_banned(self):
        c = Controller(Goal(target_fps=60), DEV, window=3, settle=0)
        c.settings = Settings(14, 1, 60)
        busy = Sample(fps_avg=60, fps_low=57, gpu_util=0.5, apu_power_w=8, temp_c=50, battery_wh=40)
        missed = replace(busy, fps_avg=50, fps_low=45, gpu_util=1.0)

        decisions = [c.observe(busy) for _ in range(3)]
        probe = decisions[-1]
        self.assertTrue(probe.changed)
        probed_to = probe.settings

        decisions = [c.observe(missed) for _ in range(3)]
        revert = decisions[-1]
        self.assertTrue(revert.changed)
        self.assertEqual(revert.settings, Settings(14, 1, 60))

        # same headroom reading right after: the failed probe must not be retried
        for _ in range(3):
            d = c.observe(busy)
        self.assertNotEqual(d.settings, probed_to)

    def test_fps_cap_pinned_to_goal(self):
        c = Controller(Goal(target_fps=45), DEV, start=Settings(15, 2, 144))
        self.assertEqual(c.settings.fps_cap, 45)


class ProfileTests(unittest.TestCase):
    def test_roundtrip_and_warm_start(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(Path(d) / "p.json")
            goal = Goal(target_fps=60, prefer="battery")
            self.assertIsNone(store.load("Game.exe", goal, DEV))
            store.save("Game.exe", goal, Settings(11, 1, 60), 60.0, 9.5)
            loaded = store.load("game.exe", goal, DEV)
            self.assertEqual(loaded, Settings(11, 1, 60))
            self.assertIsNone(store.load("game.exe", Goal(target_fps=30), DEV))

    def test_rejects_out_of_range_profile(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(Path(d) / "p.json")
            goal = Goal()
            store.save("g", goal, Settings(99, 1, 60), 60, 10)
            self.assertIsNone(store.load("g", goal, DEV))

    def test_warm_start_converges_faster(self):
        goal = Goal(target_fps=60)
        scenes = ((600, 1.0),)
        cold, rc = tuned(goal, scenes=scenes, seconds=300)
        warm, rw = tuned(goal, scenes=scenes, seconds=300, start=cold.settings)
        self.assertLessEqual(
            sum(1 for _, d in rw.decisions if d.changed),
            sum(1 for _, d in rc.decisions if d.changed),
        )


if __name__ == "__main__":
    unittest.main()
