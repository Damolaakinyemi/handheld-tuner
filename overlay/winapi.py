"""Windows-only window and input tricks. NOT yet tested on real hardware.

Every function is a safe no-op elsewhere, and swallows failures: an overlay that cannot become
click-through should still draw, and a missing XInput DLL should not crash it.

ctypes passes plain ints as 32-bit C ints. Window handles and HWND_TOPMOST (-1) are pointer-sized, so
every call that takes one declares its argument types, or 64-bit Windows would silently ignore it.
"""
from __future__ import annotations

import ctypes
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


class XInputGamepad(ctypes.Structure):  # XINPUT_GAMEPAD, 12 bytes
    _fields_ = [("wButtons", ctypes.c_uint16), ("bLeftTrigger", ctypes.c_uint8),
                ("bRightTrigger", ctypes.c_uint8), ("sThumbLX", ctypes.c_int16),
                ("sThumbLY", ctypes.c_int16), ("sThumbRX", ctypes.c_int16), ("sThumbRY", ctypes.c_int16)]


class XInputState(ctypes.Structure):  # XINPUT_STATE, 16 bytes
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", XInputGamepad)]


def _user32():
    """user32 with the argument types this module needs declared, once."""
    user32 = ctypes.windll.user32
    if not getattr(user32, "_tuner_typed", False):
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_int
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32._tuner_typed = True
    return user32


def enable_dpi_awareness() -> None:
    """Call before creating the Tk root, or Windows scales the overlay up and blurs it."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _hwnd(root) -> int:
    inner = root.winfo_id()
    return _user32().GetParent(inner) or inner


def make_click_through(root) -> bool:
    """Click-through, focus-less, hidden from Alt+Tab, and above the game."""
    if not IS_WINDOWS:
        return False
    try:
        user32 = _user32()
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
    try:
        _user32().SetWindowPos(_hwnd(root), _HWND_TOPMOST, 0, 0, 0, 0,
                               _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
    except Exception:
        pass


def shortcut_down() -> bool:
    """Ctrl+Alt+O, for handhelds docked to a keyboard."""
    if not IS_WINDOWS:
        return False
    try:
        key = _user32().GetAsyncKeyState
        return all(key(vk) & 0x8000 for vk in (_VK_CONTROL, _VK_MENU, _VK_O))
    except Exception:
        return False


class XInputReader:
    """Reads (never consumes) the state of connected XInput controllers, as a button bitmask."""

    def __init__(self) -> None:
        self._get_state = None
        if not IS_WINDOWS:
            return
        for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                fn = getattr(ctypes.windll, dll).XInputGetState
                fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(XInputState)]
                fn.restype = ctypes.c_uint32
                self._get_state = fn
                break
            except (OSError, AttributeError):
                continue

    @property
    def available(self) -> bool:
        return self._get_state is not None

    def buttons(self) -> int:
        if not self.available:
            return 0
        mask = 0
        for index in range(4):
            state = XInputState()
            try:
                if self._get_state(index, ctypes.byref(state)) == 0:  # ERROR_SUCCESS
                    mask |= state.Gamepad.wButtons
            except Exception:
                return mask
        return mask
