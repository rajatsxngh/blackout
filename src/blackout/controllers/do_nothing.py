"""C0: the contestant that never acts. It sets the floor for Downtime Avoided (PRD 8.10)."""

from collections.abc import Sequence

from blackout.info.observation import Observation
from blackout.sim.actions import DispatchTruck, Reroute


class DoNothingController:
    name = "do-nothing"

    def reset(self) -> None:
        pass

    def decide(self, observation: Observation) -> Sequence[DispatchTruck | Reroute]:
        return ()
