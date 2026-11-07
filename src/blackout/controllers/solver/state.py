"""The solver's starting point: one snapshot of what the caller believes, with overrides applied.

``planning_state`` turns a ``BeliefState`` (plus ``SolverBeliefs`` overrides) into plain,
integer data the power and fuel planners work on: where every truck can next start a trip
and when, what it carries, which hospitals burn how fast, which depots hold how much, and
which roads are believed blocked. Nothing here reads ground truth; the reference contestant
gets truth only because it hands in a belief built from truth.
"""

from dataclasses import dataclass, field, replace
from itertools import pairwise

from blackout.info.belief import BeliefState
from blackout.info.telemetry import CrewView, SubstationView, TruckView, ZoneView
from blackout.sim.city import City, EdgeId, NodeId, edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import Priority, TaskKind, VehicleState
from blackout.sim.routing import path_minutes, shortest_path

DEFAULT_REPAIR_MINUTES = 90
"""Crew minutes assumed for a failed substation nobody has estimated: the PRD's longest repair
(section 8.1), so the fuel plan never counts on power coming back sooner than it might."""


@dataclass(frozen=True, slots=True)
class SolverBeliefs:
    """Overrides laid over the caller's belief before planning (PRD section 8.7).

    They come from the caller's own reading of its observations (an LLM agent's judgment, a
    parser's facts), never from ground truth. Every field is optional:

    - ``blocked_roads`` / ``cleared_roads``: road segments to treat as blocked or open.
    - ``fuel_l`` / ``burn_lpm``: a hospital's liters in the tank now, or its burn rate.
    - ``priority``: a hospital's weight in the objective (default: its zone's priority weight,
      100 for critical).
    - ``unavailable_trucks``: trucks the plan must leave idle.
    - ``pin``: truck -> hospital; the truck's next trip must deliver there.
    """

    blocked_roads: tuple[EdgeId, ...] = ()
    cleared_roads: tuple[EdgeId, ...] = ()
    fuel_l: dict[str, int] = field(default_factory=dict)
    burn_lpm: dict[str, int] = field(default_factory=dict)
    priority: dict[str, int] = field(default_factory=dict)
    unavailable_trucks: tuple[str, ...] = ()
    pin: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HospitalNeed:
    id: str
    node: NodeId
    zone: str | None
    tank_l: int
    fuel_l: int
    burn_lpm: int
    weight: int


@dataclass(frozen=True, slots=True)
class DepotStock:
    id: str
    node: NodeId
    stock_l: int


@dataclass(frozen=True, slots=True)
class FixedDelivery:
    """Fuel already being unloaded: it lands ``minute`` minutes from now whatever we decide."""

    hospital: str
    minute: int
    liters: int


@dataclass(frozen=True, slots=True)
class TruckStart:
    """Where and when a truck can start its next trip, and with how much fuel.

    ``current`` is the trip it's on now, for the anti-thrash penalty: (depot, None) while it
    drives to load, (None, hospital) while it drives to deliver. ``loading_at`` is set while
    it's loading: its pickup is under way, so its next trip must be a direct delivery.
    ``fixed`` is a delivery it's unloading now.
    """

    id: str
    view: TruckView
    node: NodeId
    lead: int
    load_l: int
    capacity_l: int
    available: bool
    current: tuple[str | None, str | None] | None
    loading_at: str | None = None
    fixed: FixedDelivery | None = None
    pin: str | None = None
    route_ok: bool = True  # False if its current route crosses a road believed blocked


@dataclass(frozen=True, slots=True)
class CrewStart:
    id: str
    view: CrewView
    node: NodeId
    lead: int
    route_ok: bool = True


@dataclass(frozen=True, slots=True)
class PlanningState:
    """Everything the planners use, as plain integers. Minutes count from ``tick``."""

    tick: int
    city: City
    config: SimConfig
    blocked: frozenset[EdgeId]
    hospitals: tuple[HospitalNeed, ...]
    depots: tuple[DepotStock, ...]
    trucks: tuple[TruckStart, ...]
    crews: tuple[CrewStart, ...]
    zones: tuple[ZoneView, ...]
    substations: tuple[SubstationView, ...]
    repair_minutes: dict[str, int]  # down substation -> believed crew minutes to fix it

    def minutes(self, start: NodeId, end: NodeId) -> int | None:
        """Driving minutes on roads believed open, or None if there's no way through."""
        path = self.route(start, end)
        return None if path is None else path_minutes(self.city, path)

    def route(self, start: NodeId, end: NodeId) -> tuple[NodeId, ...] | None:
        path = shortest_path(self.city, start, end, self.blocked)
        return None if path is None else tuple(path)

    def hospital(self, hospital_id: str) -> HospitalNeed:
        return next(h for h in self.hospitals if h.id == hospital_id)

    def depot(self, depot_id: str) -> DepotStock:
        return next(d for d in self.depots if d.id == depot_id)


