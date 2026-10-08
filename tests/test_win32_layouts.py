"""Win32 structs must match the Windows SDK byte for byte. Wrong offsets would read or write garbage,
and ctypes would not complain. The expected numbers come from the SDK headers."""
import ctypes
import unittest

from overlay.winapi import XInputGamepad, XInputState
from tuner.windows import DEVMODEW, SystemPowerStatus, _devmode


class LayoutTests(unittest.TestCase):
    def test_devmodew(self):
        self.assertEqual(ctypes.sizeof(DEVMODEW), 220)
        offsets = {"dmSpecVersion": 64, "dmSize": 68, "dmFields": 72, "dmPositionX": 76,
                   "dmDisplayOrientation": 84, "dmColor": 92, "dmFormName": 102, "dmLogPixels": 166,
                   "dmBitsPerPel": 168, "dmPelsWidth": 172, "dmPelsHeight": 176, "dmDisplayFlags": 180,
                   "dmDisplayFrequency": 184, "dmICMMethod": 188, "dmPanningHeight": 216}
        for name, offset in offsets.items():
            self.assertEqual(getattr(DEVMODEW, name).offset, offset, name)

    def test_new_devmode_declares_its_own_size(self):
        self.assertEqual(_devmode().dmSize, 220)

    def test_system_power_status(self):
        self.assertEqual(ctypes.sizeof(SystemPowerStatus), 12)
        self.assertEqual(SystemPowerStatus.ACLineStatus.offset, 0)
        self.assertEqual(SystemPowerStatus.BatteryLifePercent.offset, 2)
        self.assertEqual(SystemPowerStatus.BatteryLifeTime.offset, 4)

    def test_xinput(self):
        self.assertEqual(ctypes.sizeof(XInputGamepad), 12)
        self.assertEqual(ctypes.sizeof(XInputState), 16)
        self.assertEqual(XInputState.Gamepad.offset, 4)
        self.assertEqual(XInputGamepad.wButtons.offset, 0)
        self.assertEqual(XInputGamepad.sThumbRY.offset, 10)

    def test_button_bits_match_the_xinput_header(self):
        from overlay.input import BACK, LEFT_THUMB, RIGHT_THUMB

        self.assertEqual((LEFT_THUMB, RIGHT_THUMB, BACK), (0x0040, 0x0080, 0x0020))


if __name__ == "__main__":
    unittest.main()
