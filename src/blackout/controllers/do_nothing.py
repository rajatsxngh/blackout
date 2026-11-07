"""C0: the contestant that never acts. It sets the floor for Downtime Avoided (PRD 8.10)."""

from collections.abc import Sequence

from blackout.info.belief import BeliefState
from blackout.info.observation import Observation
from blackout.sim.actions import AnyAction


class DoNothingController:
    """Never acts, but keeps a belief (trusting every report) so its logs show what it knew."""

    name = "do-nothing"

    def __init__(self) -> None:
        self.belief = BeliefState()

    def reset(self) -> None:
        self.belief = BeliefState()

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        self.belief.observe(observation)
        return ()
