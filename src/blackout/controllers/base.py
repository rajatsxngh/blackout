"""The interface every contestant implements."""

from collections.abc import Sequence
from typing import Protocol

from blackout.info.belief import BeliefState
from blackout.info.observation import Observation
from blackout.sim.actions import AnyAction


class Controller(Protocol):
    """Decides actions from observations alone. It never sees the scenario file (I2).

    A contestant that keeps a ``belief`` attribute (a BeliefState) gets its digest written to
    every decision record of the run log, for the viewer (see ``belief_of``).
    """

    name: str

    def reset(self) -> None:
        """Forget everything from a previous run."""
        ...

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        """Actions to take at this decision point (possibly none)."""
        ...


def uses_truth(controller: object) -> bool:
    """Whether the contestant reads ground truth (only C3 Reference may, PRD section 8.7).

    Such a contestant has ``uses_truth = True`` and an ``attach(engine)`` method taking a
    callable that returns the live engine; it must run with ``wake_on_truth=True``.
    """
    return getattr(controller, "uses_truth", False) is True


def run_stats(controller: object) -> dict[str, object] | None:
    """Machine-dependent facts about the contestant's run (such as solve times), if it keeps
    any. They go in summary.json only, never in the deterministic run log (D033)."""
    stats = getattr(controller, "run_stats", None)
    if not callable(stats):
        return None
    result = stats()
    return result if isinstance(result, dict) else None


def belief_of(controller: object) -> BeliefState | None:
    """The contestant's current belief, if it keeps one."""
    belief = getattr(controller, "belief", None)
    return belief if isinstance(belief, BeliefState) else None
