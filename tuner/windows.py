"""Windows backend for the Legion Go. NOT yet tested on real hardware.

Telemetry:  PresentMon (frame times), ryzenadj --info (APU power, temperature),
            GetSystemPowerStatus (battery %).
Actuators:  ryzenadj (TDP limits), ChangeDisplaySettingsW (desktop resolution).

Needs an elevated prompt (RyzenAdj talks to the SMU). Parsing lives in plain
functions at the top so it can be unit-tested off Windows. Every change is
verified, and the original TDP and resolution are put back on exit.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .model import Device, Sample, Settings
from .restore import RestoreState, RestoreStore


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


def limits_to_mw(info: Dict[str, float]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(stapm, fast, slow) limits in mW from a parsed --info table; None where missing."""
    def mw(key: str) -> Optional[int]:
        return int(round(info[key] * 1000)) if key in info else None

    return mw("STAPM LIMIT"), mw("PPT LIMIT FAST"), mw("PPT LIMIT SLOW")


def ryzenadj_limit_args(stapm_mw: Optional[int], fast_mw: Optional[int], slow_mw: Optional[int]) -> List[str]:
    args = []
    for flag, value in (("stapm-limit", stapm_mw), ("fast-limit", fast_mw), ("slow-limit", slow_mw)):
        if value:
            args.append(f"--{flag}={value}")
    return args


# -- Win32 helpers (call only on Windows) ------------------------------------

def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("this only runs on Windows")


def is_admin() -> bool:
    import ctypes

    _require_windows()
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def battery_status() -> Tuple[Optional[int], Optional[bool]]:
    """(percent or None if unknown, on AC power or None if unknown)."""
    import ctypes
    from ctypes import wintypes

    _require_windows()

    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                    ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    st = Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return None, None
    pct = None if st.BatteryLifePercent == 255 else int(st.BatteryLifePercent)
    ac = None if st.ACLineStatus == 255 else st.ACLineStatus == 1
    return pct, ac


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


def current_mode() -> Optional[Tuple[int, int]]:
    import ctypes

    _require_windows()
    dm = _devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
        return None
    return int(dm.dmPelsWidth), int(dm.dmPelsHeight)


def current_refresh_hz() -> Optional[int]:
    import ctypes

    _require_windows()
    dm = _devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
        return None
    return int(dm.dmDisplayFrequency)


def list_modes() -> List[Tuple[int, int, int]]:
    """Every (width, height, refresh Hz) the primary display reports, deduplicated."""
    import ctypes

    _require_windows()
    seen = set()
    i = 0
    while True:
        dm = _devmode()
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, i, ctypes.byref(dm)):
            break
        seen.add((int(dm.dmPelsWidth), int(dm.dmPelsHeight), int(dm.dmDisplayFrequency)))
        i += 1
    return sorted(seen)


def set_resolution(width: int, height: int) -> bool:
    """Switch the desktop resolution, then confirm it by reading it back."""
    import ctypes

    _require_windows()
    if current_mode() == (width, height):
        return True
    for keep_refresh in (True, False):  # the current refresh rate may not exist at the new size
        dm = _devmode()
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            return False
        dm.dmPelsWidth, dm.dmPelsHeight = width, height
        dm.dmFields = 0x80000 | 0x100000 | (0x400000 if keep_refresh else 0)  # PELSWIDTH|PELSHEIGHT|DISPLAYFREQUENCY
        # flags=0: dynamic change, not written to the registry, so a reboot always restores it
        if ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0) == 0:  # DISP_CHANGE_SUCCESSFUL
            time.sleep(0.5)
            if current_mode() == (width, height):
                return True
    return False


def restore_state(ryzenadj: str, state: RestoreState) -> List[str]:
    """Puts the saved TDP limits and resolution back. Returns problems, empty on success."""
    problems = []
    args = ryzenadj_limit_args(state.stapm_mw, state.fast_mw, state.slow_mw)
    if args:
        try:
            r = subprocess.run([ryzenadj] + args, capture_output=True, timeout=10)
            if r.returncode != 0:
                problems.append("ryzenadj refused to restore the TDP limits")
        except (OSError, subprocess.TimeoutExpired):
            problems.append("could not run ryzenadj to restore the TDP limits")
    if state.width and state.height and not set_resolution(state.width, state.height):
        problems.append(f"could not restore {state.width}x{state.height}")
    return problems


def _install_exit_hooks(cleanup) -> None:
    """Run cleanup on normal exit, SIGTERM/SIGBREAK, and closing the console window."""
    atexit.register(cleanup)

    def on_signal(_signum, _frame):
        cleanup()
        raise SystemExit(1)

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass  # not the main thread

    import ctypes

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_console_event(event):
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            cleanup()
        return 0

    _install_exit_hooks._keepalive = handler_type(on_console_event)  # must outlive this call
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_install_exit_hooks._keepalive, True)


# -- backend ---------------------------------------------------------------

