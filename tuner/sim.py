from __future__ import annotations

import random
import time
from dataclasses import replace
from typing import Sequence, Tuple

from .model import Device, Sample, Settings


class SimGame:
    """A fake game with a plausible response to TDP, resolution and scene load.

    Capable fps scales with TDP^0.6 and inversely with pixel count and scene
    weight. The fps cap holds output at the target, and APU power follows how
    busy the GPU is, so headroom shows up as lower power just like on hardware.
    """

    BASE_FPS = 55.0  # at 15 W, 1920x1200, scene weight 1.0
    IDLE_W = 3.0

    def __init__(
        self,
        device: Device,
        settings: Settings,
        scenes: Sequence[Tuple[int, float]] = ((120, 1.0), (90, 1.45), (120, 0.8)),
        seed: int = 1,
        battery_wh: float = None,
        reject_res: Sequence[int] = (),  # resolution indexes the "hardware" refuses
        max_tdp: int = None,  # highest TDP the "hardware" accepts
    ) -> None:
        self.device = device
        self.settings = settings
        self.reject_res = set(reject_res)
        self.max_tdp = max_tdp
        self.scenes = list(scenes)
        self.rng = random.Random(seed)
        self.battery_wh = device.battery_wh if battery_wh is None else battery_wh
        self.temp_c = 40.0
        self.t = 0

    def apply(self, settings: Settings) -> Settings:
        actual = settings
        if settings.res_index in self.reject_res:
            actual = replace(actual, res_index=self.settings.res_index)
        if self.max_tdp is not None and settings.tdp_w > self.max_tdp:
            actual = replace(actual, tdp_w=self.settings.tdp_w)
        self.settings = actual
        return actual

    def scene_weight(self) -> float:
        elapsed = self.t
        for length, weight in self.scenes:
            if elapsed < length:
                return weight
            elapsed -= length
        return self.scenes[-1][1]

    def capable_fps(self) -> float:
        s = self.settings
        perf = (s.tdp_w / 15.0) ** 0.6
        px_ratio = self.device.pixels(s.res_index) / (1920 * 1200)
        return self.BASE_FPS * perf / px_ratio / self.scene_weight()

    def sample(self) -> Sample:
        s = self.settings
        capable = self.capable_fps() * (1 + self.rng.uniform(-0.03, 0.03))
        fps = min(capable, float(s.fps_cap))
        util = fps / capable
        power = self.IDLE_W + (s.tdp_w - self.IDLE_W) * util
        power *= 1 + self.rng.uniform(-0.02, 0.02)

        self.temp_c += (38.0 + 1.8 * power - self.temp_c) * 0.15
        self.battery_wh -= (power + self.device.base_power_w) / 3600.0
        self.t += 1

        jitter = 0.88 + 0.08 * util  # busier GPU, worse lows
        return Sample(
            fps_avg=fps,
            fps_low=fps * jitter,
            gpu_util=util,
            apu_power_w=power,
            temp_c=self.temp_c,
            battery_wh=self.battery_wh,
        )


class PacedBackend:
    """Makes a simulated backend behave like hardware: one sample per `interval` seconds."""

    def __init__(self, inner, interval: float = 1.0) -> None:
        self.inner = inner
        self.interval = interval

    def sample(self) -> Sample:
        time.sleep(self.interval)
        return self.inner.sample()

    def apply(self, settings: Settings) -> Settings:
        return self.inner.apply(settings)

    def close(self) -> None:
        pass
