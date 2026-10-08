from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from .controller import Controller, Decision
from .model import Sample, Settings


class Backend(Protocol):
    """Telemetry source and settings applier. sample() should block for ~1 s on real hardware."""

    def sample(self) -> Sample: ...

    def apply(self, settings: Settings) -> Settings:
        """Apply what it can and return the settings actually in effect afterwards."""
        ...


@dataclass
class RunResult:
    samples: List[Sample] = field(default_factory=list)
    decisions: List[Tuple[int, Decision]] = field(default_factory=list)


def run(
    controller: Controller,
    backend: Backend,
    seconds: int,
    on_decision: Optional[Callable[[int, Decision], None]] = None,
    on_note: Optional[Callable[[str], None]] = None,
    log=None,
    dry_run: bool = False,
) -> RunResult:
    """Drive the loop. With dry_run nothing is applied: decisions are logged as
    'would ...' and then undone, so the controller keeps judging the settings that
    are really in effect."""
    note = on_note or (lambda _msg: None)
    if not dry_run:
        _apply(controller, backend, controller.settings, note)

    result = RunResult()
    for t in range(1, seconds + 1):
        sample = backend.sample()
        in_effect = controller.settings
        result.samples.append(sample)
        decision = controller.observe(sample)

        if decision is not None and decision.changed:
            if dry_run:
                controller.reject()
                decision = replace(decision, reason=f"would change: {decision.reason}")
            else:
                actual = backend.apply(decision.settings)
                if actual != decision.settings:
                    controller.report_failure(decision.settings, actual)
                    decision = replace(
                        decision, settings=actual, changed=False,
                        reason=f"could not apply ({decision.reason}), staying put",
                    )

        if decision is not None:
            result.decisions.append((t, decision))
            if on_decision:
                on_decision(t, decision)
        if log:
            extras: Dict[str, float] = getattr(backend, "extras", None) or {}
            log.write(t, sample, in_effect, decision.reason if decision else "", extras)
    return result


def _apply(controller: Controller, backend: Backend, wanted: Settings, note) -> None:
    actual = backend.apply(wanted)
    if actual != wanted:
        controller.report_failure(wanted, actual)
        note(f"could not apply starting settings {wanted}; running from {actual}")
