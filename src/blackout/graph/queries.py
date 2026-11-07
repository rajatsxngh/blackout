"""The five dependency-graph queries (docs/PRD.md section 8.5).

Baselines call these directly; agents get them as the ``query_graph`` tool (M11). Every answer
comes from the contestant's belief, so it's only as good as what the contestant knows.

    dependents(graph, node_id)              everything that depends on a node, directly or not
    time_to_failure(graph, hospital_id)     believed minutes until the hospital goes dark
    critical_path(graph, hospital_id)       depot -> truck -> route, ETA vs deadline, slack
    cascade_check(graph, substation_id)     what else goes down if this substation does
    at_risk(graph, max_slack=30)            hospitals with little or no slack, worst first
"""

from dataclasses import dataclass
from typing import Literal

import networkx as nx

from blackout.graph.dependency import CriticalPath, DependencyGraph, GraphNode
from blackout.graph.dependency import time_to_failure as _time_to_failure
from blackout.sim.entities import Priority, Substation, SubstationState, Zone
from blackout.sim.power import resolve_feeds, substation_loads


def dependents(graph: DependencyGraph, node_id: GraphNode) -> tuple[GraphNode, ...]:
    """Every node that depends on ``node_id``, directly or through others, sorted."""
    if node_id not in graph.graph:
        raise ValueError(f"{node_id!r} isn't in the dependency graph")
    return tuple(sorted(nx.ancestors(graph.graph, node_id)))


def time_to_failure(graph: DependencyGraph, hospital_id: str) -> int | None:
    """Believed minutes until ``hospital_id`` goes dark; None while it has grid power."""
    _check_hospital(graph, hospital_id)
    return _time_to_failure(graph.belief, hospital_id)


def critical_path(graph: DependencyGraph, hospital_id: str) -> CriticalPath:
    """The believed fastest resupply for ``hospital_id``, with its ETA, deadline and slack."""
    _check_hospital(graph, hospital_id)
    return graph.paths[hospital_id]


def at_risk(graph: DependencyGraph, max_slack: int = 30) -> tuple[CriticalPath, ...]:
    """Hospitals on generator with at most ``max_slack`` minutes to spare, or no way to be
    resupplied in time. Worst first: no resupply at all, then by slack."""
    risky = [
        path
        for path in graph.paths.values()
        if path.deadline is not None and (path.slack is None or path.slack <= max_slack)
    ]
    return tuple(
        sorted(risky, key=lambda p: (p.slack is not None, p.slack or 0, p.deadline, p.hospital))
    )


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """What follows if ``substation`` goes down, under the believed loads and feeds.

    ``trips`` lists the substations that would then trip, in waves (each overloaded one trips
    after the grace period, and its zones fall back to their alternates). ``restore_minutes``
    is how long getting ``substation`` back takes once a crew is on site: the believed repair
    time for a failure (None if unknown), the reset time for a trip.
    """

    substation: str
    mode: Literal["fail", "trip"]
    trips: tuple[tuple[str, ...], ...]
    unpowered_zones: tuple[str, ...]
    hospitals_on_generator: tuple[str, ...]
    lost_kw: int
    lost_weighted_kw: int
    restore_minutes: int | None

    @property
    def cascades(self) -> bool:
        return bool(self.trips)


def cascade_check(
    graph: DependencyGraph, substation_id: str, mode: Literal["fail", "trip"] = "fail"
) -> CascadeResult:
    """Simulate losing ``substation_id`` on the believed grid, following any cascade."""
    belief, rules = graph.belief, graph.config
    if substation_id not in belief.substations:
        raise ValueError(f"{substation_id!r} isn't a substation this role knows about")
    if mode not in ("fail", "trip"):
        raise ValueError(f"mode must be 'fail' or 'trip', not {mode!r}")
    substations: dict[str, Substation] = {}
    for sid in sorted(belief.substations):
        known = belief.substations[sid]
        reading = known.reading
        state = SubstationState(known.state.value) if known.state is not None else None
        substations[sid] = Substation(
            id=sid,
            name=sid,
            node=reading.node if reading is not None else "",
            capacity_kw=reading.capacity_kw if reading is not None else 0,
            state=state or SubstationState.FAILED,  # unknown counts as down
        )
    zones = {
        zid: Zone(
            id=zid,
            name=z.name,
            nodes=z.nodes,
            demand_kw=z.demand_kw,
            priority=z.priority,
            primary=z.primary,
            alternate=z.alternate,
            shed=z.shed,
        )
        for zid, z in sorted(belief.zones.items())
        if z.primary in substations and (z.alternate is None or z.alternate in substations)
    }
    before = resolve_feeds(zones, substations)
    substations[substation_id].state = (
        SubstationState.FAILED if mode == "fail" else SubstationState.TRIPPED
    )
    waves: list[tuple[str, ...]] = []
    while True:
        feeds = resolve_feeds(zones, substations)
        loads = substation_loads(zones, feeds)
        overloaded = tuple(
            sid
            for sid in sorted(substations)
            if substations[sid].state is SubstationState.ONLINE
            and loads.get(sid, 0) > substations[sid].capacity_kw
        )
        if not overloaded:
            break
        waves.append(overloaded)
        for sid in overloaded:
            substations[sid].state = SubstationState.TRIPPED
    lost = [zid for zid in sorted(zones) if before[zid] is not None and feeds[zid] is None]
    unpowered = tuple(zid for zid in sorted(zones) if feeds[zid] is None)
    hospitals = tuple(
        hid
        for hid in sorted(belief.hospitals)
        if (reading := belief.hospitals[hid].reading) is not None and reading.zone in unpowered
    )
    eta = belief.substations[substation_id].eta_minutes
    restore = rules.reset_minutes if mode == "trip" else None
    if mode == "fail" and eta is not None and isinstance(eta.value, int) and eta.value > 0:
        restore = eta.value
    return CascadeResult(
        substation=substation_id,
        mode=mode,
        trips=tuple(waves),
        unpowered_zones=unpowered,
        hospitals_on_generator=hospitals,
        lost_kw=sum(zones[z].demand_kw for z in lost),
        lost_weighted_kw=sum(
            zones[z].demand_kw * rules.weight(Priority(zones[z].priority)) for z in lost
        ),
        restore_minutes=restore,
    )


def _check_hospital(graph: DependencyGraph, hospital_id: str) -> None:
    if hospital_id not in graph.paths:
        raise ValueError(f"{hospital_id!r} isn't a hospital this role knows about")
