from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path.home() / ".handheld-tuner" / "restore.json"


@dataclass
class RestoreState:
    """What to put back if the tuner dies without cleaning up. None means 'could not be read'."""

    stapm_mw: Optional[int] = None
    fast_mw: Optional[int] = None
    slow_mw: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


class RestoreStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, state: RestoreState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), indent=2))

    def load(self) -> Optional[RestoreState]:
        try:
            raw = json.loads(self.path.read_text())
            return RestoreState(**{k: raw.get(k) for k in RestoreState.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
