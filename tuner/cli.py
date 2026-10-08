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
from .service import DEFAULT_TOKEN_PATH, TunerServer, TunerService
from .sim import PacedBackend, SimGame

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


def _backend_factory(args, device):
    if args.simulate:
        def make_sim(game, dry_run):
            sim = SimGame(device, Controller.default_settings(Goal(), device), seed=args.seed)
            return PacedBackend(sim, args.tick)
        return make_sim

    def make_windows(game, dry_run):
        from .windows import WindowsBackend

        return WindowsBackend(device, game, args.presentmon, args.ryzenadj, read_only=dry_run)

    return make_windows


def cmd_serve(args) -> int:
    device = LEGION_GO
    service = TunerService(device, _backend_factory(args, device), ProfileStore(args.profiles))
    try:
        server = TunerServer(service, args.host, args.port, args.token_file)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    mode = "simulated game" if args.simulate else "Windows hardware"
    print(f"tuner service on http://{args.host}:{server.port} ({mode})")
    print(f"API token is in {server.token_path}; see docs/API.md")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        service.stop_session()  # restores the original TDP and resolution
        server.close()
    return 0


def _api(args, method: str, path: str, body=None, stream: bool = False):
    import json
    import urllib.error
    import urllib.request

    try:
        token = Path(args.token_file).read_text().strip()
    except OSError:
        print(f"error: no token at {args.token_file}; is `tuner serve` running?", file=sys.stderr)
        raise SystemExit(1)
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=None if stream else 30)
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("error", e.reason)
        print(f"error {e.code}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"error: cannot reach the service on port {args.port}: {e.reason}", file=sys.stderr)
        raise SystemExit(1)
    return resp if stream else json.loads(resp.read())


def _goal_body(args) -> dict:
    body = {}
    for key, value in (("fps", args.fps), ("hours", args.hours), ("prefer", args.prefer), ("max_temp", args.max_temp)):
        if value is not None:
            body[key] = value
    return body


def cmd_ctl(args) -> int:
    import json

    action = args.action
    if action == "start":
        body = {"game": args.game, "dry_run": args.dry_run, **_goal_body(args)}
        out = _api(args, "POST", "/v1/session", body)
    elif action == "goal":
        out = _api(args, "POST", "/v1/goal", _goal_body(args))
    elif action in ("pause", "resume", "shutdown"):
        out = _api(args, "POST", f"/v1/{action}")
    elif action == "stop":
        out = _api(args, "DELETE", "/v1/session")
    elif action == "watch":
        resp = _api(args, "GET", "/v1/events", stream=True)
        try:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                e = json.loads(line[5:])
                if e["type"] == "sample":
                    s = e["settings"]
                    print(f"{e['fps']:5.1f} fps  {e['power_w']:5.1f} W  {e['temp_c']:3.0f} C  {s['tdp_w']:>2} W {s['resolution']}")
                elif e["type"] == "decision" and e["changed"]:
                    print(f"  -> {e['settings']['tdp_w']} W {e['settings']['resolution']}: {e['reason']}"
                          + ("" if e["applied"] else "  (not applied)"))
                elif e["type"] == "note":
                    print(f"  note: {e['message']}")
        except KeyboardInterrupt:
            pass
        return 0
    else:
        out = _api(args, "GET", "/v1/status")
    print(json.dumps(out, indent=2))
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

    serve = sub.add_parser("serve", help="run the background service with a local HTTP API")
    add_tool_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="loopback addresses only")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token-file", default=str(DEFAULT_TOKEN_PATH))
    serve.add_argument("--profiles", default=None)
    serve.add_argument("--simulate", action="store_true", help="use a simulated game instead of hardware")
    serve.add_argument("--tick", type=float, default=1.0, help="seconds per simulated sample")
    serve.add_argument("--seed", type=int, default=1)
    serve.set_defaults(func=cmd_serve)

    ctl = sub.add_parser("ctl", help="control a running service")
    ctl.add_argument("action", choices=("status", "start", "goal", "pause", "resume", "stop", "watch", "shutdown"))
    ctl.add_argument("--port", type=int, default=8765)
    ctl.add_argument("--token-file", default=str(DEFAULT_TOKEN_PATH))
    ctl.add_argument("--game", help="start: exe name, e.g. eldenring.exe")
    ctl.add_argument("--fps", type=int, default=None)
    ctl.add_argument("--hours", type=float, default=None)
    ctl.add_argument("--max-temp", dest="max_temp", type=float, default=None)
    ctl.add_argument("--prefer", choices=("quality", "battery"), default=None)
    ctl.add_argument("--dry-run", action="store_true")
    ctl.set_defaults(func=cmd_ctl)

    prof = sub.add_parser("profiles", help="list saved profiles")
    prof.add_argument("--profiles", default=None)
    prof.set_defaults(func=cmd_profiles)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
