"""Every rejection reason, and checks that validation never leaks hidden truth."""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from blackout.env import BlackoutEnv, DecisionRequest
from blackout.sim.actions import DispatchTruck, RejectReason, Reroute
from blackout.sim.entities import TaskKind
from tests.helpers import open_env

type Case = Callable[[], tuple[BlackoutEnv, DispatchTruck | Reroute]]


def move(target: str = "B1", truck: str = "T1", **kwargs: object) -> DispatchTruck:
    return DispatchTruck(truck_id=truck, task=TaskKind.MOVE, target_id=target, **kwargs)  # type: ignore[arg-type]


def _unknown_vehicle() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), move(truck="T9")


def _conflict() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    env = open_env()
    assert env.act("all", move("B1")).accepted
    return env, move("A2")


def _broken() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(events=[{"kind": "truck_breakdown", "tick": 0, "truck": "T1"}]), move()


def _busy() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    env = open_env(
        trucks=[{"id": "T1", "node": "A1"}, {"id": "T2", "node": "C1"}],
        events=[{"kind": "truck_breakdown", "tick": 2, "truck": "T2"}],
    )
    load = DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="D1")
    assert env.act("all", load).accepted
    request = env.advance()  # T2 breaks down at tick 2, while T1 is still loading
    assert isinstance(request, DecisionRequest)
    assert request.tick == 2
    return env, move()


def _no_task() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), Reroute(vehicle_id="T1")


def _invalid_target() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="H1")


def _truck_full() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    env = open_env(trucks=[{"id": "T1", "node": "A1", "load_l": 500}])
    return env, DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="D1")


def _depot_empty() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    env = open_env(depots=[{"id": "D1", "name": "Dry", "node": "A1", "stock_l": 0}])
    return env, DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="D1")


def _empty_truck() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), DispatchTruck(truck_id="T1", task=TaskKind.DELIVER, target_id="H1")


def _unknown_edge() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), move(avoid_edges=("A1-C3",))


def _bad_route() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), move("C1", route=("A1", "C1"))


def _no_route() -> tuple[BlackoutEnv, DispatchTruck | Reroute]:
    return open_env(), move("C3", avoid_edges=("B3-C3", "C2-C3"))


CASES: dict[RejectReason, Case] = {
    RejectReason.UNKNOWN_VEHICLE: _unknown_vehicle,
    RejectReason.CONFLICT: _conflict,
    RejectReason.VEHICLE_BROKEN: _broken,
    RejectReason.VEHICLE_BUSY: _busy,
    RejectReason.NO_TASK: _no_task,
    RejectReason.INVALID_TARGET: _invalid_target,
    RejectReason.TRUCK_FULL: _truck_full,
    RejectReason.DEPOT_EMPTY: _depot_empty,
    RejectReason.EMPTY_TRUCK: _empty_truck,
    RejectReason.UNKNOWN_EDGE: _unknown_edge,
    RejectReason.BAD_ROUTE: _bad_route,
    RejectReason.NO_ROUTE: _no_route,
}


def test_every_reject_reason_has_a_case() -> None:
    assert set(CASES) == set(RejectReason)


@pytest.mark.parametrize("reason", list(RejectReason))
def test_rejected_actions_change_nothing(reason: RejectReason) -> None:
    env, action = CASES[reason]()
    before = env.engine.state_hash()
    result = env.act("all", action)
    assert not result.accepted
    assert result.reason is reason
    assert result.message
    assert env.engine.state_hash() == before
    assert env.engine.metrics.actions_rejected == {reason.value: 1}


@pytest.mark.parametrize(
    ("route", "problem"),
    [
        (("B1", "C1"), "must start at A1"),
        (("A1", "B1"), "must end at C1"),
        (("A1", "C1"), "not joined by a road"),
        (("A1", "Z9", "C1"), "unknown intersection"),
        ((), "route is empty"),
    ],
)
def test_bad_routes_say_what_is_wrong(route: tuple[str, ...], problem: str) -> None:
    result = open_env().act("all", move("C1", route=route))
    assert result.reason is RejectReason.BAD_ROUTE
    assert problem in result.message


@pytest.mark.parametrize(
    ("task", "target"),
    [(TaskKind.LOAD, "H1"), (TaskKind.DELIVER, "D1"), (TaskKind.MOVE, "Z9"), (TaskKind.LOAD, "X1")],
)
def test_target_must_suit_the_task(task: TaskKind, target: str) -> None:
    env = open_env(trucks=[{"id": "T1", "node": "A1", "load_l": 100}])
    result = env.act("all", DispatchTruck(truck_id="T1", task=task, target_id=target))
    assert result.reason is RejectReason.INVALID_TARGET


def test_a_rejected_action_does_not_use_up_the_turn() -> None:
    env = open_env()
    assert not env.act("all", move("C1", route=("A1", "C1"))).accepted
    assert env.act("all", move("C1")).accepted


def test_hidden_blocks_are_not_revealed_by_validation() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}])
    result = env.act("all", move("B1"))
    assert result.accepted
    assert result.route == ("A1", "B1")
    assert "block" not in result.message


def test_known_blocks_are_routed_around_unless_a_route_says_otherwise() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}])
    env.act("all", move("B1"))
    request = env.advance()  # the truck finds the block
    assert isinstance(request, DecisionRequest)
    detour = env.act("all", Reroute(vehicle_id="T1"))
    assert detour.accepted
    assert detour.route == ("A1", "A2", "B2", "B1")
    # An explicit route may still try the known block (e.g. if it's believed to have cleared).
    env2 = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}])
    env2.act("all", move("B1"))
    env2.advance()
    assert env2.act("all", Reroute(vehicle_id="T1", route=("A1", "B1"))).accepted


def test_route_and_avoid_edges_together_is_a_schema_error() -> None:
    with pytest.raises(ValidationError, match="either route or avoid_edges"):
        Reroute(vehicle_id="T1", route=("A1", "B1"), avoid_edges=("A1-A2",))


def test_reasons_are_limited_to_140_characters() -> None:
    with pytest.raises(ValidationError, match="at most 140 characters"):
        Reroute(vehicle_id="T1", reason="x" * 141)