class WindowsBackend:
    FAST_LIMIT_RATIO = 1.2  # short-boost limit relative to sustained TDP
    TDP_TOLERANCE_W = 0.5

    def __init__(
        self,
        device: Device,
        game: str,
        presentmon: str,
        ryzenadj: str,
        read_only: bool = False,
        restore_store: Optional[RestoreStore] = None,
        notify=print,
    ) -> None:
        _require_windows()
        self.device = device
        self.ryzenadj = ryzenadj
        self.read_only = read_only
        self.notify = notify
        self.settings: Optional[Settings] = None
        self.extras: Dict[str, float] = {}
        self._last_info: Dict[str, float] = {}
        self._frames: Deque[Tuple[float, float]] = deque()  # (monotonic time, ms)
        self._lock = threading.Lock()
        self._last_sample = time.monotonic()
        self._closed = False

        self._read_info()
        if not read_only:
            self._store = restore_store or RestoreStore()
            self._save_restore_point()
            _install_exit_hooks(self.close)

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
        pct, _ac = battery_status()
        cap = self.settings.fps_cap if self.settings else 60
        return Sample(
            fps_avg=fps_avg,
            fps_low=fps_low,
            gpu_util=estimate_gpu_util(fps_avg, cap),
            apu_power_w=info.get("STAPM VALUE", info.get("PPT VALUE SLOW", 0.0)),
            temp_c=info.get("THM VALUE CORE", 0.0),
            battery_wh=self.device.battery_wh * (100 if pct is None else pct) / 100.0,
        )

    def apply(self, settings: Settings) -> Settings:
        """Applies what it can, verifies each change, and returns what is really in effect."""
        d = self.device
        if not (d.tdp_min_w <= settings.tdp_w <= d.tdp_max_w and 0 <= settings.res_index < len(d.resolutions)):
            raise ValueError(f"refusing out-of-range settings {settings}")
        if self.read_only:
            raise RuntimeError("backend is read-only")

        prev = self.settings
        tdp = prev.tdp_w if prev else self._effective_tdp(settings.tdp_w)
        res = prev.res_index if prev else self._effective_res(settings.res_index)

        if tdp != settings.tdp_w:
            tdp = settings.tdp_w if self._set_tdp(settings.tdp_w) else self._effective_tdp(tdp)
        if res != settings.res_index:
            w, h = d.resolutions[settings.res_index]
            res = settings.res_index if set_resolution(w, h) else self._effective_res(res)

        self.settings = Settings(tdp, res, settings.fps_cap)
        return self.settings

    def current_settings(self) -> Optional[Settings]:
        """What the device is set to right now, for starting a dry run from reality."""
        info = self._read_info()
        mode = current_mode()
        if "STAPM LIMIT" not in info or mode is None:
            return None
        res = self.device.resolution_index(*mode)
        if res is None:
            return None
        return Settings(self.device.snap_tdp(info["STAPM LIMIT"]), res, 60)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._pm.terminate()
        except OSError:
            pass
        if self.read_only:
            return
        state = self._store.load()
        problems = restore_state(self.ryzenadj, state) if state else ["no restore point was saved"]
        if problems:
            self.notify("could not fully restore your settings: " + "; ".join(problems)
                        + " (run `python -m tuner restore`, or reboot)")
        else:
            self._store.clear()

    # -- internals

    def _save_restore_point(self) -> None:
        if self._store.exists():
            self.notify("a previous run did not exit cleanly; keeping its saved original settings "
                        "(run `python -m tuner restore` to put them back now)")
            return
        stapm, fast, slow = limits_to_mw(self._last_info)
        mode = current_mode()
        if stapm is None:
            self.notify("could not read the current TDP limits, so they cannot be restored "
                        "automatically (sleeping or rebooting resets them)")
        self._store.save(RestoreState(stapm, fast, slow, *(mode or (None, None))))

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

    def _set_tdp(self, tdp_w: int) -> bool:
        mw = tdp_w * 1000
        args = ryzenadj_limit_args(mw, int(mw * self.FAST_LIMIT_RATIO), mw)
        for attempt in range(2):  # the power table can lag a moment behind the write
            try:
                r = subprocess.run([self.ryzenadj] + args, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if r.returncode != 0:
                return False
            time.sleep(0.5 if attempt == 0 else 1.0)
            info = self._read_info()
            if "STAPM LIMIT" not in info or abs(info["STAPM LIMIT"] - tdp_w) <= self.TDP_TOLERANCE_W:
                return True  # verified, or the limit cannot be read back so trust the exit code
        return False

    def _effective_tdp(self, fallback: int) -> int:
        info = self._read_info()
        return self.device.snap_tdp(info["STAPM LIMIT"]) if "STAPM LIMIT" in info else fallback

    def _effective_res(self, fallback: int) -> int:
        mode = current_mode()
        idx = self.device.resolution_index(*mode) if mode else None
        return fallback if idx is None else idx

    def _read_info(self) -> Dict[str, float]:
        try:
            out = subprocess.run([self.ryzenadj, "--info"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return self._last_info
        info = parse_ryzenadj_info(out)
        if info:
            self._last_info = info
            self.extras = info
        return self._last_info
