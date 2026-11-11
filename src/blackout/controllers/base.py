"""The interface every contestant implements."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from blackout.info.belief import BeliefState
from blackout.info.observation import Observation
from blackout.sim.actions import ActionResult, AnyAction


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


@dataclass(frozen=True, slots=True)
class Actor:
    """What a contestant that acts as it goes (an LLM agent's action tools, PRD 8.8) gets at a
    decision point: ``act`` validates and applies one action immediately (``env.act``), and
    ``observe`` shows the role's view right now, including what this turn's actions did. The
    clock stays paused, so acting this way changes nothing a list of actions wouldn't."""

    act: Callable[[AnyAction], ActionResult]
    observe: Callable[[], Observation]


def acts_live(controller: object) -> bool:
    """Whether the contestant acts through an ``Actor`` (``decide_live(observation, actor)``)
    instead of returning its actions from ``decide``."""
    return getattr(controller, "acts_live", False) is True and callable(
        getattr(controller, "decide_live", None)
    )


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


def decision_log(controller: object) -> dict[str, Any] | None:
    """What an LLM contestant adds to a decision record (``llm``, ``info``, ``notes``), read
    right after it decided. Its values must be deterministic given the responses (D089)."""
    log = getattr(controller, "decision_log", None)
    if not callable(log):
        return None
    result = log()
    return result if isinstance(result, dict) else None


def transcripts_of(controller: object) -> list[dict[str, Any]] | None:
    """An LLM contestant's call transcripts, for ``transcripts.jsonl.gz``."""
    fn = getattr(controller, "transcripts", None)
    if not callable(fn):
        return None
    result = fn()
    return result if isinstance(result, list) else None


def describe(controller: object) -> dict[str, Any]:
    """The model, effort, run index and prompt hash an LLM contestant runs with (None for
    the others), for the run header and the run's config hash."""
    return {
        "model": getattr(controller, "model", None),
        "effort": getattr(controller, "effort", None),
        "prompt_hash": getattr(controller, "prompt_hash", None),
    }
