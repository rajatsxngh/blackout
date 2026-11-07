"""Property tests: physics invariants and determinism under random play on random cities."""

from hypothesis import given
from hypothesis import strategies as st

from blackout.env import BlackoutEnv, run_episode
from blackout.scenarios.schema import Scenario
from tests.helpers import InvariantChecker, RandomController
from tests.strategies import scenarios


@given(scenario=scenarios(), seed=st.integers(0, 2**32 - 1))
def test_physics_invariants_hold_under_random_play(scenario: Scenario, seed: int) -> None:
    checker = InvariantChecker()
    result = run_episode(BlackoutEnv(scenario, on_tick=checker), RandomController(seed))
    assert checker.ticks == len(result.tick_hashes)
    assert result.done.tick <= scenario.horizon
    assert result.done.metrics["decision_points"] == len(result.decisions)


@given(scenario=scenarios(), seed=st.integers(0, 2**32 - 1))
def test_same_inputs_give_identical_runs(scenario: Scenario, seed: int) -> None:
    first = run_episode(BlackoutEnv(scenario), RandomController(seed))
    second = run_episode(BlackoutEnv(scenario), RandomController(seed))
    assert first.tick_hashes == second.tick_hashes
    assert [d.results for d in first.decisions] == [d.results for d in second.decisions]
