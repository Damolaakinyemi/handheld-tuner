import unittest
from statistics import mean

from tuner.controller import Controller
from tuner.model import LEGION_GO, Goal, Sample, Settings
from tuner.runner import run
from tuner.sim import SimGame

DEV = LEGION_GO


def tuned(goal, seconds, **faults):
    controller = Controller(goal, DEV)
    scenes = faults.pop("scenes", ((900, 1.0),))
    game = SimGame(DEV, controller.settings, scenes=scenes, **faults)
    return controller, game, run(controller, game, seconds)


class NoFramesTests(unittest.TestCase):
    def test_silence_never_raises_power(self):
        """A wrong process name or a long loading screen reads as 0 fps. That is not a slow game."""
        controller, game, result = tuned(Goal(60), 300, blackouts=[(0, 10_000)])
        start = Controller.default_settings(Goal(60), DEV)
        self.assertEqual(controller.settings, start)
        self.assertEqual(game.settings, start)
        reasons = {d.reason for _, d in result.decisions}
        self.assertEqual(reasons, {"waiting for frames from the game"})
        self.assertFalse(controller.converged, "waiting is not the same as being tuned")

    def test_a_short_menu_does_not_poison_a_healthy_window(self):
        controller, game, result = tuned(Goal(60), 700, blackouts=[(400, 405), (500, 504)])
        late = [d for t, d in result.decisions if t > 380]
        self.assertTrue(late)
        self.assertFalse([d for d in late if d.changed], "brief menus must not trigger changes")
        self.assertTrue(all("below target" not in d.reason for d in late))

    def test_tuning_resumes_after_the_blackout(self):
        controller, game, result = tuned(Goal(60), 600, blackouts=[(0, 100)])
        after = [d for t, d in result.decisions if t > 120]
        self.assertTrue(any(d.changed for d in after), "it should start tuning once frames return")
        self.assertGreaterEqual(mean(s.fps_avg for s in result.samples[-60:]), 60 * 0.97)

    def test_heat_is_still_handled_while_the_game_is_silent(self):
        controller = Controller(Goal(60, max_temp_c=60.0), DEV, start=Settings(24, 2, 60))
        hot_silent = Sample(fps_avg=0, fps_low=0, gpu_util=0, apu_power_w=20, temp_c=75, battery_wh=40)
        decisions = [controller.observe(hot_silent) for _ in range(12)]
        last = decisions[-1]
        self.assertTrue(last.changed)
        self.assertLess(last.settings.tdp_w, 24)

    def test_pending_probe_survives_a_silent_window(self):
        controller = Controller(Goal(60), DEV, window=3, settle=0)
        controller.settings = Settings(14, 1, 60)
        busy = Sample(fps_avg=60, fps_low=57, gpu_util=0.5, apu_power_w=8, temp_c=50, battery_wh=40)
        silent = Sample(fps_avg=0, fps_low=0, gpu_util=0, apu_power_w=3, temp_c=50, battery_wh=40)
        probe = [controller.observe(busy) for _ in range(3)][-1]
        self.assertTrue(probe.changed)
        [controller.observe(silent) for _ in range(3)]
        self.assertEqual(controller.settings, probe.settings, "a loading screen must not undo or ban the probe")


class UnreadableTemperatureTests(unittest.TestCase):
    def test_never_raises_power_when_blind_to_heat(self):
        goal = Goal(60)
        start = Controller.default_settings(goal, DEV)
        controller, game, result = tuned(goal, 500, scenes=((900, 1.6),), hide_temp=True)
        peak = max(d.settings.tdp_w for _, d in result.decisions)
        self.assertLessEqual(peak, start.tdp_w)
        self.assertLessEqual(game.settings.tdp_w, start.tdp_w)

    def test_it_says_why_and_falls_back_to_resolution(self):
        # so heavy that even the lowest resolution needs more power than the start setting
        controller, game, result = tuned(Goal(60), 500, scenes=((900, 3.0),), hide_temp=True)
        reasons = " | ".join(d.reason for _, d in result.decisions)
        self.assertIn("lowering resolution", reasons)
        self.assertEqual(game.settings.res_index, 0)
        self.assertIn("temperature unreadable", reasons)

    def test_can_still_lower_power_without_a_temperature(self):
        controller, game, result = tuned(Goal(60, runtime_hours=2.0, prefer="battery"), 500,
                                         scenes=((900, 0.5),), hide_temp=True)
        self.assertLess(game.settings.tdp_w, Controller.default_settings(Goal(60), DEV).tdp_w)

    def test_normal_sensor_still_allows_raising_power(self):
        controller, game, result = tuned(Goal(60), 500, scenes=((900, 1.6),))
        self.assertGreater(max(d.settings.tdp_w for _, d in result.decisions),
                           Controller.default_settings(Goal(60), DEV).tdp_w)


if __name__ == "__main__":
    unittest.main()
