"""How a handheld player opens the panel without a mouse: hold a controller chord or a keyboard shortcut."""
from __future__ import annotations

from typing import Optional

# XInput button bits
LEFT_THUMB = 0x0040   # L3
RIGHT_THUMB = 0x0080  # R3
BACK = 0x0020

DEFAULT_CHORD = LEFT_THUMB | RIGHT_THUMB


class HoldTrigger:
    """Fires once each time `active` has stayed true for `hold` seconds. Releasing re-arms it."""

    def __init__(self, hold: float = 0.5) -> None:
        self.hold = hold
        self._since: Optional[float] = None
        self._fired = False

    def update(self, active: bool, now: float) -> bool:
        if not active:
            self._since, self._fired = None, False
            return False
        if self._since is None:
            self._since = now
        if not self._fired and now - self._since >= self.hold:
            self._fired = True
            return True
        return False


def chord_pressed(buttons: int, chord: int = DEFAULT_CHORD) -> bool:
    return buttons & chord == chord
