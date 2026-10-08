import argparse
import sys
from pathlib import Path

from .client import DEFAULT_TOKEN_PATH


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="overlay", description="In-game overlay for the tuner service")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token-file", default=str(DEFAULT_TOKEN_PATH))
    p.add_argument("--corner", choices=("top-left", "top-right", "bottom-left", "bottom-right"), default="top-left")
    p.add_argument("--scale", type=float, default=1.0, help="size multiplier on top of automatic screen scaling")
    p.add_argument("--margin", type=int, default=14, help="gap from the screen edge")
    p.add_argument("--quiet", action="store_true", help="hide the overlay while offline or idle")
    p.add_argument("--start-expanded", action="store_true")
    p.add_argument("--hold-seconds", type=float, default=0.5,
                   help="how long to hold L3+R3 (or Ctrl+Alt+O) to open and close the panel")
    p.add_argument("--no-input", action="store_true", help="ignore the controller chord and keyboard shortcut")
    p.add_argument("--click-toggle", action="store_true",
                   help="click the overlay to open it (default off on Windows, where it is click-through)")
    args = p.parse_args(argv)

    if not 0.3 <= args.scale <= 4:
        p.error("--scale must be between 0.3 and 4")
    if not Path(args.token_file).parent.exists():
        print(f"note: {args.token_file} does not exist yet; waiting for `tuner serve`", file=sys.stderr)

    from .app import OverlayApp  # imported late so --help works without a display

    OverlayApp(
        port=args.port, token_file=args.token_file, corner=args.corner, scale=args.scale,
        margin=args.margin, quiet=args.quiet, start_expanded=args.start_expanded,
        hold=args.hold_seconds, use_input=not args.no_input,
        click_toggle=args.click_toggle or sys.platform != "win32",
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
