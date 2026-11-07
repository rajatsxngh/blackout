"""The dependency graph and its five queries, on hand-checked cases.

The 3x3 test city has 2-minute roads. H1 (C3) holds 100 L and burns 2 L/min: 50 minutes.
Truck T1 starts empty at depot D1 (A1). Loading and unloading take 5 minutes each.
"""

import pytest

from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest, run_episode
from blackout.graph.dependency import DependencyGraph, build_graph, fuel_node, generator_node
from blackout.graph.queries import (
    at_risk,
    cascade_check,
    critical_path,
    dependents,
    time_to_failure,
)
from blackout.info.belief import BeliefState
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import AssignCrew, DispatchTruck
from blackout.sim.entities import TaskKind
from tests.helpers import DEMO, grid, open_env, substation


def graph_of(env: BlackoutEnv) -> DependencyGraph:
    belief = BeliefState()
    belief.observe(env.observe())
    return build_graph(belief, env.config)


def test_an_empty_truck_goes_via_the_depot() -> None:
    graph = graph_of(open_env())
    path = critical_path(graph, "H1")
    assert time_to_failure(graph, "H1") == 50
    assert (path.truck, path.depot) == ("T1", "D1")
    assert path.route == ("A1", "A2", "A3", "B3", "C3")  # ties broken by node ID
    assert path.eta == 8 + 5 + 5
    assert (path.deadline, path.slack) == (50, 32)
    assert path.roads == ("A1-A2", "A2-A3", "A3-B3", "B3-C3")
    assert at_risk(graph) == ()
    assert at_risk(graph, max_slack=32) == (path,)


def test_a_loaded_truck_mid_road_goes_direct_from_the_next_intersection() -> None:
    env = open_env(
        trucks=[{"id": "T1", "node": "A1", "load_l": 300}, {"id": "T2", "node": "C1"}],
        events=[{"kind": "truck_breakdown", "tick": 1, "truck": "T2"}],
    )
    env.act("all", DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="A3"))
    request = env.advance()  # tick 1: T1 is one minute along A1-A2, T2 broke down
    assert isinstance(request, DecisionRequest)
    path = critical_path(graph_of(env), "H1")
    assert (path.truck, path.depot) == ("T1", None)
    assert path.route == ("A2", "A3", "B3", "C3")
    assert path.eta == 1 + 6 + 5  # finish the road, three more, unload
    assert path.slack == 49 - path.eta


def test_a_truck_already_delivering_keeps_its_route() -> None:
    env = open_env(trucks=[{"id": "T1", "node": "A1", "load_l": 300}])
    route = ("A1", "A2", "A3", "B3", "C3")
    env.act("all", DispatchTruck(truck_id="T1", task=TaskKind.DELIVER, target_id="H1", route=route))
    path = critical_path(graph_of(env), "H1")
    assert (path.route, path.eta) == (route, 8 + 5)


def test_known_blocks_change_the_critical_path() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-A2"}])
    before = critical_path(graph_of(env), "H1")
    assert before.route[:2] == ("A1", "A2")  # the block isn't known yet
    env.act("all", DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="A3"))
    request = env.advance()
    assert isinstance(request, DecisionRequest)
    assert request.observations["all"].known_blocks == ("A1-A2",)
    after = critical_path(graph_of(env), "H1")
    assert "A1-A2" not in after.roads
    assert after.route == ("A1", "B1", "B2", "B3", "C3")
    assert after.eta == before.eta


def test_no_working_truck_means_no_resupply() -> None:
    env = open_env(events=[{"kind": "truck_breakdown", "tick": 0, "truck": "T1"}])
    graph = graph_of(env)
    path = critical_path(graph, "H1")
    assert not path.feasible
    assert (path.eta, path.slack, path.deadline) == (None, None, 50)
    assert at_risk(graph, max_slack=0) == (path,)


def test_dependents_follow_the_chains_back_to_the_hospital() -> None:
    graph = graph_of(open_env(**grid()))
    assert dependents(graph, "S2") == ("H1", "ZC")
    assert dependents(graph, "S1") == ("ZA", "ZB")
    chain = ("H1", fuel_node("H1"), generator_node("H1"), "T1")
    assert dependents(graph, "D1") == chain
    assert dependents(graph, "A2-A3") == chain
    assert time_to_failure(graph, "H1") is None  # on the grid
    assert critical_path(graph, "H1").slack is None
    with pytest.raises(ValueError, match="isn't in the dependency graph"):
        dependents(graph, "Q7")
    with pytest.raises(ValueError, match="isn't a hospital"):
        critical_path(graph, "Q7")


