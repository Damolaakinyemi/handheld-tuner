from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from statistics import mean

from .controller import Controller, Decision
from .log import CsvLog
from .model import LEGION_GO, Goal
from .profiles import ProfileStore
from .restore import RestoreStore
from .runner import run
from .sim import SimGame

LOG_DIR = Path.home() / ".handheld-tuner" / "logs"


def _goal(args) -> Goal:
    return Goal(
        target_fps=args.fps,
        runtime_hours=args.hours,
        max_temp_c=args.max_temp,
        prefer=args.prefer,
    )


def _describe(device, s) -> str:
    w, h = device.resolutions[s.res_index]
    return f"{s.tdp_w:>2}W {w}x{h}"


def _print_decision(device):
    def show(t: int, d: Decision) -> None:
        st = d.stats
        arrow = "->" if d.changed else "  "
        print(
            f"t={t:>4}s  {st.fps_avg:5.1f}fps (low {st.fps_low:5.1f})  gpu {st.gpu_util:4.0%}  "
            f"{st.power_w:5.1f}W  {st.temp_c:3.0f}C  {arrow} {_describe(device, d.settings):<14} {d.reason}"
        )

    return show


def _summary(device, goal, result, controller) -> None:
    tail = result.samples[-60:]
    power = mean(s.apu_power_w for s in tail)
    wh = result.samples[-1].battery_wh
    print()
    print(f"final settings : {_describe(device, controller.settings)}  converged={controller.converged}")
    print(f"last 60 s      : {mean(s.fps_avg for s in tail):.1f} fps, {power:.1f} W APU")
    print(f"projected life : {wh / (power + device.base_power_w):.1f} h from {wh:.1f} Wh remaining")


def _log_path(args) -> Path:
    if args.log:
        return Path(args.log)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", args.game or "session")
    return LOG_DIR / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.csv"


def cmd_simulate(args) -> int:
    device = LEGION_GO
    goal = _goal(args)
    store = ProfileStore(args.profiles) if args.game else None
    start = store.load(args.game, goal, device) if store else None
    if start:
        print(f"warm start from saved profile: {_describe(device, start)}")

    controller = Controller(goal, device, start=start)
    game = SimGame(device, controller.settings, seed=args.seed)
    log = CsvLog(args.log, device) if args.log else None
    try:
        result = run(controller, game, args.seconds, _print_decision(device), print, log)
    finally:
        if log:
            log.close()
    _summary(device, goal, result, controller)

    if store and controller.converged:
        tail = result.samples[-60:]
        store.save(
            args.game, goal, controller.settings,
            mean(s.fps_avg for s in tail), mean(s.apu_power_w for s in tail),
        )
        print(f"saved profile for {args.game!r}")
    return 0


def cmd_profiles(args) -> int:
    entries = ProfileStore(args.profiles).all()
    if not entries:
        print("no saved profiles")
    for key, e in sorted(entries.items()):
        w, h = LEGION_GO.resolutions[e["res_index"]]
        print(f"{key:<50} {e['tdp_w']}W {w}x{h}  {e['fps']} fps @ {e['power_w']} W")
    return 0


def cmd_run(args) -> int:
    from .windows import WindowsBackend

    device = LEGION_GO
    goal = _goal(args)
    store = ProfileStore(args.profiles)
    try:
        backend = WindowsBackend(device, args.game, args.presentmon, args.ryzenadj, read_only=args.dry_run)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # a dry run judges the settings the device really has; a live run starts from the saved profile
    start = backend.current_settings() if args.dry_run else store.load(args.game, goal, device)
    if args.dry_run and start is None:
        print("note: could not read the current TDP or resolution, so the log assumes defaults for them")
    controller = Controller(goal, device, start=start)
    log_path = _log_path(args)
    log = CsvLog(log_path, device)
    print(f"{'DRY RUN, nothing will be changed. ' if args.dry_run else ''}logging to {log_path}")

    result = None
    try:
        result = run(controller, backend, args.seconds, _print_decision(device), print, log, args.dry_run)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        log.close()
        backend.close()
    if result and not args.dry_run and result.samples and controller.converged:
        tail = result.samples[-60:]
        store.save(
            args.game, goal, controller.settings,
            mean(s.fps_avg for s in tail), mean(s.apu_power_w for s in tail),
        )
        print(f"saved profile for {args.game!r}")
    return 0


def cmd_check(args) -> int:
    from .check import FAIL, format_report, run_checks

    results, sections = run_checks(LEGION_GO, args.presentmon, args.ryzenadj, args.game, args.test_write)
    report = format_report(results, sections)
    print(report)
    Path(args.report).write_text(report)
    print(f"report saved to {Path(args.report).resolve()}")
    return 1 if any(r.status == FAIL for r in results) else 0


def cmd_restore(args) -> int:
    from .windows import restore_state

    store = RestoreStore()
    state = store.load()
    if state is None:
        print("nothing to restore")
        return 0
    try:
        problems = restore_state(args.ryzenadj, state)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if problems:
        print("could not fully restore: " + "; ".join(problems))
        return 1
    store.clear()
    print("original TDP limits and resolution restored")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tuner", description="Goal-based game tuner for handhelds")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_goal_args(sp):
        sp.add_argument("--fps", type=int, default=60, help="target fps")
        sp.add_argument("--hours", type=float, default=None, help="target battery runtime in hours")
        sp.add_argument("--max-temp", type=float, default=85.0)
        sp.add_argument("--prefer", choices=("quality", "battery"), default="quality")
        sp.add_argument("--profiles", default=None, help="profile file (default ~/.handheld-tuner/profiles.json)")
        sp.add_argument("--log", default=None, help="write a per-second CSV log here")

    def add_tool_args(sp):
        sp.add_argument("--presentmon", default="PresentMon.exe")
        sp.add_argument("--ryzenadj", default="ryzenadj.exe")

    sim = sub.add_parser("simulate", help="run the controller against a simulated game")
    add_goal_args(sim)
    sim.add_argument("--seconds", type=int, default=420)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--game", default=None, help="save/load a profile under this name")
    sim.set_defaults(func=cmd_simulate)

    live = sub.add_parser("run", help="tune a running game on Windows")
    add_goal_args(live)
    add_tool_args(live)
    live.add_argument("--game", required=True, help="process name, e.g. eldenring.exe")
    live.add_argument("--seconds", type=int, default=24 * 3600)
    live.add_argument("--dry-run", action="store_true",
                      help="log telemetry and what the tuner would do, but change nothing")
    live.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="test which parts of the Windows backend work on this device")
    add_tool_args(check)
    check.add_argument("--game", default=None, help="a running game's exe, to test a PresentMon capture")
    check.add_argument("--test-write", action="store_true",
                       help="also try changing settings: re-applies the current TDP, switches resolution for 3 s")
    check.add_argument("--report", default="tuner-check.txt", help="where to save the report")
    check.set_defaults(func=cmd_check)

    restore = sub.add_parser("restore", help="put back the original TDP and resolution after a crashed run")
    restore.add_argument("--ryzenadj", default="ryzenadj.exe")
    restore.set_defaults(func=cmd_restore)

    prof = sub.add_parser("profiles", help="list saved profiles")
    prof.add_argument("--profiles", default=None)
    prof.set_defaults(func=cmd_profiles)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
