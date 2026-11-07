"""The interface every contestant implements."""

from collections.abc import Sequence
from typing import Protocol

from blackout.info.observation import Observation
from blackout.sim.actions import AnyAction


class Controller(Protocol):
    """Decides actions from observations alone. It never sees the scenario file (I2)."""

    name: str

    def reset(self) -> None:
        """Forget everything from a previous run."""
        ...

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        """Actions to take at this decision point (possibly none)."""
        ...
