"""Hypothesis strategies for random but valid scenarios: cities, fleets, grids and events."""

from itertools import pairwise
from typing import Any

from hypothesis import strategies as st

from blackout.scenarios.schema import Scenario
from blackout.sim.city import edge_ids, node_ids


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
    crews = [{"id": f"C{i + 1}", "node": draw(place)} for i in range(draw(st.integers(0, 2)))]
    substations = draw(substation_specs(place))
    zones = draw(zone_specs(nodes, substations, {h["node"] for h in hospitals}))

    tick = st.integers(0, horizon - 1)
    road = st.sampled_from(edges)
    kinds = [
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
    ]
    if substations:
        kinds.append(
            st.fixed_dictionaries(
                {
                    "kind": st.just("substation_fail"),
                    "tick": tick,
                    "substation": st.sampled_from([s["id"] for s in substations]),
                    "repair_minutes": st.integers(1, 40),
                }
            )
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
            "crews": crews,
            "substations": substations,
            "zones": zones,
            "events": draw(st.lists(st.one_of(*kinds), max_size=8)),
        }
    )


@st.composite
def substation_specs(draw: st.DrawFn, place: st.SearchStrategy[str]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for i in range(draw(st.integers(0, 3))):
        state = draw(st.sampled_from(["online", "online", "failed", "tripped"]))
        spec: dict[str, Any] = {
            "id": f"S{i + 1}",
            "name": f"Substation {i + 1}",
            "node": draw(place),
            "capacity_kw": draw(st.integers(1000, 20000)),
            "state": state,
        }
        if state == "failed":
            spec["repair_minutes"] = draw(st.integers(1, 40))
        specs.append(spec)
    return specs


@st.composite
def zone_specs(
    draw: st.DrawFn,
    nodes: list[str],
    substations: list[dict[str, Any]],
    hospital_nodes: set[str],
) -> list[dict[str, Any]]:
    """Disjoint zones over a random subset of the intersections, fed by random substations."""
    if not substations:
        return []
    feeders = [s["id"] for s in substations]
    order = draw(st.permutations(nodes))
    count = draw(st.integers(1, min(4, len(nodes))))
    covered = draw(st.integers(count, len(nodes)))
    cuts: list[int] = []
    if count > 1:  # split the covered prefix into `count` non-empty runs
        cuts = draw(
            st.lists(
                st.integers(1, covered - 1), min_size=count - 1, max_size=count - 1, unique=True
            )
        )
    specs: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(pairwise([0, *sorted(cuts), covered])):
        members = list(order[start:end])
        primary = draw(st.sampled_from(feeders))
        others = [f for f in feeders if f != primary]
        alternate = draw(st.none() | st.sampled_from(others)) if others else None
        critical = any(node in hospital_nodes for node in members)
        priority = "critical" if critical else draw(st.sampled_from(["essential", "residential"]))
        specs.append(
            {
                "id": f"Z{i + 1}",
                "name": f"Zone {i + 1}",
                "nodes": members,
                "demand_kw": draw(st.integers(500, 9000)),
                "priority": priority,
                "primary": primary,
                "alternate": alternate,
            }
        )
    return specs
