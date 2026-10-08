"""Turns service events into what the overlay should show. No GUI code, so it is easy to test.

All times are monotonic seconds passed in by the caller.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

TOAST_SECONDS = 2.5
CHIP_HIGHLIGHT_SECONDS = 3.0
SPARK_POINTS = 30

_FRIENDLY = [
    (r"^could not apply", "Hardware refused a change"),
    (r"probe missed", "Went too far, backing off"),
    (r"\d+C over \d+C limit", "Running hot. Lowering power"),
    (r"below target, raising", "Below target. Raising the power limit"),
    (r"below target, lowering", "Below target. Lowering resolution"),
    (r"higher resolution", "Spare headroom. Trying a sharper picture"),
    (r"over .*budget", "Over your battery budget. Trimming power"),
    (r"trimming power", "Spare headroom. Trimming power"),
    (r"unreachable", "Can't reach the target. Doing its best"),
    (r"waiting for frames", "Waiting for the game…"),
    (r"temperature unreadable", "Can't read the temperature. Keeping power steady"),
    (r"on target", "On target"),
]


def friendly(reason: str) -> str:
    """Plain-language version of a controller reason, without any dry-run prefix."""
    reason = reason.removeprefix("would change: ")
    for pattern, text in _FRIENDLY:
        if re.search(pattern, reason):
            return text
    return reason


@dataclass(frozen=True)
class Panel:
    fps: str
    status: str
    tone: str
    detail: str
    tdp: str
    tdp_changed: bool
    res: str
    res_changed: bool
    spark: Tuple[float, ...]
    decision: str
    goal: str
    badge: Optional[str]


@dataclass(frozen=True)
class View:
    visible: bool
    expanded: bool
    tone: str  # ok | warn | muted | error
    headline: str
    toast: Optional[str]
    panel: Optional[Panel]


class OverlayModel:
    def __init__(self, quiet: bool = False, start_expanded: bool = False) -> None:
        self.quiet = quiet  # hide the pill when offline or idle
        self.expanded = start_expanded
        self.online = False
        self.offline_reason = "waiting for the tuner"
        self.state = "idle"
        self.error: Optional[str] = None
        self.dry_run = False
        self.goal: Optional[dict] = None
        self.device: dict = {}
        self.sample: Optional[dict] = None
        self.decision_text = ""
        self.unreachable = False
        self.spark: Deque[float] = deque(maxlen=SPARK_POINTS)
        self._toast: Optional[Tuple[str, float]] = None
        self._tdp: Optional[int] = None
        self._res: Optional[str] = None
        self._tdp_changed_at = -1e9
        self._res_changed_at = -1e9

    # -- input

    def toggle(self) -> None:
        self.expanded = not self.expanded

    def apply(self, event: dict, now: float) -> None:
        kind = event.get("type")
        if kind == "_online":
            self.online = True
        elif kind == "_offline":
            self.online = False
            self.offline_reason = event.get("reason") or "not reachable"
            self.sample = None
        elif kind == "state":
            self._on_state(event, now)
        elif kind == "sample":
            self._on_sample(event, now)
        elif kind == "decision":
            self._on_decision(event, now)
        elif kind == "note":
            self._on_note(event, now)

    def _on_state(self, e: dict, now: float) -> None:
        self.online = True
        self.state = e.get("state", "idle")
        self.error = e.get("error")
        self.dry_run = bool(e.get("dry_run"))
        self.goal = e.get("goal")
        self.device = e.get("device") or self.device
        if self.state == "idle":
            self.sample = None
            self.spark.clear()
            self._tdp = self._res = None
            self.decision_text = ""
            self.unreachable = False
            self._toast = None
        elif self.state == "error":
            self._toast = (self.error or "Tuner stopped", now)

    def _on_sample(self, e: dict, now: float) -> None:
        self.sample = e
        self.spark.append(float(e.get("power_w", 0)))
        s = e.get("settings") or {}
        tdp, res = s.get("tdp_w"), s.get("resolution")
        if self._tdp is not None and tdp != self._tdp:
            self._tdp_changed_at = now
        if self._res is not None and res != self._res:
            self._res_changed_at = now
        self._tdp, self._res = tdp, res

    def _on_decision(self, e: dict, now: float) -> None:
        reason = e.get("reason", "")
        text = friendly(reason)
        self.unreachable = "unreachable" in reason
        if e.get("changed"):
            self.decision_text = text
            prefix = "" if e.get("applied") else "Would: "
            self._toast = (prefix + text, now)
        elif reason.startswith("could not apply"):
            self.decision_text = text
            self._toast = (text, now)
        else:
            self.decision_text = text

    def _on_note(self, e: dict, now: float) -> None:
        message = e.get("message", "")
        if message.startswith("saved profile"):
            message = "Saved your settings for this game"
        self._toast = (message, now)

    # -- output

    def view(self, now: float) -> View:
        toast = self._toast[0] if self._toast and now - self._toast[1] < TOAST_SECONDS else None

        if not self.online:
            text = "Tuner offline"
            return View(not self.quiet, False, "muted", text, None, None)
        if self.state == "idle":
            return View(not self.quiet, False, "muted", "No game being tuned", toast, None)
        if self.state == "error":
            return View(True, False, "error", "Tuner stopped", toast, None)

        s = self.sample
        goal_fps = (self.goal or {}).get("fps", 0)
        paused = self.state == "paused"
        if s is None:
            tone, headline = "muted", "Starting…"
        else:
            on_target = s["fps"] >= goal_fps * 0.97 and not self.unreachable
            tone = "muted" if paused else ("ok" if on_target else "warn")
            headline = f"{s['fps']:.0f} fps · {s['power_w']:.1f} W"
            if paused:
                headline = "Paused · " + headline
            elif self.dry_run:
                headline = "Dry run · " + headline
        panel = self._panel(now, tone) if self.expanded and s is not None else None
        return View(True, panel is not None, tone, headline, toast, panel)

    def _panel(self, now: float, tone: str) -> Panel:
        s = self.sample
        settings = s["settings"]
        recent = list(self.spark)[-5:]
        power = sum(recent) / len(recent)
        base = self.device.get("base_power_w", 4.0)
        hours = s["battery_wh"] / (power + base) if power + base > 0 else 0.0
        goal = self.goal or {}
        goal_text = f"Goal {goal.get('fps', '?')} fps"
        if goal.get("hours"):
            goal_text += f" · {goal['hours']:g} h"
        if goal.get("prefer") == "battery":
            goal_text += " · save power"
        if self.state == "paused":
            status, badge = "Paused", "PAUSED"
        elif self.dry_run:
            status, badge = ("On target" if tone == "ok" else "Adjusting"), "DRY RUN"
        else:
            status, badge = ("On target" if tone == "ok" else "Adjusting"), None
        return Panel(
            fps=f"{s['fps']:.0f}",
            status=status,
            tone=tone,
            detail=f"{s['power_w']:.1f} W · {s['temp_c']:.0f} °C · {hours:.1f} h left",
            tdp=f"{settings['tdp_w']} W",
            tdp_changed=now - self._tdp_changed_at < CHIP_HIGHLIGHT_SECONDS,
            res=settings["resolution"].replace("x", "×"),
            res_changed=now - self._res_changed_at < CHIP_HIGHLIGHT_SECONDS,
            spark=tuple(self.spark),
            decision=self.decision_text or "Watching your game…",
            goal=goal_text,
            badge=badge,
        )
