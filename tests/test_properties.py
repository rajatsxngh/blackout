"""Property tests: physics invariants and determinism under random play on random cities."""

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from blackout.env import BlackoutEnv, run_episode
from blackout.scenarios.schema import Scenario
from blackout.sim.city import edge_ids, node_ids
from tests.helpers import InvariantChecker, RandomController


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    width, height = draw(st.integers(2, 4)), draw(st.integers(2, 4))
    nodes, edges = node_ids(width, height), edge_ids(width, height)
    horizon = draw(st.integers(5, 90))
    place = st.sampled_from(nodes)

    hospitals: list[dict[str, Any]] = []
    for i in range(draw(st.integers(1, 2))):
        tank = draw(st.integers(1, 600))
        hospitals.append(
            {
                "id": f"H{i + 1}",
                "name": f"Hospital {i + 1}",
                "node": draw(place),
                "tank_l": tank,
                "fuel_l": draw(st.integers(0, tank)),
                "burn_lpm": draw(st.integers(1, 5)),
                "grid_powered": draw(st.booleans()),
            }
        )
    depots = [
        {
            "id": f"D{i + 1}",
            "name": f"Depot {i + 1}",
            "node": draw(place),
            "stock_l": draw(st.integers(0, 2000)),
        }
        for i in range(draw(st.integers(1, 2)))
    ]
    trucks: list[dict[str, Any]] = []
    for i in range(draw(st.integers(1, 3))):
        capacity = draw(st.integers(1, 500))
        trucks.append(
            {
                "id": f"T{i + 1}",
                "node": draw(place),
                "capacity_l": capacity,
                "load_l": draw(st.integers(0, capacity)),
            }
        )

    tick = st.integers(0, horizon - 1)
    road = st.sampled_from(edges)
    event = st.one_of(
        st.fixed_dictionaries({"kind": st.just("road_block"), "tick": tick, "edge": road}),
        st.fixed_dictionaries({"kind": st.just("road_clear"), "tick": tick, "edge": road}),
        st.fixed_dictionaries(
            {
                "kind": st.just("truck_breakdown"),
                "tick": tick,
                "truck": st.sampled_from([t["id"] for t in trucks]),
                "repair_minutes": st.none() | st.integers(1, 30),
            }
        ),
        st.fixed_dictionaries(
            {
                "kind": st.just("burn_rate_change"),
                "tick": tick,
                "hospital": st.sampled_from([h["id"] for h in hospitals]),
                "burn_lpm": st.integers(1, 6),
            }
        ),
    )
    return Scenario.model_validate(
        {
            "id": "random",
            "horizon": horizon,
            "city": {
                "width": width,
                "height": height,
                "default_travel_minutes": draw(st.integers(1, 4)),
                "travel_minutes": draw(st.dictionaries(road, st.integers(1, 5), max_size=4)),
            },
            "hospitals": hospitals,
            "depots": depots,
            "trucks": trucks,
            "events": draw(st.lists(event, max_size=8)),
        }
    )


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
