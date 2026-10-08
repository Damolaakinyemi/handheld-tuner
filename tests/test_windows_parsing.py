import unittest

from tuner.windows import (
    estimate_gpu_util,
    fps_stats,
    frame_time_column,
    parse_frame_ms,
    parse_ryzenadj_info,
)


class ParsingTests(unittest.TestCase):
    def test_presentmon_row(self):
        header = "Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,MsBetweenPresents,MsInPresentAPI"
        col = frame_time_column(header)
        self.assertEqual(col, 5)
        self.assertAlmostEqual(parse_frame_ms("game.exe,1,0x1,DXGI,1,16.67,0.2", col), 16.67)
        self.assertIsNone(parse_frame_ms("game.exe,1,0x1,DXGI,1,NA,0.2", col))
        self.assertIsNone(parse_frame_ms("short", col))

    def test_missing_column(self):
        self.assertIsNone(frame_time_column("a,b,c"))

    def test_fps_stats(self):
        avg, low = fps_stats([16.0] * 99 + [50.0])
        self.assertAlmostEqual(avg, 1000 * 100 / (16 * 99 + 50), places=3)
        self.assertAlmostEqual(low, 20.0)
        self.assertEqual(fps_stats([]), (0.0, 0.0))

    def test_ryzenadj_info(self):
        text = (
            "| Name           | Value   | Parameter       |\n"
            "|----------------|---------|-----------------|\n"
            "| STAPM LIMIT    | 15.000  | stapm-limit     |\n"
            "| STAPM VALUE    | 11.250  |                 |\n"
            "| THM VALUE CORE | 67.500  | thm-value-core  |\n"
        )
        info = parse_ryzenadj_info(text)
        self.assertEqual(info["STAPM VALUE"], 11.25)
        self.assertEqual(info["THM VALUE CORE"], 67.5)
        self.assertNotIn("NAME", info)

    def test_gpu_util_estimate(self):
        self.assertAlmostEqual(estimate_gpu_util(90, 60), 60 / 90)
        self.assertEqual(estimate_gpu_util(55, 60), 1.0)
        self.assertEqual(estimate_gpu_util(0, 60), 1.0)


if __name__ == "__main__":
    unittest.main()