def planning_state(
    belief: BeliefState, overrides: SolverBeliefs, config: SimConfig
) -> PlanningState:
    """Snapshot ``belief``, with ``overrides`` laid over it, for planning."""
    city = belief.city
    if city is None:
        raise ValueError("the belief has no map yet: call belief.observe() first")
    blocked = (set(belief.blocked_roads()) | set(overrides.blocked_roads)) - set(
        overrides.cleared_roads
    )
    weight_of = {z.id: config.weight(z.priority) for z in belief.zones.values()}

    hospitals: list[HospitalNeed] = []
    for hospital_id in sorted(belief.hospitals):
        reading = belief.hospitals[hospital_id].reading
        fuel = overrides.fuel_l.get(hospital_id, belief.fuel_now(hospital_id))
        burn = overrides.burn_lpm.get(hospital_id, belief.burn(hospital_id))
        if reading is None or fuel is None or burn is None:
            continue  # nothing usable is known about it
        default = weight_of.get(reading.zone or "", config.weight(Priority.CRITICAL))
        hospitals.append(
            HospitalNeed(
                id=hospital_id,
                node=reading.node,
                zone=reading.zone,
                tank_l=reading.tank_l,
                fuel_l=max(0, min(fuel, reading.tank_l)),
                burn_lpm=max(0, burn),
                weight=overrides.priority.get(hospital_id, default),
            )
        )
    tanks = {h.id: h for h in hospitals}

    depots: list[DepotStock] = []
    for depot_id in sorted(belief.depots):
        known = belief.depots[depot_id]
        if known.reading is None or known.stock_l is None:
            continue
        assert isinstance(known.stock_l.value, int)
        depots.append(DepotStock(depot_id, known.reading.node, known.stock_l.value))

    trucks: list[TruckStart] = []
    for truck_id in sorted(belief.trucks):
        start = _truck_start(belief.trucks[truck_id], city, blocked, tanks)
        if truck_id in overrides.unavailable_trucks:
            start = replace(start, available=False)
        if truck_id in overrides.pin:
            start = replace(start, pin=overrides.pin[truck_id])
        trucks.append(start)
    # Fuel a truck is loading now comes out of its depot's stock.
    loading = {d.id: 0 for d in depots}
    for t in trucks:
        if t.loading_at is not None and t.loading_at in loading:
            loading[t.loading_at] += t.load_l - t.view.load_l
    depots = [DepotStock(d.id, d.node, max(0, d.stock_l - loading[d.id])) for d in depots]

    crews = tuple(_crew_start(belief.crews[c], city, blocked) for c in sorted(belief.crews))
    substations = tuple(
        b.reading for _, b in sorted(belief.substations.items()) if b.reading is not None
    )
    repair: dict[str, int] = {}
    for s in substations:
        if s.state.value == "online":
            continue
        known = belief.substations[s.id].eta_minutes
        if s.work_left is not None:
            repair[s.id] = s.work_left
        elif s.state.value == "tripped":
            repair[s.id] = config.reset_minutes
        elif known is not None and isinstance(known.value, int) and known.value > 0:
            repair[s.id] = known.value
        else:
            repair[s.id] = DEFAULT_REPAIR_MINUTES
    return PlanningState(
        tick=belief.tick,
        city=city,
        config=config,
        blocked=frozenset(blocked),
        hospitals=tuple(hospitals),
        depots=tuple(depots),
        trucks=tuple(trucks),
        crews=crews,
        zones=tuple(belief.zones[z] for z in sorted(belief.zones)),
        substations=substations,
        repair_minutes=repair,
    )


def _lead(city: City, at: NodeId, next_node: NodeId | None, elapsed: int) -> int:
    """Minutes until a vehicle driving a road reaches the node ahead (0 at a node)."""
    if next_node is None:
        return 0
    return city.edges[edge_id(at, next_node)].minutes - elapsed


def _route_ok(
    at: NodeId, next_node: NodeId | None, route: tuple[NodeId, ...], blocked: set[EdgeId]
) -> bool:
    path = route if next_node is not None else (at, *route)
    return not any(edge_id(a, b) in blocked for a, b in pairwise(path))


def _truck_start(
    truck: TruckView,
    city: City,
    blocked: set[EdgeId],
    tanks: dict[str, HospitalNeed],
) -> TruckStart:
    lead = _lead(city, truck.at, truck.next_node, truck.elapsed)
    task, state = truck.task, truck.state

    def start(node: NodeId, lead: int, load_l: int, **rest: object) -> TruckStart:
        return replace(
            TruckStart(truck.id, truck, node, lead, load_l, truck.capacity_l, True, None), **rest
        )

    if state is VehicleState.BROKEN:
        return start(truck.anchor, 0, truck.load_l, available=False)
    if state is VehicleState.LOADING and task is not None:
        room = truck.capacity_l - truck.load_l
        wanted = room if task.amount_l is None else min(room, task.amount_l)
        return start(  # the load is trimmed to the depot's stock by planning_state
            truck.at,
            truck.work_left,
            truck.load_l + wanted,
            current=(task.target_id, None),
            loading_at=task.target_id,
        )
    if state is VehicleState.UNLOADING and task is not None:
        need = tanks.get(task.target_id)
        wanted = truck.load_l if task.amount_l is None else min(truck.load_l, task.amount_l)
        room = wanted if need is None else max(0, need.tank_l - need.fuel_l)
        delivered = min(wanted, room)
        return start(
            truck.at,
            truck.work_left,
            truck.load_l - delivered,
            fixed=FixedDelivery(task.target_id, truck.work_left, delivered),
        )
    current: tuple[str | None, str | None] | None = None
    moving = state is VehicleState.MOVING
    if moving and task is not None and task.kind is TaskKind.DELIVER:
        current = (None, task.target_id)
    elif moving and task is not None and task.kind is TaskKind.LOAD:
        current = (task.target_id, None)
    ok = _route_ok(truck.at, truck.next_node, truck.route, blocked)
    return start(truck.anchor, lead, truck.load_l, current=current, route_ok=ok)


def _crew_start(crew: CrewView, city: City, blocked: set[EdgeId]) -> CrewStart:
    return CrewStart(
        id=crew.id,
        view=crew,
        node=crew.anchor,
        lead=_lead(city, crew.at, crew.next_node, crew.elapsed),
        route_ok=_route_ok(crew.at, crew.next_node, crew.route, blocked),
    )
