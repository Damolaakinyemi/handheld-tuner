# handheld-tuner

A goal-based game tuner for the Lenovo Legion Go. You say what you want ("60 fps for 2.5 hours"), and it
adjusts TDP and display resolution while you play until it gets there, then remembers the answer per game.

## Try it (any OS, no dependencies, Python 3.9+)

```
python3 -m tuner simulate --fps 60 --hours 2.5          # watch the controller work a simulated game
python3 -m tuner simulate --fps 60 --prefer battery --game demo.exe   # saves a profile when converged
python3 -m tuner profiles
python3 -m unittest discover -s tests -t .
```

## Get it onto the Legion Go

1. Install [Python 3.9 or newer](https://www.python.org/downloads/) (tick "Add python.exe to PATH") and
   [Git](https://git-scm.com/download/win).
2. In a terminal: `git clone https://github.com/Damolaakinyemi/handheld-tuner.git`. The repo is private, so Git
   asks you to sign in to GitHub in a browser the first time. No Git? Use the green **Code** button, then
   **Download ZIP**, and unzip it.
3. Update later with `git pull` inside the folder.

There is nothing to `pip install`. The tuner and overlay use only Python's standard library (the overlay
needs Tk, which the python.org installer includes).

## Run it on the Legion Go (Windows 11, elevated prompt)

Needs [PresentMon](https://github.com/GameTechDev/PresentMon) (2.x console build) and
[RyzenAdj](https://github.com/FlyGoat/RyzenAdj) on disk. Go in this order:

**1. Hardware check.** Finds out what works before the tuner relies on it, and saves a report to send back.

```
python -m tuner check --presentmon C:\tools\PresentMon.exe --ryzenadj C:\tools\ryzenadj.exe --game eldenring.exe
python -m tuner check ... --test-write     # also re-applies the current TDP and flips resolution for 3 s
```

**2. Dry run.** Reads telemetry and logs what the tuner *would* do, changing nothing. Play 10 minutes, then
keep the CSV (default `~/.handheld-tuner/logs/`). This is the data needed to calibrate against real games.

```
python -m tuner run --dry-run --game eldenring.exe --fps 60 --hours 2.5 --presentmon ... --ryzenadj ...
```

**3. Live.** Same command without `--dry-run`.

**Safety.** Every TDP and resolution change is verified by reading it back. If one is refused, the tuner stops
asking for it and works around it. The original TDP limits and resolution are saved on start and restored on
exit, on Ctrl+C, and when the console window is closed. If the machine dies mid-run, `python -m tuner restore`
puts them back (TDP also resets on sleep or reboot, and the resolution change is never written to the registry).

**The Windows backend ([tuner/windows.py](tuner/windows.py)) has never run on real hardware.** It was written
against the PresentMon and RyzenAdj docs, and only its parsing and decision logic are unit-tested.
Expect to fix things on first run.

## Run it as a background service

`serve` runs the tuner as a service with a local HTTP API, so an overlay, tray app, or anything else can drive
it. Full reference in [docs/API.md](docs/API.md).

```
python -m tuner serve --simulate                  # fake game, runs anywhere; build frontends against this
python -m tuner serve --presentmon ... --ryzenadj ...   # real hardware (Windows, admin)
python -m tuner ctl start --game eldenring.exe --fps 60 --hours 2.5   # or --dry-run
python -m tuner ctl watch | status | goal --fps 40 | pause | resume | stop | shutdown
```

It only listens on loopback, needs a per-run bearer token (`~/.handheld-tuner/token`), checks the Host header,
and takes JSON only, because it can change TDP and resolution. Stopping a session, or the service, puts the
original settings back. Profiles are saved automatically when a session converges.

## The overlay

`overlay/` is a small always-on-top window that reads the service's event stream. It is a separate program:
it only talks to the service over the API (see [docs/API.md](docs/API.md)), so it can crash or restart without
touching your game or your settings.

```
python -m tuner serve --simulate     # or the real service
python -m overlay                    # pill in the top-left corner
python -m overlay --corner top-right --scale 1.2 --quiet
```

- **Pill**: status dot, fps and power. A line under it announces each change for about 2.5 s, e.g.
  "Below target. Lowering resolution". Dim when offline or idle, `--quiet` hides it then.
- **Panel**: fps, power, temperature, estimated battery left, the current TDP and resolution (a chip turns blue
  for 3 s after the tuner changes it), a power graph, the last decision, and your goal.
- **Open and close the panel** by holding **L3+R3** for half a second (or Ctrl+Alt+O). It reads the controller
  without consuming input. On Windows the window is click-through, so it cannot be clicked.
- It reconnects by itself, including after a service restart, which issues a new token.

**Not yet tested on Windows or the Legion Go.** Everything that draws and decides (the model, the event reader,
the Tk drawing) runs and is tested off Windows. Click-through, staying above the game, DPI scaling and the XInput
chord are Windows-only code that has never run, so expect to adjust them. Like any overlay it only shows over
borderless or windowed games, not exclusive fullscreen.

## How the controller decides

Every 12 s window it compares frame rate, GPU headroom, power and temperature with the goal, then makes
one change at a time:

| Situation | Move |
| --- | --- |
| Over the temperature limit | Lower TDP, and cap TDP for a while (heat follows TDP, not resolution) |
| Below target fps | Raise TDP if the battery budget allows, else lower resolution |
| On target but over the battery budget | Trim TDP, then resolution |
| On target with spare GPU | `quality`: raise resolution; `battery`: lower TDP |

Moves that save power or spend headroom are probes: if the next window misses the target, the controller
reverts and refuses to retry that setting for a while. Once nothing changes for four windows it counts as
converged, and the result is saved per game, goal and preference for a warm start next time.

## Known limits

- **No fps limiter yet.** The design pins fps to the target so headroom reads as low GPU load. Without a
  limiter the Windows backend infers headroom from `target / uncapped fps`, so vsync-locked games look fully
  loaded and are never trimmed. Next step is Radeon Chill through AMD's ADLX SDK, or RTSS.
- **Resolution only helps games that follow the desktop resolution** (borderless or driver-scaled). Games
  with their own fullscreen mode ignore it.
- **Legion Go only**, with TDP 8-30 W in 2 W steps. Limits are in [tuner/model.py](tuner/model.py).
- The simulator's numbers are made up to behave plausibly. Real tuning constants (window length, thresholds,
  the power-per-TDP-watt estimate) need calibrating on the device.
