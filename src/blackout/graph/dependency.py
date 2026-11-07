"""The dependency graph: what each hospital's survival hangs on, as a contestant believes it.

It's a NetworkX ``DiGraph`` rebuilt from a ``BeliefState`` at each decision point, never from
ground truth. An edge ``u -> v`` means "u depends on v". Node IDs are entity IDs, plus
``<hospital>/generator`` and ``<hospital>/fuel`` for each hospital's backup:

    hospital -> zone -> substation -> crew          grid power, and who's fixing it
    hospital -> hospital/generator -> hospital/fuel  the backup generator and its tank
    hospital/fuel -> truck -> depot, road, ...       the believed fastest resupply

Every node has a ``kind`` (hospital, generator, fuel, truck, depot, road, zone, substation,
crew). Resupply edges carry ``eta`` (minutes until the fuel is in the tank) and ``slack``
(minutes to spare before the tank runs dry).

The resupply plan for a hospital is its *critical path*: the truck, optional depot and route
that get fuel into its tank soonest, routing around every road believed blocked. A truck
already delivering there keeps its current route if that route avoids believed blocks.
"""

from dataclasses import dataclass
from itertools import pairwise

import networkx as nx

from blackout.info.belief import BeliefState
from blackout.info.telemetry import TruckView
from blackout.sim.city import City, EdgeId, NodeId, edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind, VehicleState
from blackout.sim.routing import path_minutes, shortest_path

type GraphNode = str


def generator_node(hospital_id: str) -> GraphNode:
    return f"{hospital_id}/generator"


def fuel_node(hospital_id: str) -> GraphNode:
    return f"{hospital_id}/fuel"


@dataclass(frozen=True, slots=True)
class CriticalPath:
    """The believed fastest way to get fuel into one hospital's tank.

    ``deadline`` is the believed time to failure (None while the hospital has grid power).
    ``eta`` counts minutes until the fuel is in the tank: finishing the current road or task,
    driving, loading at ``depot`` if the truck is empty, and unloading. ``slack`` is
    ``deadline - eta``; negative means the tank runs dry first. ``truck`` is None if no truck
    can get there on roads believed open.
    """

    hospital: str
    deadline: int | None
    truck: str | None = None
    depot: str | None = None
    route: tuple[NodeId, ...] = ()
    eta: int | None = None
    slack: int | None = None

    @property
    def roads(self) -> tuple[EdgeId, ...]:
        return tuple(edge_id(a, b) for a, b in pairwise(self.route))

    @property
    def feasible(self) -> bool:
        return self.truck is not None


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    graph: nx.DiGraph
    belief: BeliefState
    config: SimConfig
    paths: dict[str, CriticalPath]


def time_to_failure(belief: BeliefState, hospital_id: str) -> int | None:
    """Believed minutes until the hospital goes dark: None if it has grid power (or nothing
    is known about it), 0 if it already is dark."""
    if belief.powered(hospital_id):
        return None
    fuel, burn = belief.fuel_now(hospital_id), belief.burn(hospital_id)
    if fuel is None or burn is None:
        return None
    return fuel // burn


def build_graph(belief: BeliefState, config: SimConfig | None = None) -> DependencyGraph:
    """The dependency graph for ``belief``. Uses only belief, the public map and public rules."""
    rules = config or SimConfig()
    graph = nx.DiGraph()
    for zone_id in sorted(belief.zones):
        zone = belief.zones[zone_id]
        graph.add_node(zone_id, kind="zone", powered=zone.powered, priority=zone.priority.value)
        feeds = [zone.fed_by] if zone.fed_by is not None else [zone.primary, zone.alternate]
        for feeder in feeds:
            if feeder is not None:
                graph.add_edge(zone_id, feeder, kind="feed")
    for substation_id in sorted(belief.substations):
        state = belief.substations[substation_id].state
        graph.add_node(
            substation_id, kind="substation", state=None if state is None else state.value
        )
    for crew_id in sorted(belief.crews):
        crew = belief.crews[crew_id]
        graph.add_node(crew_id, kind="crew", state=crew.state.value)
        if crew.substation is not None:
            graph.add_edge(crew.substation, crew_id, kind="repair")
    for truck_id in sorted(belief.trucks):
        truck = belief.trucks[truck_id]
        graph.add_node(truck_id, kind="truck", state=truck.state.value, load_l=truck.load_l)
    for depot_id in sorted(belief.depots):
        graph.add_node(depot_id, kind="depot")

    paths: dict[str, CriticalPath] = {}
    for hospital_id in sorted(belief.hospitals):
        ttf = time_to_failure(belief, hospital_id)
        reading = belief.hospitals[hospital_id].reading
        graph.add_node(hospital_id, kind="hospital", ttf=ttf)
        graph.add_node(generator_node(hospital_id), kind="generator")
        graph.add_node(fuel_node(hospital_id), kind="fuel", liters=belief.fuel_now(hospital_id))
        if reading is not None and reading.zone is not None:
            graph.add_edge(hospital_id, reading.zone, kind="grid")
        graph.add_edge(hospital_id, generator_node(hospital_id), kind="backup")
        graph.add_edge(generator_node(hospital_id), fuel_node(hospital_id), kind="tank")
        path = _critical_path(belief, rules, hospital_id, ttf)
        paths[hospital_id] = path
        if path.truck is None:
            continue
        resupply = fuel_node(hospital_id)
        graph.add_edge(resupply, path.truck, kind="resupply", eta=path.eta, slack=path.slack)
        if path.depot is not None:
            graph.add_edge(path.truck, path.depot, kind="load")
        for road in path.roads:
            graph.add_node(road, kind="road", status=_road_status(belief, road))
            graph.add_edge(path.truck, road, kind="route")
    return DependencyGraph(graph, belief, rules, paths)


