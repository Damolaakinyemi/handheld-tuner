from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

from .controller import Controller, Decision
from .model import Sample, Settings


class Backend(Protocol):
    """Telemetry source and settings applier. sample() should block for ~1 s on real hardware."""

    def sample(self) -> Sample: ...

    def apply(self, settings: Settings) -> None: ...


@dataclass
class RunResult:
    samples: List[Sample] = field(default_factory=list)
    decisions: List[Tuple[int, Decision]] = field(default_factory=list)


def run(
    controller: Controller,
    backend: Backend,
    seconds: int,
    on_decision: Optional[Callable[[int, Decision], None]] = None,
) -> RunResult:
    backend.apply(controller.settings)
    result = RunResult()
    for t in range(1, seconds + 1):
        sample = backend.sample()
        result.samples.append(sample)
        decision = controller.observe(sample)
        if decision is None:
            continue
        result.decisions.append((t, decision))
        if decision.changed:
            backend.apply(decision.settings)
        if on_decision:
            on_decision(t, decision)
    return result
