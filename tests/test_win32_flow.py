"""Runs the Windows-only code paths on any machine by faking the Win32 API underneath them.

This cannot prove the real API behaves as assumed, but it executes every line of control flow,
so typos, wrong flags and bad argument order are caught here instead of on the Legion Go.
"""
import ctypes
import sys
import types
import unittest
from unittest import mock

import overlay.winapi as winapi
import tuner.windows as W
from tuner.restore import RestoreState

DM_PELSWIDTH, DM_PELSHEIGHT, DM_DISPLAYFREQUENCY = 0x80000, 0x100000, 0x400000

MODES = [(1280, 800, 60), (1280, 800, 144), (1600, 1000, 144), (1920, 1200, 60), (1920, 1200, 144),
         (2560, 1600, 60), (2560, 1600, 144)]


class FakeUser32:
    def __init__(self):
        self.mode, self.hz = (2560, 1600), 144
        self.modes = list(MODES)
        self.changes = []
        self.change_ignored = False  # returns success but the mode does not change
        self.exstyle = 0x100
        self.pos_calls = []
        self.keys_down = set()
        self.dpi_aware = False
        # real ctypes functions take attributes such as .argtypes; bound methods do not
        for name in ("EnumDisplaySettingsW", "ChangeDisplaySettingsW", "GetParent", "GetWindowLongW",
                     "SetWindowLongW", "SetWindowPos", "GetAsyncKeyState", "SetProcessDPIAware"):
            setattr(self, name, self._as_function(getattr(self, "_" + name)))

    @staticmethod
    def _as_function(method):
        def function(*args):
            return method(*args)
        return function

    # display
    def _EnumDisplaySettingsW(self, name, index, ref):
        dm = ref._obj
        if index == -1:
            (dm.dmPelsWidth, dm.dmPelsHeight), dm.dmDisplayFrequency = self.mode, self.hz
            return 1
        if 0 <= index < len(self.modes):
            dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = self.modes[index]
            return 1
        return 0

    def _ChangeDisplaySettingsW(self, ref, flags):
        dm = ref._obj
        w, h, hz, fields = dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency, dm.dmFields
        self.changes.append({"size": (w, h), "fields": fields, "flags": flags, "hz": hz, "dmSize": dm.dmSize})
        wants_hz = bool(fields & DM_DISPLAYFREQUENCY)
        found = [m for m in self.modes if m[:2] == (w, h) and (not wants_hz or m[2] == hz)]
        if not found:
            return -2  # DISP_CHANGE_BADMODE
        if not self.change_ignored:
            self.mode, self.hz = (w, h), (hz if wants_hz else found[-1][2])
        return 0

    # window
    def _GetParent(self, hwnd):
        return None  # a top-level Tk window has no parent: the code must fall back to winfo_id

    def _GetWindowLongW(self, hwnd, index):
        assert index == -20
        return self.exstyle

    def _SetWindowLongW(self, hwnd, index, value):
        assert index == -20
        self.exstyle = value
        return 0

    def _SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
        self.pos_calls.append((hwnd, after, flags))
        return 1

    def _GetAsyncKeyState(self, vk):
        return -32768 if vk in self.keys_down else 0  # a SHORT with the high bit set

    def _SetProcessDPIAware(self):
        self.dpi_aware = True


def fake_windll(user32=None, admin=True, power=(1, 0, 80), xinput=None):
    user32 = user32 or FakeUser32()
    status = {"power": power}

    def get_power_status(ref):
        st = ref._obj
        st.ACLineStatus, st.BatteryFlag, st.BatteryLifePercent = status["power"]
        return 1

    windll = types.SimpleNamespace(
        user32=user32,
        shell32=types.SimpleNamespace(IsUserAnAdmin=lambda: 1 if admin else 0),
        kernel32=types.SimpleNamespace(GetSystemPowerStatus=get_power_status),
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=mock.Mock(side_effect=OSError("no shcore"))),
    )
    for name, fn in (xinput or {}).items():
        setattr(windll, name, types.SimpleNamespace(XInputGetState=fn))
    windll.status = status
    return windll


