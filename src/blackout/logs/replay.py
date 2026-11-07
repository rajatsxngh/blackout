"""Replaying a run log against its scenario, and checking nothing differs (invariant I4).

The replay re-issues every logged action, rejected ones included, at the logged decision
points. It stops at the first difference: a state hash, a decision tick, an observation, or an
action outcome that doesn't match what was recorded.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter

from blackout.env import BlackoutEnv, DecisionRequest, EpisodeResult, run_episode
from blackout.info.observation import Observation
from blackout.logs.reader import RunLog
from blackout.logs.schema import DecisionPointRecord, LogError
from blackout.scenarios.schema import Scenario, scenario_digest
from blackout.sim.actions import Action, ActionResult, AnyAction
from blackout.sim.config import SimConfig
from blackout.sim.engine import Engine
from blackout.sim.metrics import Metrics

ACTION: TypeAdapter[AnyAction] = TypeAdapter(Action)


class ReplayMismatchError(LogError):
    """Replaying the log didn't reproduce the run it recorded."""


class ReplayController:
    """Plays back a log's actions at its decision points, checking each is where it should be."""

    name = "replay"

    def __init__(self, decisions: Sequence[DecisionPointRecord]) -> None:
        self._decisions = tuple(decisions)
        self._next = 0

    @property
    def remaining(self) -> tuple[DecisionPointRecord, ...]:
        """Logged decision points the replay hasn't reached."""
        return self._decisions[self._next :]

    def reset(self) -> None:
        self._next = 0

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        if self._next >= len(self._decisions):
            raise ReplayMismatchError(
                f"the replay opened a decision point at tick {observation.tick}"
                " that isn't in the log"
            )
        record = self._decisions[self._next]
        where = f"decision point {record.i}"
        if observation.tick != record.t:
            raise ReplayMismatchError(
                f"{where} is at tick {record.t} in the log"
                f" but at tick {observation.tick} in the replay"
            )
        if observation.digest() != record.obs:
            raise ReplayMismatchError(
                f"{where} at tick {record.t}: the observation differs from the log"
            )
        self._next += 1
        return [ACTION.validate_python(action.raw) for action in record.actions]


@dataclass(frozen=True, slots=True)
class Replay:
    """A verified replay: the episode, plus the referee's own metrics and report."""

    episode: EpisodeResult
    metrics: Metrics
    power_restored_at: int | None
    report: dict[str, object]


def replay(log: RunLog, scenario: Scenario | None = None) -> Replay:
    """Re-simulate ``log`` and check every state hash, decision point and action outcome.

    ``scenario`` defaults to the one embedded in the log. Raises ReplayMismatchError at the
    first difference.
    """
    scenario = log.scenario() if scenario is None else scenario
    if scenario_digest(scenario) != log.header.scenario.hash:
        raise ReplayMismatchError(
            f"scenario {scenario.id!r} isn't the one this run was recorded on"
        )
    env = BlackoutEnv(scenario, config=SimConfig.from_dict(log.header.engine.config))
    expected = [tick.h for tick in log.ticks]

    def check_tick(engine: Engine) -> None:
        if engine.tick >= len(expected):
            raise ReplayMismatchError(
                f"the replay ran past tick {len(expected) - 1}, where the log ends"
            )
        if env.tick_hashes[-1] != expected[engine.tick]:
            raise ReplayMismatchError(
                f"the replayed state differs from the log at tick {engine.tick}"
            )

    def check_outcomes(request: DecisionRequest, results: tuple[ActionResult, ...]) -> None:
        record = log.decisions[request.index]
        replayed = [(r.accepted, None if r.reason is None else r.reason.value) for r in results]
        if replayed != [(a.accepted, a.reason) for a in record.actions]:
            raise ReplayMismatchError(
                f"decision point {record.i} at tick {record.t}: action outcomes differ from the log"
            )

    env.add_tick_listener(check_tick)
    controller = ReplayController(log.decisions)
    episode = run_episode(env, controller, on_decision=check_outcomes)
    if controller.remaining:
        missing = controller.remaining[0]
        raise ReplayMismatchError(
            f"the log has decision point {missing.i} at tick {missing.t},"
            f" but the replay ended at tick {episode.done.tick}"
        )
    if len(episode.tick_hashes) != len(expected):
        raise ReplayMismatchError(
            f"the replay ended at tick {episode.done.tick},"
            f" but the log runs to tick {len(expected) - 1}"
        )
    end = log.end
    if end is not None and (end.reason, end.run_hash) != (episode.done.reason, episode.run_hash):
        raise ReplayMismatchError("the end record doesn't match how the replay ended")
    engine = env.engine
    return Replay(episode, engine.metrics, engine.power_restored_at(), engine.report())
