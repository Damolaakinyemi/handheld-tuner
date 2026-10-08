from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Device:
    name: str
    tdp_min_w: int
    tdp_max_w: int
    tdp_step_w: int
    resolutions: Tuple[Tuple[int, int], ...]  # ascending pixel count
    battery_wh: float
    base_power_w: float  # screen, SSD, radios: draw that APU power readings miss

    def pixels(self, res_index: int) -> int:
        w, h = self.resolutions[res_index]
        return w * h


LEGION_GO = Device(
    name="Lenovo Legion Go",
    tdp_min_w=8,
    tdp_max_w=30,
    tdp_step_w=2,
    resolutions=((1280, 800), (1600, 1000), (1920, 1200), (2560, 1600)),
    battery_wh=49.2,
    base_power_w=4.0,
)


@dataclass(frozen=True)
class Goal:
    target_fps: int = 60
    runtime_hours: Optional[float] = None  # None: no battery target
    max_temp_c: float = 85.0
    prefer: str = "quality"  # "quality" spends headroom on resolution, "battery" on saved power

    def __post_init__(self) -> None:
        if self.prefer not in ("quality", "battery"):
            raise ValueError("prefer must be 'quality' or 'battery'")


@dataclass(frozen=True)
class Settings:
    tdp_w: int
    res_index: int
    fps_cap: int


@dataclass(frozen=True)
class Sample:
    """One second of telemetry."""

    fps_avg: float
    fps_low: float  # 1% low over the second
    gpu_util: float  # 0..1, share of the frame budget the GPU was busy
    apu_power_w: float
    temp_c: float
    battery_wh: float  # remaining
