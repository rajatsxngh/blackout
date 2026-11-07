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


def belief_of(controller: object) -> BeliefState | None:
    """The contestant's current belief, if it keeps one."""
    belief = getattr(controller, "belief", None)
    return belief if isinstance(belief, BeliefState) else None
