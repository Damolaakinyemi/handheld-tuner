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

## Run it on the Legion Go (Windows 11, elevated prompt)

Needs [PresentMon](https://github.com/GameTechDev/PresentMon) (2.x console build) and
[RyzenAdj](https://github.com/FlyGoat/RyzenAdj) on disk.

```
python -m tuner run --game eldenring.exe --fps 60 --hours 2.5 --presentmon C:\tools\PresentMon.exe --ryzenadj C:\tools\ryzenadj.exe
```

**The Windows backend ([tuner/windows.py](tuner/windows.py)) has never run on real hardware.** It was written
against the PresentMon and RyzenAdj docs, and only its parsing is unit-tested. Expect to fix things on first run.

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
