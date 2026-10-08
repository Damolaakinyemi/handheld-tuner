"""Windows-only window and input tricks. NOT yet tested on real hardware.

Every function is a safe no-op elsewhere, and swallows failures: an overlay that cannot become
click-through should still draw, and a missing XInput DLL should not crash it.
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20        # clicks pass through to the game
_WS_EX_TOOLWINDOW = 0x80         # not in Alt+Tab or the taskbar
_WS_EX_LAYERED = 0x80000
_WS_EX_NOACTIVATE = 0x8000000    # never takes keyboard focus from the game
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE, _SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
_VK_CONTROL, _VK_MENU, _VK_O = 0x11, 0x12, 0x4F


def enable_dpi_awareness() -> None:
    """Call before creating the Tk root, or Windows scales the overlay up and blurs it."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _hwnd(root) -> int:
    import ctypes

    inner = root.winfo_id()
    return ctypes.windll.user32.GetParent(inner) or inner


def make_click_through(root) -> bool:
    """Click-through, focus-less, hidden from Alt+Tab, and above the game."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        user32 = ctypes.windll.user32
        hwnd = _hwnd(root)
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE,
                              style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
        keep_on_top(root)
        return True
    except Exception:
        return False


def keep_on_top(root) -> None:
    """Games can push other windows down; call this every couple of seconds."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.user32.SetWindowPos(_hwnd(root), _HWND_TOPMOST, 0, 0, 0, 0,
                                          _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
    except Exception:
        pass


def shortcut_down() -> bool:
    """Ctrl+Alt+O, for handhelds docked to a keyboard."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        key = ctypes.windll.user32.GetAsyncKeyState
        return all(key(vk) & 0x8000 for vk in (_VK_CONTROL, _VK_MENU, _VK_O))
    except Exception:
        return False


class XInputReader:
    """Reads (never consumes) the state of connected XInput controllers, as a button bitmask."""

    def __init__(self) -> None:
        self._get_state = None
        self._state_type = None
        if not IS_WINDOWS:
            return
        import ctypes

        class Gamepad(ctypes.Structure):
            _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                        ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                        ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                        ("sThumbRY", ctypes.c_short)]

        class State(ctypes.Structure):
            _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", Gamepad)]

        for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._get_state = getattr(ctypes.windll, dll).XInputGetState
                self._state_type = State
                break
            except (OSError, AttributeError):
                continue

    @property
    def available(self) -> bool:
        return self._get_state is not None

    def buttons(self) -> int:
        if not self.available:
            return 0
        import ctypes

        mask = 0
        for index in range(4):
            state = self._state_type()
            try:
                if self._get_state(index, ctypes.byref(state)) == 0:  # ERROR_SUCCESS
                    mask |= state.Gamepad.wButtons
            except Exception:
                return mask
        return mask
