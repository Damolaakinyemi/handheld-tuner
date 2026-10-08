from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .model import Device, Goal, Settings

DEFAULT_PATH = Path.home() / ".handheld-tuner" / "profiles.json"


class ProfileStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH

    @staticmethod
    def key(game: str, goal: Goal) -> str:
        hours = f"{goal.runtime_hours:g}h" if goal.runtime_hours else "any"
        return f"{game.lower()}|{goal.target_fps}fps|{goal.prefer}|{hours}"

    def load(self, game: str, goal: Goal, device: Device) -> Optional[Settings]:
        entry = self._read().get(self.key(game, goal))
        if not entry:
            return None
        try:
            s = Settings(int(entry["tdp_w"]), int(entry["res_index"]), goal.target_fps)
        except (KeyError, TypeError, ValueError):
            return None
        valid = (
            device.tdp_min_w <= s.tdp_w <= device.tdp_max_w
            and 0 <= s.res_index < len(device.resolutions)
        )
        return s if valid else None

    def save(self, game: str, goal: Goal, settings: Settings, fps: float, power_w: float) -> None:
        data = self._read()
        data[self.key(game, goal)] = {
            "tdp_w": settings.tdp_w,
            "res_index": settings.res_index,
            "fps": round(fps, 1),
            "power_w": round(power_w, 1),
            "updated": int(time.time()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def all(self) -> dict:
        return self._read()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
