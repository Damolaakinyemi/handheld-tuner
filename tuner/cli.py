from __future__ import annotations

import argparse
import sys
from statistics import mean

from .controller import Controller, Decision
from .model import LEGION_GO, Goal
from .profiles import ProfileStore
from .runner import run
from .sim import SimGame


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


def cmd_simulate(args) -> int:
    device = LEGION_GO
    goal = _goal(args)
    store = ProfileStore(args.profiles) if args.game else None
    start = store.load(args.game, goal, device) if store else None
    if start:
        print(f"warm start from saved profile: {_describe(device, start)}")

    controller = Controller(goal, device, start=start)
    game = SimGame(device, controller.settings, seed=args.seed)
    result = run(controller, game, args.seconds, _print_decision(device))
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
    controller = Controller(goal, device, start=store.load(args.game, goal, device))
    try:
        backend = WindowsBackend(device, args.game, args.presentmon, args.ryzenadj)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        result = run(controller, backend, args.seconds, _print_decision(device))
    except KeyboardInterrupt:
        result = None
    finally:
        backend.close()
    if result and result.samples and controller.converged:
        tail = result.samples[-60:]
        store.save(
            args.game, goal, controller.settings,
            mean(s.fps_avg for s in tail), mean(s.apu_power_w for s in tail),
        )
        print(f"saved profile for {args.game!r}")
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

    sim = sub.add_parser("simulate", help="run the controller against a simulated game")
    add_goal_args(sim)
    sim.add_argument("--seconds", type=int, default=420)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--game", default=None, help="save/load a profile under this name")
    sim.set_defaults(func=cmd_simulate)

    live = sub.add_parser("run", help="tune a running game on Windows")
    add_goal_args(live)
    live.add_argument("--game", required=True, help="process name, e.g. eldenring.exe")
    live.add_argument("--seconds", type=int, default=24 * 3600)
    live.add_argument("--presentmon", default="PresentMon.exe")
    live.add_argument("--ryzenadj", default="ryzenadj.exe")
    live.set_defaults(func=cmd_run)

    prof = sub.add_parser("profiles", help="list saved profiles")
    prof.add_argument("--profiles", default=None)
    prof.set_defaults(func=cmd_profiles)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