class OnFakeWindows(unittest.TestCase):
    def setUp(self):
        self.windll = fake_windll()
        self.user32 = self.windll.user32
        for patcher in (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(ctypes, "windll", self.windll, create=True),
            mock.patch.object(winapi, "IS_WINDOWS", True),
            mock.patch("tuner.windows.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class DisplayTests(OnFakeWindows):
    def test_reading_the_display(self):
        self.assertEqual(W.current_mode(), (2560, 1600))
        self.assertEqual(W.current_refresh_hz(), 144)
        self.assertEqual(W.list_modes(), sorted(MODES))

    def test_switching_resolution_requests_a_dynamic_change_at_the_current_refresh_rate(self):
        self.assertTrue(W.set_resolution(1600, 1000))
        (change,) = self.user32.changes
        self.assertEqual(change["size"], (1600, 1000))
        self.assertEqual(change["fields"], DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY)
        self.assertEqual(change["flags"], 0, "flags=0 means not written to the registry, so a reboot undoes it")
        self.assertEqual(change["dmSize"], 220)
        self.assertEqual(W.current_mode(), (1600, 1000))

    def test_switching_to_the_current_mode_does_nothing(self):
        self.assertTrue(W.set_resolution(2560, 1600))
        self.assertEqual(self.user32.changes, [])

    def test_falls_back_to_any_refresh_rate_when_the_current_one_does_not_exist(self):
        self.user32.modes = [m for m in MODES if m != (1600, 1000, 144)] + [(1600, 1000, 60)]
        self.assertTrue(W.set_resolution(1600, 1000))
        self.assertEqual(len(self.user32.changes), 2)
        self.assertFalse(self.user32.changes[1]["fields"] & DM_DISPLAYFREQUENCY)
        self.assertEqual(W.current_mode(), (1600, 1000))

    def test_an_unsupported_mode_fails_cleanly(self):
        self.assertFalse(W.set_resolution(1366, 768))
        self.assertEqual(W.current_mode(), (2560, 1600))

    def test_success_is_confirmed_by_reading_the_mode_back(self):
        self.user32.change_ignored = True  # Windows says OK but nothing changed
        self.assertFalse(W.set_resolution(1600, 1000))

    def test_restore_helper_on_the_real_system_class(self):
        self.user32.mode = (1280, 800)
        calls = []
        system = W.System()
        system.run = lambda cmd, timeout=10: (calls.append(cmd), (0, ""))[1]
        problems = W.restore_state("ryzenadj.exe", RestoreState(15000, 20000, 15000, 2560, 1600), system)
        self.assertEqual(problems, [])
        self.assertEqual(W.current_mode(), (2560, 1600))
        self.assertIn("--fast-limit=20000", calls[0])


class SystemTests(OnFakeWindows):
    def test_battery(self):
        self.assertEqual(W.battery_status(), (80, True))
        self.windll.status["power"] = (0, 0, 255)
        self.assertEqual(W.battery_status(), (None, False))
        self.windll.status["power"] = (255, 0, 40)
        self.assertEqual(W.battery_status(), (40, None))

    def test_admin_check(self):
        self.assertTrue(W.is_admin())
        self.windll.shell32.IsUserAnAdmin = lambda: 0
        self.assertFalse(W.is_admin())

    def test_system_wrapper_methods(self):
        system = W.System()
        self.assertTrue(system.is_admin())
        self.assertEqual(system.battery_percent(), 80)
        self.assertEqual(system.current_mode(), (2560, 1600))
        self.assertTrue(system.set_resolution(1920, 1200))

    def test_run_reports_exit_codes_missing_programs_and_hangs(self):
        system = W.System.__new__(W.System)  # skip the platform check: this only uses subprocess
        code, out = system.run([sys.executable, "-c", "print('hi'); import sys; sys.exit(3)"])
        self.assertEqual((code, out.strip()), (3, "hi"))
        code, out = system.run(["definitely-not-a-real-program-xyz"])
        self.assertEqual(code, -1)
        self.assertTrue(out)
        code, _ = system.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertEqual(code, -1)


class OverlayWindowTests(OnFakeWindows):
    ROOT = types.SimpleNamespace(winfo_id=lambda: 4242)

    def test_click_through_sets_the_right_styles_and_goes_topmost(self):
        self.assertTrue(winapi.make_click_through(self.ROOT))
        style = self.user32.exstyle
        for flag in (0x80000, 0x20, 0x8000000, 0x80):  # layered, transparent, no-activate, tool window
            self.assertTrue(style & flag, hex(flag))
        self.assertTrue(style & 0x100, "keeps the styles the window already had")
        hwnd, after, flags = self.user32.pos_calls[-1]
        self.assertEqual((hwnd, after), (4242, -1), "HWND_TOPMOST is -1")
        self.assertEqual(flags, 0x1 | 0x2 | 0x10 | 0x40)

    def test_argument_types_are_declared_for_pointer_sized_handles(self):
        winapi.make_click_through(self.ROOT)
        u = self.user32
        self.assertEqual(u.SetWindowPos.argtypes[:2], [ctypes.c_void_p, ctypes.c_void_p])
        self.assertIs(u.GetParent.restype, ctypes.c_void_p)
        self.assertEqual(u.GetWindowLongW.argtypes[0], ctypes.c_void_p)
        self.assertEqual(u.GetAsyncKeyState.argtypes, [ctypes.c_int])

    def test_failures_never_raise(self):
        self.user32.SetWindowLongW = mock.Mock(side_effect=OSError("nope"))
        self.assertFalse(winapi.make_click_through(self.ROOT))
        self.user32.SetWindowPos = mock.Mock(side_effect=OSError("nope"))
        winapi.keep_on_top(self.ROOT)

    def test_dpi_awareness_falls_back_to_the_older_call(self):
        winapi.enable_dpi_awareness()
        self.assertTrue(self.user32.dpi_aware)

    def test_keyboard_shortcut_needs_all_three_keys(self):
        self.assertFalse(winapi.shortcut_down())
        self.user32.keys_down = {0x11, 0x12}
        self.assertFalse(winapi.shortcut_down())
        self.user32.keys_down = {0x11, 0x12, 0x4F}
        self.assertTrue(winapi.shortcut_down())


class XInputTests(unittest.TestCase):
    def reader_with(self, windll):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", windll, create=True), \
                mock.patch.object(winapi, "IS_WINDOWS", True):
            return winapi.XInputReader()

    @staticmethod
    def pads(buttons_by_index):
        def get_state(index, ref):
            if index in buttons_by_index:
                ref._obj.Gamepad.wButtons = buttons_by_index[index]
                return 0
            return 1167  # ERROR_DEVICE_NOT_CONNECTED
        return get_state

    def test_combines_buttons_across_controllers_and_skips_disconnected_ones(self):
        reader = self.reader_with(fake_windll(xinput={"xinput1_4": self.pads({0: 0x0040, 2: 0x0080})}))
        self.assertTrue(reader.available)
        self.assertEqual(reader.buttons(), 0x00C0)

    def test_falls_back_through_older_dlls(self):
        # xinput1_4 and xinput1_3 do not exist on this machine: looking them up raises
        reader = self.reader_with(fake_windll(xinput={"xinput9_1_0": self.pads({0: 0x20})}))
        self.assertTrue(reader.available)
        self.assertEqual(reader.buttons(), 0x20)

    def test_no_xinput_means_no_input_and_no_crash(self):
        reader = self.reader_with(fake_windll())
        self.assertFalse(reader.available)
        self.assertEqual(reader.buttons(), 0)

    def test_a_failing_call_returns_what_was_read_so_far(self):
        def get_state(index, ref):
            if index == 0:
                ref._obj.Gamepad.wButtons = 0x40
                return 0
            raise OSError("driver hiccup")

        reader = self.reader_with(fake_windll(xinput={"xinput1_4": get_state}))
        self.assertEqual(reader.buttons(), 0x40)

    def test_off_windows_it_is_inert(self):
        reader = winapi.XInputReader()
        self.assertFalse(reader.available)
        self.assertEqual(reader.buttons(), 0)


if __name__ == "__main__":
    unittest.main()