def test_crews_hang_off_the_substation_they_work_on() -> None:
    env = open_env(
        **grid(
            substations=[
                substation("S1", "A2", 10000, state="tripped"),
                substation("S2", "C2", 20000),
            ]
        )
    )
    env.act("all", AssignCrew(crew_id="C1", substation_id="S1"))
    graph = graph_of(env)
    assert dependents(graph, "C1") == ("S1",)
    edge = graph.graph.edges["S1", "C1"]
    assert edge["kind"] == "repair"


def test_cascade_check_follows_the_overload_through_the_grid() -> None:
    # S1 (10 MW) carries ZA + ZB (8 MW); S2 (10 MW) carries ZC (5 MW). Losing S2 puts 13 MW
    # on S1, which trips, and the whole grid goes dark.
    graph = graph_of(open_env(**grid()))
    result = cascade_check(graph, "S2")
    assert result.cascades
    assert result.trips == (("S1",),)
    assert result.unpowered_zones == ("ZA", "ZB", "ZC")
    assert result.hospitals_on_generator == ("H1",)
    assert result.lost_kw == 13000
    assert result.lost_weighted_kw == 4000 * 1 + 4000 * 10 + 5000 * 100
    assert result.restore_minutes is None  # the repair time isn't known
    assert cascade_check(graph, "S2", mode="trip").restore_minutes == 10


def test_cascade_check_with_enough_spare_capacity() -> None:
    env = open_env(
        **grid(substations=[substation("S1", "A2", 10000), substation("S2", "C2", 20000)])
    )
    result = cascade_check(graph_of(env), "S1")
    assert not result.cascades
    assert result.unpowered_zones == ()
    assert result.lost_kw == 0
    with pytest.raises(ValueError, match="isn't a substation"):
        cascade_check(graph_of(env), "S9")
    with pytest.raises(ValueError, match="mode"):
        cascade_check(graph_of(env), "S1", mode="melt")  # type: ignore[arg-type]


def test_a_believed_repair_time_is_the_restore_estimate() -> None:
    env = open_env(
        **grid(
            substations=[
                substation("S1", "A2", 10000, state="failed", repair_minutes=30),
                substation("S2", "C2", 20000),
            ],
            events=[{"kind": "burn_rate_change", "tick": 50, "hospital": "H1", "burn_lpm": 3}],
        )
    )
    env.act("all", AssignCrew(crew_id="C1", substation_id="S1"))
    while (request := env.advance()) and isinstance(request, DecisionRequest):
        if request.observations["all"].substation("S1").work_left is not None:
            break
    assert cascade_check(graph_of(env), "S1").restore_minutes == 30


def test_hello_blackout_block_shows_up_only_after_discovery() -> None:
    """The M4 acceptance case: the block enters telemetry when T1 hits it at tick 18, and the
    critical path to St. Mary's switches from Maple St to the detour at that moment."""
    controller = ScriptedController(load_script(DEMO.with_suffix(".script.json")))
    seen: dict[int, tuple[tuple[str, ...], tuple[str, ...], int | None]] = {}

    def check(request: DecisionRequest, results: object) -> None:
        observation = request.observations["all"]
        path = critical_path(build_graph(controller.belief), "H1")
        seen[request.tick] = (observation.known_blocks, path.route, path.slack)

    env = BlackoutEnv(load_scenario(DEMO))
    run_episode(env, controller, on_decision=check)
    ticks = sorted(seen)
    assert all(seen[t][0] == () for t in ticks if t < 18)
    assert all(seen[t][0] == ("M3-M4",) for t in ticks if t >= 18)
    maple = ("A1", "B1", "C1", "E1", "M1", "M2", "M3", "M4", "M5")
    assert seen[5][1:] == (maple, 14)  # 16 min drive + 5 unloading, 35 min of fuel left
    assert seen[18][1:] == (("M3", "E3", "E4", "M4", "M5"), 6)  # 11 + 5 against 22


def test_the_graph_is_built_from_belief_not_truth() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A2-A3"}])
    graph = graph_of(env)
    assert graph.graph.nodes["A2-A3"]["status"] == "open"  # truth says blocked; nobody knows
    assert critical_path(graph, "H1").roads[1] == "A2-A3"
