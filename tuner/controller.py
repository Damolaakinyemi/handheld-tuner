from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import mean
from typing import Dict, List, Optional, Tuple

from .model import Device, Goal, Sample, Settings


@dataclass(frozen=True)
class WindowStats:
    fps_avg: float
    fps_low: float
    gpu_util: float
    power_w: float
    temp_c: float
    battery_wh: float


@dataclass(frozen=True)
class Decision:
    settings: Settings
    reason: str
    changed: bool
    stats: WindowStats


class Controller:
    """Closed-loop tuner: feed it one Sample per second, apply any Decision it returns.

    The fps cap is pinned to the goal, so spare GPU capacity shows up as low
    utilisation rather than extra fps. The tuner spends that headroom according
    to goal.prefer, and makes speculative moves one step at a time, reverting
    and temporarily banning any that miss the target.
    """

    FPS_TOLERANCE = 0.97
    LOW_RATIO = 0.80
    HEADROOM_UTIL = 0.80
    TDP_DOWN_UTIL = 0.85
    SATURATED_UTIL = 0.97
    BUDGET_SLACK = 1.03
    THERMAL_BAN_FACTOR = 4  # heat changes slowly, so don't retry a hot setting soon
    WATTS_PER_TDP_WATT = 0.9  # rough marginal APU draw when GPU-bound
    MIN_LIVE_FPS = 1.0  # a second with fewer frames than this is a loading screen, not gameplay

    def __init__(
        self,
        goal: Goal,
        device: Device,
        start: Optional[Settings] = None,
        window: int = 12,
        settle: int = 4,
        ban_windows: int = 8,
        converge_windows: int = 4,
    ) -> None:
        self.goal = goal
        self.device = device
        self.window = window
        self.settle = settle
        self.ban_windows = ban_windows
        self.converge_windows = converge_windows

        base = start or self.default_settings(goal, device)
        self.settings = replace(base, fps_cap=goal.target_fps)

        self._samples: List[Sample] = []
        self._settle_left = 0
        self._windows = 0
        self._stable = 0
        self._bans: Dict[Settings, int] = {}
        self._prev: Optional[Settings] = None
        self._last_kind: Optional[str] = None  # "probe", "thermal", "perf" or None
        self._tdp_ceiling: Optional[int] = None  # set when hot; heat tracks TDP, not resolution
        self._ceiling_expires = 0
        self._blocked_tdp = set()  # values the hardware refused to apply
        self._blocked_res = set()
        self._temp_known = True  # a temperature of 0 means the sensor could not be read

    @staticmethod
    def default_settings(goal: Goal, device: Device) -> Settings:
        tdp = device.snap_tdp(15)
        res = min(2, len(device.resolutions) - 1)
        return Settings(tdp, res, goal.target_fps)

    @property
    def converged(self) -> bool:
        return self._stable >= self.converge_windows

    def power_budget_w(self, battery_wh: float) -> Optional[float]:
        if not self.goal.runtime_hours:
            return None
        return max(0.0, battery_wh / self.goal.runtime_hours - self.device.base_power_w)

    def observe(self, sample: Sample) -> Optional[Decision]:
        """Returns a Decision each time a window completes, otherwise None."""
        if self._settle_left > 0:
            self._settle_left -= 1
            return None
        self._samples.append(sample)
        if len(self._samples) < self.window:
            return None
        stats, live = _summarise(self._samples, self.MIN_LIVE_FPS)
        self._samples = []
        self._windows += 1
        if not live and stats.temp_c <= self.goal.max_temp_c:
            # menus, loading screens or a wrong process name: do not mistake silence for a slow game
            return Decision(self.settings, "waiting for frames from the game", False, stats)
        return self._decide(stats)

    def reject(self) -> None:
        """Undo the move from the last Decision, e.g. in dry-run where nothing is applied."""
        if self._prev is not None:
            self.settings = self._prev
        self._prev = None
        self._last_kind = None
        self._settle_left = 0
        self._stable = 0

    def report_failure(self, requested: Settings, actual: Settings) -> None:
        """The backend could not apply `requested` and is really at `actual`.

        Each dimension that did not take effect is blocked for the rest of the session,
        so the controller works around it instead of asking again.
        """
        if requested.res_index != actual.res_index:
            self._blocked_res.add(requested.res_index)
        if requested.tdp_w != actual.tdp_w:
            self._blocked_tdp.add(requested.tdp_w)
        self.settings = replace(actual, fps_cap=self.goal.target_fps)
        self._prev = None
        self._last_kind = None
        self._samples = []
        self._settle_left = self.settle
        self._stable = 0

    # -- decision logic -------------------------------------------------

    def _decide(self, st: WindowStats) -> Decision:
        goal, cur = self.goal, self.settings
        self._temp_known = st.temp_c > 0
        budget = self.power_budget_w(st.battery_wh)
        hot = st.temp_c > goal.max_temp_c
        meets = (
            st.fps_avg >= goal.target_fps * self.FPS_TOLERANCE
            and st.fps_low >= goal.target_fps * self.LOW_RATIO
        )
        over = budget is not None and st.power_w > budget * self.BUDGET_SLACK

        if hot:
            self._tdp_ceiling = cur.tdp_w - self.device.tdp_step_w
            self._ceiling_expires = self._windows + self.ban_windows * self.THERMAL_BAN_FACTOR
            cand = self._first_free(self._tdp(cur, -1), self._res(cur, -1))
            return self._move(cand, "thermal", f"{st.temp_c:.0f}C over {goal.max_temp_c:.0f}C limit", st)

        if not meets:
            if self._last_kind == "probe" and self._prev is not None:
                self._ban(cur)
                return self._move(self._prev, None, "probe missed the fps target, reverting", st)
            within = budget is None or st.power_w + self._step_watts() <= budget
            cand = self._first_free(
                self._tdp(cur, +1) if within else None,
                self._res(cur, -1),
                self._tdp(cur, +1),  # fps outranks the battery goal as a last resort
            )
            if cand is None:
                if not self._temp_known:
                    return self._hold("temperature unreadable, not raising power", st)
                return self._hold(f"target unreachable at {st.fps_avg:.0f}fps, best effort", st)
            if cand.tdp_w > cur.tdp_w:
                return self._move(cand, "perf", f"{st.fps_avg:.0f}fps below target, raising power", st)
            return self._move(cand, "perf", f"{st.fps_avg:.0f}fps below target, lowering resolution", st)

        if over:
            if st.gpu_util < self.SATURATED_UTIL:
                order = (self._tdp(cur, -1), self._res(cur, -1))
            else:
                order = (self._res(cur, -1), self._tdp(cur, -1))
            cand = self._first_free(*order)
            if cand is None:
                return self._hold("over power budget but nothing left to trim", st)
            return self._move(cand, "probe", f"{st.power_w:.1f}W over {budget:.1f}W budget", st)

        if st.gpu_util < self.HEADROOM_UTIL:
            cand, why = self._spend_headroom(st)
            if cand is not None:
                return self._move(cand, "probe", why, st)

        return self._hold("on target", st)

    def _spend_headroom(self, st: WindowStats):
        cur = self.settings
        free = 1.0 / max(st.gpu_util, 0.05)  # how many times heavier a frame could get
        if self.goal.prefer == "quality":
            up = self._res(cur, +1)
            if up is not None and self._is_free(up):
                ratio = self.device.pixels(up.res_index) / self.device.pixels(cur.res_index)
                if free > ratio * 1.05:
                    return up, f"{st.gpu_util:.0%} GPU busy, trying higher resolution"
        down = self._tdp(cur, -1)
        if down is not None and self._is_free(down) and st.gpu_util < self.TDP_DOWN_UTIL:
            return down, f"{st.gpu_util:.0%} GPU busy, trimming power"
        return None, ""

    # -- helpers --------------------------------------------------------

    def _step_watts(self) -> float:
        return self.device.tdp_step_w * self.WATTS_PER_TDP_WATT

    def _tdp(self, s: Settings, direction: int) -> Optional[Settings]:
        tdp = s.tdp_w + direction * self.device.tdp_step_w
        if not self.device.tdp_min_w <= tdp <= self.device.tdp_max_w or tdp in self._blocked_tdp:
            return None
        if direction > 0 and not self._temp_known:
            return None  # never add heat while blind to it
        if direction > 0 and self._tdp_ceiling is not None and self._windows < self._ceiling_expires:
            if tdp > self._tdp_ceiling:
                return None
        return replace(s, tdp_w=tdp)

    def _res(self, s: Settings, direction: int) -> Optional[Settings]:
        idx = s.res_index + direction
        if not 0 <= idx < len(self.device.resolutions) or idx in self._blocked_res:
            return None
        return replace(s, res_index=idx)

    def _ban(self, s: Settings) -> None:
        self._bans[s] = self._windows + self.ban_windows

    def _is_free(self, s: Settings) -> bool:
        return self._bans.get(s, 0) <= self._windows

    def _first_free(self, *cands: Optional[Settings]) -> Optional[Settings]:
        for c in cands:
            if c is not None and self._is_free(c):
                return c
        return None

    def _move(self, cand: Optional[Settings], kind: Optional[str], reason: str, st: WindowStats) -> Decision:
        if cand is None:
            return self._hold(reason, st)
        self._prev = self.settings
        self._last_kind = kind
        self.settings = cand
        self._settle_left = self.settle
        self._stable = 0
        return Decision(cand, reason, True, st)

    def _hold(self, reason: str, st: WindowStats) -> Decision:
        self._last_kind = None
        self._stable += 1
        return Decision(self.settings, reason, False, st)


def _summarise(samples: List[Sample], min_live_fps: float) -> Tuple[WindowStats, bool]:
    """Stats for one window, and whether enough of it was real gameplay to act on.

    Seconds without frames are left out of the fps, power and headroom averages, so a short
    menu or loading screen cannot drag a healthy window below target. Temperature is the
    maximum over every second, because heat does not pause when the game does.
    """
    live = [s for s in samples if s.fps_avg >= min_live_fps]
    enough = len(live) * 2 >= len(samples)
    basis = live if enough else samples
    return WindowStats(
        fps_avg=mean(s.fps_avg for s in live) if enough else 0.0,
        fps_low=mean(s.fps_low for s in live) if enough else 0.0,
        gpu_util=mean(s.gpu_util for s in basis),
        power_w=mean(s.apu_power_w for s in basis),
        temp_c=max(s.temp_c for s in samples),
        battery_wh=samples[-1].battery_wh,
    ), enough
