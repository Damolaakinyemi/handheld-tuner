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
import ctypes
import shutil
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
    _require_windows()
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


# Win32 structs use explicit-size types, never wintypes or c_wchar, whose sizes differ by platform.
# That keeps the layout identical everywhere, so tests can check it without a Windows machine.
class SystemPowerStatus(ctypes.Structure):  # SYSTEM_POWER_STATUS, 12 bytes
    _fields_ = [("ACLineStatus", ctypes.c_uint8), ("BatteryFlag", ctypes.c_uint8),
                ("BatteryLifePercent", ctypes.c_uint8), ("SystemStatusFlag", ctypes.c_uint8),
                ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]


class DEVMODEW(ctypes.Structure):  # display flavour of DEVMODEW, 220 bytes
    _fields_ = [
        ("dmDeviceName", ctypes.c_uint16 * 32), ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16), ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16), ("dmFields", ctypes.c_uint32),
        ("dmPositionX", ctypes.c_int32), ("dmPositionY", ctypes.c_int32),
        ("dmDisplayOrientation", ctypes.c_uint32), ("dmDisplayFixedOutput", ctypes.c_uint32),
        ("dmColor", ctypes.c_int16), ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16), ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16), ("dmFormName", ctypes.c_uint16 * 32),
        ("dmLogPixels", ctypes.c_uint16), ("dmBitsPerPel", ctypes.c_uint32),
        ("dmPelsWidth", ctypes.c_uint32), ("dmPelsHeight", ctypes.c_uint32),
        ("dmDisplayFlags", ctypes.c_uint32), ("dmDisplayFrequency", ctypes.c_uint32),
        ("dmICMMethod", ctypes.c_uint32), ("dmICMIntent", ctypes.c_uint32),
        ("dmMediaType", ctypes.c_uint32), ("dmDitherType", ctypes.c_uint32),
        ("dmReserved1", ctypes.c_uint32), ("dmReserved2", ctypes.c_uint32),
        ("dmPanningWidth", ctypes.c_uint32), ("dmPanningHeight", ctypes.c_uint32),
    ]


def battery_status() -> Tuple[Optional[int], Optional[bool]]:
    """(percent or None if unknown, on AC power or None if unknown)."""
    _require_windows()
    st = SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return None, None
    pct = None if st.BatteryLifePercent == 255 else int(st.BatteryLifePercent)
    ac = None if st.ACLineStatus == 255 else st.ACLineStatus == 1
    return pct, ac


def _devmode() -> DEVMODEW:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def current_mode() -> Optional[Tuple[int, int]]:
    _require_windows()
    dm = _devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
        return None
    return int(dm.dmPelsWidth), int(dm.dmPelsHeight)


def current_refresh_hz() -> Optional[int]:
    _require_windows()
    dm = _devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
        return None
    return int(dm.dmDisplayFrequency)


def list_modes() -> List[Tuple[int, int, int]]:
    """Every (width, height, refresh Hz) the primary display reports, deduplicated."""
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


def restore_state(ryzenadj: str, state: RestoreState, system: Optional["System"] = None) -> List[str]:
    """Puts the saved TDP limits and resolution back. Returns problems, empty on success."""
    system = system or System()
    problems = []
    args = ryzenadj_limit_args(state.stapm_mw, state.fast_mw, state.slow_mw)
    if args:
        code, _ = system.run([ryzenadj] + args)
        if code != 0:
            problems.append("could not restore the TDP limits with ryzenadj")
    if state.width and state.height and not system.set_resolution(state.width, state.height):
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

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_console_event(event):
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            cleanup()
        return 0

    _install_exit_hooks._keepalive = handler_type(on_console_event)  # must outlive this call
    set_handler = ctypes.windll.kernel32.SetConsoleCtrlHandler
    set_handler.argtypes = [handler_type, ctypes.c_int]
    set_handler.restype = ctypes.c_int
    set_handler(_install_exit_hooks._keepalive, 1)


# -- pre-flight checks (pure, unit-tested) -----------------------------------

def startup_problems(ryzenadj: str, presentmon: str, system: "System") -> List[str]:
    """Things that must be true before the backend touches anything."""
    problems = []
    if not system.is_admin():
        problems.append("this terminal is not elevated; open it with Run as administrator, because "
                        "RyzenAdj cannot reach the power controller otherwise")
    if not system.which(ryzenadj):
        problems.append(f"cannot find RyzenAdj at {ryzenadj!r}; pass --ryzenadj with the full path to ryzenadj.exe")
    if not system.which(presentmon):
        problems.append(f"cannot find PresentMon at {presentmon!r}; pass --presentmon with the full path "
                        "to the PresentMon 2.x console exe")
    return problems