def _road_status(belief: BeliefState, road: EdgeId) -> str:
    estimate = belief.roads.get(road)
    return "open" if estimate is None else str(estimate.value)


def _critical_path(
    belief: BeliefState, rules: SimConfig, hospital_id: str, deadline: int | None
) -> CriticalPath:
    city = belief.city
    reading = belief.hospitals[hospital_id].reading
    if city is None or reading is None:
        return CriticalPath(hospital_id, deadline)
    target = reading.node
    blocked = set(belief.blocked_roads())
    best: CriticalPath | None = None
    for truck_id in sorted(belief.trucks):  # ties go to the lower truck ID
        truck = belief.trucks[truck_id]
        option = _fastest_resupply(belief, rules, city, truck, hospital_id, target, blocked)
        if option is None:
            continue
        eta, route, depot = option
        if best is None or best.eta is None or eta < best.eta:
            slack = None if deadline is None else deadline - eta
            best = CriticalPath(hospital_id, deadline, truck_id, depot, route, eta, slack)
    return best or CriticalPath(hospital_id, deadline)


def _fastest_resupply(
    belief: BeliefState,
    rules: SimConfig,
    city: City,
    truck: TruckView,
    hospital_id: str,
    target: NodeId,
    blocked: set[EdgeId],
) -> tuple[int, tuple[NodeId, ...], str | None] | None:
    """(eta, route, depot) for this truck's fastest way to fill the hospital, or None."""
    if truck.state is VehicleState.BROKEN:
        return None
    start = truck.anchor
    lead = 0  # minutes before the truck can start a new route from ``start``
    if truck.next_node is not None:
        lead += city.edges[edge_id(truck.at, truck.next_node)].minutes - truck.elapsed
    if truck.state in (VehicleState.LOADING, VehicleState.UNLOADING):
        lead += truck.work_left
    task = truck.task
    if truck.state is VehicleState.UNLOADING and task is not None and task.target_id == hospital_id:
        return lead, (truck.at,), None  # already unloading here
    heading_here = (
        task is not None
        and task.kind is TaskKind.DELIVER
        and task.target_id == hospital_id
        and truck.state is VehicleState.MOVING
    )
    if heading_here:
        current = truck.route if truck.next_node is not None else (truck.at, *truck.route)
        if current and current[-1] == target and not _crosses(current, blocked):
            return lead + path_minutes(city, current) + rules.unload_minutes, tuple(current), None

    will_have_fuel = truck.state is VehicleState.LOADING or (
        truck.load_l > 0 and truck.state is not VehicleState.UNLOADING
    )
    if will_have_fuel:
        direct = shortest_path(city, start, target, blocked)
        if direct is None:
            return None
        return lead + path_minutes(city, direct) + rules.unload_minutes, tuple(direct), None

    best: tuple[int, tuple[NodeId, ...], str | None] | None = None
    for depot_id in sorted(belief.depots):
        depot = belief.depots[depot_id]
        stock = depot.stock_l
        if depot.reading is None or stock is None or stock.value == 0:
            continue
        to_depot = shortest_path(city, start, depot.reading.node, blocked)
        if to_depot is None:
            continue
        onward = shortest_path(city, depot.reading.node, target, blocked)
        if onward is None:
            continue
        route = (*to_depot, *onward[1:])
        eta = lead + path_minutes(city, route) + rules.load_minutes + rules.unload_minutes
        if best is None or eta < best[0]:
            best = (eta, route, depot_id)
    return best


def _crosses(path: tuple[NodeId, ...], blocked: set[EdgeId]) -> bool:
    return any(edge_id(a, b) in blocked for a, b in pairwise(path))
