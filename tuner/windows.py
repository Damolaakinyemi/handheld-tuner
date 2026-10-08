"""Windows backend for the Legion Go. NOT yet tested on real hardware.

Telemetry:  PresentMon (frame times), ryzenadj --info (APU power, temperature),
            GetSystemPowerStatus (battery %).
Actuators:  ryzenadj (TDP limits), ChangeDisplaySettingsW (desktop resolution).

Needs an elevated prompt (RyzenAdj talks to the SMU). Parsing lives in plain
functions at the top so it can be unit-tested off Windows.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .model import Device, Sample, Settings


# -- pure parsing helpers --------------------------------------------------

def frame_time_column(header_line: str) -> Optional[int]:
    cols = [c.strip().lower() for c in header_line.split(",")]
    for name in ("msbetweenpresents", "msbetweendisplaychange"):
        if name in cols:
            return cols.index(name)
    return None


def parse_frame_ms(line: str, col: int) -> Optional[float]:
    parts = line.split(",")
    if col >= len(parts):
        return None
    try:
        ms = float(parts[col])
    except ValueError:
        return None
    return ms if ms > 0 else None


def fps_stats(frame_ms: List[float]) -> Tuple[float, float]:
    """(average fps, 1% low fps) from frame intervals in ms."""
    if not frame_ms:
        return 0.0, 0.0
    avg = 1000.0 * len(frame_ms) / sum(frame_ms)
    ordered = sorted(frame_ms)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    return avg, 1000.0 / p99


def parse_ryzenadj_info(text: str) -> Dict[str, float]:
    """Parses '| NAME | value | parameter |' rows into {NAME: value}."""
    out: Dict[str, float] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        try:
            out[cells[0].upper()] = float(cells[1])
        except ValueError:
            continue
    return out


def estimate_gpu_util(fps_avg: float, cap: int) -> float:
    """With no frame limiter, headroom is inferred: running at 90 fps for a 60 fps target
    means the GPU would be ~67% busy if capped. Games locked by vsync read as fully busy."""
    if fps_avg <= 0:
        return 1.0
    return min(1.0, cap / fps_avg)


# -- backend ---------------------------------------------------------------

class WindowsBackend:
    FAST_LIMIT_RATIO = 1.2  # short-boost limit relative to sustained TDP

    def __init__(self, device: Device, game: str, presentmon: str, ryzenadj: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the Windows backend only runs on Windows")
        self.device = device
        self.ryzenadj = ryzenadj
        self.settings: Optional[Settings] = None
        self._frames: Deque[Tuple[float, float]] = deque()  # (monotonic time, ms)
        self._lock = threading.Lock()
        self._last_sample = time.monotonic()
        self._original_mode = self._current_mode()
        self._last_info: Dict[str, float] = {}

        self._pm = subprocess.Popen(
            [presentmon, "--process_name", game, "--output_stdout",
             "--stop_existing_session", "--terminate_on_proc_exit"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        threading.Thread(target=self._read_presentmon, daemon=True).start()

    # -- Backend protocol

    def sample(self) -> Sample:
        wait = 1.0 - (time.monotonic() - self._last_sample)
        if wait > 0:
            time.sleep(wait)
        now = time.monotonic()
        with self._lock:
            while self._frames and self._frames[0][0] < now - 1.0:
                self._frames.popleft()
            recent = [ms for _, ms in self._frames]
        self._last_sample = now

        fps_avg, fps_low = fps_stats(recent)
        info = self._read_info()
        cap = self.settings.fps_cap if self.settings else 60
        return Sample(
            fps_avg=fps_avg,
            fps_low=fps_low,
            gpu_util=estimate_gpu_util(fps_avg, cap),
            apu_power_w=info.get("STAPM VALUE", info.get("PPT VALUE SLOW", 0.0)),
            temp_c=info.get("THM VALUE CORE", 0.0),
            battery_wh=self._battery_wh(),
        )

    def apply(self, settings: Settings) -> None:
        prev = self.settings
        if prev is None or prev.tdp_w != settings.tdp_w:
            self._set_tdp(settings.tdp_w)
        if prev is None or prev.res_index != settings.res_index:
            w, h = self.device.resolutions[settings.res_index]
            if not self._set_resolution(w, h):
                print(f"warning: could not switch display to {w}x{h}", file=sys.stderr)
        self.settings = settings

    def close(self) -> None:
        self._pm.terminate()
        if self._original_mode:
            self._set_resolution(*self._original_mode)

    # -- internals

    def _read_presentmon(self) -> None:
        col = None
        for line in self._pm.stdout:
            if col is None:
                col = frame_time_column(line)
                continue
            ms = parse_frame_ms(line, col)
            if ms is not None:
                with self._lock:
                    self._frames.append((time.monotonic(), ms))

    def _set_tdp(self, tdp_w: int) -> None:
        mw = tdp_w * 1000
        subprocess.run(
            [self.ryzenadj, f"--stapm-limit={mw}", f"--slow-limit={mw}",
             f"--fast-limit={int(mw * self.FAST_LIMIT_RATIO)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    def _read_info(self) -> Dict[str, float]:
        try:
            out = subprocess.run([self.ryzenadj, "--info"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return self._last_info
        info = parse_ryzenadj_info(out)
        if info:
            self._last_info = info
        return self._last_info

    def _battery_wh(self) -> float:
        import ctypes
        from ctypes import wintypes

        class Status(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

        st = Status()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
        pct = st.BatteryLifePercent
        pct = 100 if pct == 255 else pct  # 255 means unknown
        return self.device.battery_wh * pct / 100.0

    @staticmethod
    def _devmode():
        import ctypes
        from ctypes import wintypes

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
            ]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        return dm

    def _current_mode(self) -> Optional[Tuple[int, int]]:
        import ctypes

        dm = self._devmode()
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
            return None
        return dm.dmPelsWidth, dm.dmPelsHeight

    def _set_resolution(self, width: int, height: int) -> bool:
        import ctypes

        dm = self._devmode()
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            return False
        dm.dmPelsWidth, dm.dmPelsHeight = width, height
        dm.dmFields = 0x80000 | 0x100000 | 0x400000  # DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
        # flags=0: dynamic change, not written to the registry, so a reboot always restores it
        return ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0) == 0  # DISP_CHANGE_SUCCESSFUL
