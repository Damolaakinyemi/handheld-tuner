from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, Optional

from .model import Device, Sample, Settings

# RyzenAdj --info rows worth keeping next to every sample, for calibration
EXTRA_KEYS = ("STAPM LIMIT", "STAPM VALUE", "PPT LIMIT FAST", "PPT VALUE FAST",
              "PPT LIMIT SLOW", "PPT VALUE SLOW", "THM VALUE CORE")

COLUMNS = ["t", "wall_time", "fps_avg", "fps_low", "gpu_util", "apu_power_w", "temp_c",
           "battery_wh", "tdp_w", "res_w", "res_h", "decision"] + [k.lower().replace(" ", "_") for k in EXTRA_KEYS]


class CsvLog:
    """One row per second, flushed immediately so a crash still leaves a usable log."""

    def __init__(self, path, device: Device) -> None:
        self.path = Path(path)
        self.device = device
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)
        self._file.flush()

    def write(self, t: int, sample: Sample, settings: Settings, decision: str = "",
              extras: Optional[Dict[str, float]] = None) -> None:
        w, h = self.device.resolutions[settings.res_index]
        extras = extras or {}
        self._writer.writerow(
            [t, time.strftime("%Y-%m-%d %H:%M:%S"), f"{sample.fps_avg:.2f}", f"{sample.fps_low:.2f}",
             f"{sample.gpu_util:.3f}", f"{sample.apu_power_w:.2f}", f"{sample.temp_c:.1f}",
             f"{sample.battery_wh:.3f}", settings.tdp_w, w, h, decision]
            + [extras.get(k, "") for k in EXTRA_KEYS]
        )
        self._file.flush()

    def close(self) -> None:
        self._file.close()