def telemetry_problems(info: Dict[str, float]) -> List[str]:
    """Readings live tuning cannot do without: it must see what it changes, and see heat."""
    problems = []
    if "STAPM LIMIT" not in info:
        problems.append("cannot read the current TDP limit (no STAPM LIMIT row), so changes could not be "
                        "verified or undone")
    if not any(k in info for k in ("STAPM VALUE", "PPT VALUE SLOW")):
        problems.append("cannot read the APU power draw (no STAPM VALUE row)")
    if info.get("THM VALUE CORE", 0.0) <= 0:
        problems.append("cannot read the core temperature (no THM VALUE CORE row), so the heat limit "
                        "would not work")
    return problems


# -- the OS seam -------------------------------------------------------------

class System:
    """Everything the backend asks of the operating system, in one place so tests can fake it."""

    def __init__(self) -> None:
        _require_windows()

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def is_admin(self) -> bool:
        return is_admin()

    def run(self, cmd: List[str], timeout: float = 10) -> Tuple[int, str]:
        """(exit code, combined output). A command that cannot run or hangs gives (-1, reason)."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, str(e)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def start_presentmon(self, cmd: List[str]):
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def current_mode(self) -> Optional[Tuple[int, int]]:
        return current_mode()

    def set_resolution(self, width: int, height: int) -> bool:
        return set_resolution(width, height)

    def battery_percent(self) -> Optional[int]:
        return battery_status()[0]

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def install_exit_hooks(self, cleanup) -> None:
        _install_exit_hooks(cleanup)


# -- backend ---------------------------------------------------------------

class WindowsBackend:
    """Telemetry and actuators for a Windows handheld. Live mode (read_only=False) refuses to
    start unless it can read the TDP limit, power and temperature, saves the original TDP and
    resolution first, verifies every change it makes, and puts everything back on exit."""

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
        system: Optional[System] = None,
        boost_ratio: float = 1.0,
    ) -> None:
        if not 1.0 <= boost_ratio <= 1.3:
            raise ValueError("boost_ratio must be between 1.0 and 1.3")
        self.sys = system or System()
        problems = startup_problems(ryzenadj, presentmon, self.sys)
        if problems:
            raise RuntimeError("; ".join(problems))

        self.device = device
        self.ryzenadj = ryzenadj
        self.read_only = read_only
        self.boost_ratio = boost_ratio  # short-boost limit as a multiple of the TDP; 1.0 means no boost
        self.notify = notify
        self.settings: Optional[Settings] = None
        self.extras: Dict[str, float] = {}
        self._last_info: Dict[str, float] = {}
        self._frames: Deque[Tuple[float, float]] = deque()  # (monotonic time, ms)
        self._frame_col: Optional[int] = None
        self._lock = threading.Lock()
        self._last_sample = self.sys.monotonic()
        self._last_pct: Optional[int] = None
        self._closed = False

        self._read_info()
        if not read_only:
            blind = telemetry_problems(self._last_info)
            if blind:
                raise RuntimeError("live tuning needs working telemetry: " + "; ".join(blind)
                                   + ". Run `check` and send me the report, or use --dry-run to watch "
                                     "without changing anything.")

        self._pm = self.sys.start_presentmon(
            [presentmon, "--process_name", game, "--output_stdout",
             "--stop_existing_session", "--terminate_on_proc_exit"])
        threading.Thread(target=self._read_presentmon, daemon=True).start()

        if not read_only:
            self._store = restore_store or RestoreStore()
            self._save_restore_point()
            self.sys.install_exit_hooks(self.close)

    # -- Backend protocol

    def sample(self) -> Sample:
        wait = 1.0 - (self.sys.monotonic() - self._last_sample)
        if wait > 0:
            self.sys.sleep(wait)
        now = self.sys.monotonic()
        with self._lock:
            while self._frames and self._frames[0][0] < now - 1.0:
                self._frames.popleft()
            recent = [ms for _, ms in self._frames]
        self._last_sample = now

        fps_avg, fps_low = fps_stats(recent)
        info = self._read_info()
        pct = self.sys.battery_percent()
        if pct is not None:
            self._last_pct = pct
        cap = self.settings.fps_cap if self.settings else 60
        return Sample(
            fps_avg=fps_avg,
            fps_low=fps_low,
            gpu_util=estimate_gpu_util(fps_avg, cap),
            apu_power_w=info.get("STAPM VALUE", info.get("PPT VALUE SLOW", 0.0)),
            temp_c=info.get("THM VALUE CORE", 0.0),  # 0 means unreadable; the controller treats it so
            battery_wh=self.device.battery_wh * (100 if self._last_pct is None else self._last_pct) / 100.0,
        )

    def apply(self, settings: Settings) -> Settings:
        """Applies what it can, verifies each change, and returns what is really in effect."""
        d = self.device
        if not (d.tdp_min_w <= settings.tdp_w <= d.tdp_max_w and 0 <= settings.res_index < len(d.resolutions)):
            raise ValueError(f"refusing out-of-range settings {settings}")
        if self.read_only:
            raise RuntimeError("backend is read-only")

        prev = self.settings
        if prev:
            tdp = prev.tdp_w
        else:
            # compare with the exact limit, not the snapped one: 15 W is not 14 W, so it must be written
            exact = self._read_info().get("STAPM LIMIT")
            close = exact is not None and abs(exact - settings.tdp_w) <= self.TDP_TOLERANCE_W
            tdp = settings.tdp_w if close else None
        res = prev.res_index if prev else self._current_res_index()  # None if the display cannot be read

        if tdp != settings.tdp_w:
            if self._set_tdp(settings.tdp_w):
                tdp = settings.tdp_w
            else:
                tdp = self._effective_tdp(prev.tdp_w if prev else settings.tdp_w)
        if res != settings.res_index:
            w, h = d.resolutions[settings.res_index]
            if self.sys.set_resolution(w, h):
                res = settings.res_index
            else:
                res = self._current_res_index()
                if res is None:
                    res = settings.res_index
                    self.notify("could not read or change the display mode")

        self.settings = Settings(tdp, res, settings.fps_cap)
        return self.settings

    def current_settings(self) -> Optional[Settings]:
        """What the device is set to right now, for starting a dry run from reality."""
        info = self._read_info()
        mode = self.sys.current_mode()
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
        problems = restore_state(self.ryzenadj, state, self.sys) if state else ["no restore point was saved"]
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
        mode = self.sys.current_mode()
        self._store.save(RestoreState(stapm, fast, slow, *(mode or (None, None))))

    def _read_presentmon(self) -> None:
        for line in self._pm.stdout:
            self._ingest(line)

    def _ingest(self, line: str) -> None:
        """One line of PresentMon CSV: the header first, then one frame per row."""
        if self._frame_col is None:
            self._frame_col = frame_time_column(line)
            return
        ms = parse_frame_ms(line, self._frame_col)
        if ms is not None:
            with self._lock:
                self._frames.append((self.sys.monotonic(), ms))

    def _set_tdp(self, tdp_w: int) -> bool:
        mw = tdp_w * 1000
        args = ryzenadj_limit_args(mw, int(mw * self.boost_ratio), mw)
        for attempt in range(2):  # the power table can lag a moment behind the write
            code, _ = self.sys.run([self.ryzenadj] + args)
            if code != 0:
                return False
            self.sys.sleep(0.5 if attempt == 0 else 1.0)
            info = self._read_info()
            if "STAPM LIMIT" not in info or abs(info["STAPM LIMIT"] - tdp_w) <= self.TDP_TOLERANCE_W:
                return True  # verified, or the limit cannot be read back so trust the exit code
        return False

    def _effective_tdp(self, fallback: int) -> int:
        info = self._read_info()
        return self.device.snap_tdp(info["STAPM LIMIT"]) if "STAPM LIMIT" in info else fallback

    def _current_res_index(self) -> Optional[int]:
        mode = self.sys.current_mode()
        if mode is None:
            return None
        exact = self.device.resolution_index(*mode)
        return exact if exact is not None else self.device.nearest_resolution_index(*mode)

    def _read_info(self) -> Dict[str, float]:
        code, out = self.sys.run([self.ryzenadj, "--info"], timeout=3)
        info = parse_ryzenadj_info(out) if code == 0 else {}
        if info:
            self._last_info = info
            self.extras = info
        return self._last_info
