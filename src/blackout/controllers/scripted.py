"""A contestant that replays a fixed script, for demos and tests.

Each step has an ``at_tick``. A step is issued at the first decision point whose tick is at or
after that time, so a script doesn't need to know exactly when decision points happen.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, NonNegativeInt

from blackout.info.observation import Observation
from blackout.sim.actions import Action, AnyAction


class ScriptStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at_tick: NonNegativeInt
    action: Action


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    steps: list[ScriptStep]


def load_script(path: Path) -> Script:
    return Script.model_validate_json(path.read_text(encoding="utf-8"))


class ScriptedController:
    name = "scripted"

    def __init__(self, script: Script) -> None:
        self._steps = sorted(script.steps, key=lambda step: step.at_tick)
        self._next = 0

    def reset(self) -> None:
        self._next = 0

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        due: list[AnyAction] = []
        while self._next < len(self._steps) and self._steps[self._next].at_tick <= observation.tick:
            due.append(self._steps[self._next].action)
            self._next += 1
        return due
