"""The stepper API: reset, act, advance, step, observe."""

import pytest

from blackout.env import ROLE_ALL, BlackoutEnv, DecisionRequest, Done
from blackout.sim.actions import DispatchTruck
from blackout.sim.entities import TaskKind, VehicleState
from blackout.sim.triggers import Trigger, TriggerKind
from tests.helpers import make_scenario, open_env


def load() -> DispatchTruck:
    return DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="D1")


def test_reset_opens_the_first_decision_point() -> None:
    env = BlackoutEnv(make_scenario())
    request = env.reset()
    assert (request.index, request.tick) == (0, 0)
    assert request.triggers == (Trigger(TriggerKind.START),)
    assert set(request.observations) == {ROLE_ALL}
    assert env.roles == (ROLE_ALL,)


def test_acting_before_reset_or_after_the_end_is_an_error() -> None:
    env = BlackoutEnv(make_scenario(horizon=3))
    with pytest.raises(RuntimeError):
        env.act(ROLE_ALL, load())
    env.reset()
    while isinstance(request := env.advance(), DecisionRequest):
        pass
    assert isinstance(request, Done)
    assert env.advance() is request
    with pytest.raises(RuntimeError):
        env.act(ROLE_ALL, load())


def test_unknown_roles_are_rejected() -> None:
    env = open_env()
    with pytest.raises(ValueError, match="unknown role"):
        env.act("power", load())
    with pytest.raises(ValueError, match="unknown role"):
        env.observe("power")
    with pytest.raises(ValueError, match="unknown roles"):
        env.step({"power": [load()]})


def test_actions_apply_immediately_while_the_clock_is_paused() -> None:
    env = open_env()
    assert env.observe().truck("T1").state is VehicleState.IDLE
    env.act(ROLE_ALL, load())
    after = env.observe()
    assert after.tick == 0
    assert after.truck("T1").state is VehicleState.LOADING


def test_step_is_act_then_advance() -> None:
    first = open_env()
    first.act(ROLE_ALL, load())
    first.advance()
    second = open_env()
    results, request = second.step({ROLE_ALL: [load()]})
    assert [r.accepted for r in results] == [True]
    assert isinstance(request, DecisionRequest)
    assert second.tick_hashes == first.tick_hashes


def test_decision_points_are_numbered_and_counted() -> None:
    env = open_env(horizon=40)
    indexes = [0]
    while isinstance(request := env.advance(), DecisionRequest):
        indexes.append(request.index)
    assert indexes == list(range(len(indexes)))
    assert env.engine.metrics.decision_points == len(indexes)


def test_simultaneous_triggers_are_merged_and_sorted() -> None:
    env = open_env(
        trucks=[{"id": "T1", "node": "A1"}, {"id": "T2", "node": "B1"}],
        events=[
            {"kind": "truck_breakdown", "tick": 3, "truck": "T2"},
            {"kind": "truck_breakdown", "tick": 3, "truck": "T1"},
        ],
    )
    request = env.advance()
    assert isinstance(request, DecisionRequest)
    assert [str(t) for t in request.triggers] == ["vehicle_broken(T1)", "vehicle_broken(T2)"]


def test_dispatching_mid_road_routes_from_the_next_intersection() -> None:
    env = open_env(
        trucks=[{"id": "T1", "node": "A1"}, {"id": "T2", "node": "C1"}],
        events=[{"kind": "truck_breakdown", "tick": 1, "truck": "T2"}],
    )
    env.act(ROLE_ALL, DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="C1"))
    request = env.advance()  # tick 1: T1 is one minute along A1-B1
    assert isinstance(request, DecisionRequest)
    result = env.act(ROLE_ALL, DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="A2"))
    assert result.accepted
    assert result.route[0] == "B1"
    assert result.eta_minutes == 1 + 4  # finish A1-B1, then two 2-minute roads


def test_observations_never_show_hidden_blocks() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "B2-C2"}])
    assert env.engine.blocked == {"B2-C2"}
    assert env.observe().known_blocks == ()
