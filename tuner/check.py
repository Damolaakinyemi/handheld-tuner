"""`python -m tuner check`: finds out what works on this device before the tuner relies on it."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .model import Device
from .windows import (
    battery_status,
    current_mode,
    current_refresh_hz,
    fps_stats,
    frame_time_column,
    is_admin,
    list_modes,
    parse_frame_ms,
    parse_ryzenadj_info,
    set_resolution,
)

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


# -- pure evaluation (unit-tested off Windows) -----------------------------

def evaluate_ryzenadj_info(returncode: int, text: str) -> CheckResult:
    name = "ryzenadj --info"
    if returncode != 0:
        return CheckResult(name, FAIL, f"exited with code {returncode}; needs an admin prompt, and the "
                                       "firmware or driver may block it")
    info = parse_ryzenadj_info(text)
    if not info:
        return CheckResult(name, FAIL, "ran, but no 'NAME | value' rows could be parsed; see the raw output "
                                       "in the report")
    wanted = {"STAPM LIMIT": "TDP read-back", "STAPM VALUE": "APU power", "THM VALUE CORE": "temperature"}
    missing = [f"{key} ({what})" for key, what in wanted.items() if key not in info]
    if missing:
        return CheckResult(name, WARN, f"parsed {len(info)} rows but missing: {', '.join(missing)}. "
                                       f"Rows found: {', '.join(sorted(info))}")
    return CheckResult(name, PASS, f"{len(info)} rows; TDP limit {info['STAPM LIMIT']:.1f} W, "
                                   f"power {info['STAPM VALUE']:.1f} W, core {info['THM VALUE CORE']:.0f} C")


def evaluate_presentmon_output(lines: Sequence[str]) -> CheckResult:
    name = "PresentMon capture"
    if not lines:
        return CheckResult(name, FAIL, "no output at all; is the game running, and is this PresentMon 2.x console?")
    col = frame_time_column(lines[0])
    if col is None:
        return CheckResult(name, FAIL, f"no MsBetweenPresents column in the header: {lines[0].strip()[:200]}")
    frames = [ms for ms in (parse_frame_ms(line, col) for line in lines[1:]) if ms is not None]
    if len(frames) < 10:
        return CheckResult(name, WARN, f"header parsed but only {len(frames)} frames captured; is the game "
                                       "actually rendering?")
    avg, low = fps_stats(frames)
    return CheckResult(name, PASS, f"{len(frames)} frames, {avg:.1f} fps average, {low:.1f} fps 1% low")


def missing_resolutions(device: Device, modes: Sequence[Tuple[int, int, int]]) -> List[Tuple[int, int]]:
    available = {(w, h) for w, h, _hz in modes}
    return [r for r in device.resolutions if r not in available]


def format_report(results: Sequence[CheckResult], sections: Dict[str, str]) -> str:
    lines = ["handheld-tuner hardware check", "=" * 29, ""]
    for r in results:
        lines.append(f"[{r.status.upper():4}] {r.name}: {r.detail}")
    for title, body in sections.items():
        lines += ["", f"--- {title} ---", body.rstrip() or "(empty)"]
    return "\n".join(lines) + "\n"


# -- the checks themselves (Windows only) ---------------------------------

def _run(cmd: List[str], timeout: float = 10) -> Tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def _capture_presentmon(presentmon: str, game: str, seconds: float) -> List[str]:
    proc = subprocess.Popen(
        [presentmon, "--process_name", game, "--output_stdout", "--stop_existing_session",
         "--terminate_on_proc_exit"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    lines: List[str] = []

    def reader():
        for line in proc.stdout:
            lines.append(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(seconds)
    proc.terminate()
    t.join(timeout=2)
    return lines


def run_checks(device: Device, presentmon: str, ryzenadj: str, game: Optional[str],
               test_write: bool) -> Tuple[List[CheckResult], Dict[str, str]]:
    results: List[CheckResult] = []
    sections: Dict[str, str] = {}

    if sys.platform != "win32":
        return [CheckResult("platform", FAIL, "this check only works on Windows")], sections

    results.append(CheckResult("administrator", PASS if is_admin() else FAIL,
                               "elevated" if is_admin() else "not elevated; RyzenAdj needs an admin prompt"))

    # RyzenAdj telemetry
    try:
        code, out = _run([ryzenadj, "--info"])
        sections["ryzenadj --info (raw)"] = out
        results.append(evaluate_ryzenadj_info(code, out))
        info = parse_ryzenadj_info(out)
    except (OSError, subprocess.TimeoutExpired) as e:
        results.append(CheckResult("ryzenadj --info", FAIL, f"could not run {ryzenadj!r}: {e}"))
        info = {}

    # RyzenAdj write: set the limit it already has, so nothing actually changes
    if not test_write:
        results.append(CheckResult("ryzenadj write", SKIP, "pass --test-write to try it (re-sets the current limit)"))
    elif "STAPM LIMIT" not in info:
        results.append(CheckResult("ryzenadj write", SKIP, "current limit unreadable, nothing safe to write"))
    else:
        current = int(round(info["STAPM LIMIT"] * 1000))
        try:
            code, out = _run([ryzenadj, f"--stapm-limit={current}"])
            time.sleep(0.5)
            _, after = _run([ryzenadj, "--info"])
            back = parse_ryzenadj_info(after).get("STAPM LIMIT")
            ok = code == 0 and back is not None and abs(back - current / 1000) <= 0.5
            results.append(CheckResult("ryzenadj write", PASS if ok else FAIL,
                                       f"exit {code}, limit reads {back} W after writing {current / 1000} W"))
        except (OSError, subprocess.TimeoutExpired) as e:
            results.append(CheckResult("ryzenadj write", FAIL, str(e)))

    # PresentMon
    if not game:
        try:
            code, out = _run([presentmon, "--help"])
            results.append(CheckResult("PresentMon runs", PASS if code == 0 else WARN,
                                       f"exit code {code} (pass --game <exe> to test a real capture)"))
        except (OSError, subprocess.TimeoutExpired) as e:
            results.append(CheckResult("PresentMon runs", FAIL, f"could not run {presentmon!r}: {e}"))
    else:
        try:
            lines = _capture_presentmon(presentmon, game, 6)
            sections["PresentMon (first 5 lines)"] = "".join(lines[:5])
            results.append(evaluate_presentmon_output(lines))
        except OSError as e:
            results.append(CheckResult("PresentMon capture", FAIL, f"could not run {presentmon!r}: {e}"))

    # battery
    pct, ac = battery_status()
    if pct is None:
        results.append(CheckResult("battery", WARN, "percentage unknown"))
    else:
        results.append(CheckResult("battery", PASS, f"{pct}%, {'plugged in' if ac else 'on battery'}"
                                   + ("; unplug to test discharge" if ac else "")))

    # display
    modes = list_modes()
    now = current_mode()
    sections["display modes"] = "current: {} @ {} Hz\n".format(now, current_refresh_hz()) + \
        "\n".join(f"{w}x{h} @ {hz} Hz" for w, h, hz in modes)
    missing = missing_resolutions(device, modes)
    results.append(CheckResult("display modes", WARN if missing else PASS,
                               f"missing {missing}" if missing else "all four tuner resolutions are listed"))

    if not test_write:
        results.append(CheckResult("resolution switch", SKIP, "pass --test-write to try it (switches for 3 s)"))
    elif now is None or not modes:
        results.append(CheckResult("resolution switch", SKIP, "current mode unknown"))
    else:
        target = device.resolutions[0] if device.resolutions[0] != now else device.resolutions[1]
        ok = set_resolution(*target)
        time.sleep(3)
        restored = set_resolution(*now)
        results.append(CheckResult("resolution switch", PASS if ok and restored else FAIL,
                                   f"to {target[0]}x{target[1]}: {'ok' if ok else 'failed'}; "
                                   f"back to {now[0]}x{now[1]}: {'ok' if restored else 'FAILED'}"))
    return results, sections
