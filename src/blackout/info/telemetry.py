"""Telemetry: the structured, always-true channel (docs/PRD.md section 8.4).

Telemetry covers the fleet, crews, substations, zones, discovered road blocks, hospitals and
depot stock. It never includes hidden truth: undiscovered blocks, how long a failed substation
will take to repair (until a crew is working on it), or future events.

The delay modifier makes hospital and depot readings ``delay`` ticks old. Each such reading
carries ``as_of``, the tick it was taken. Fleet status, the grid and road blocks are always
real-time.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from blackout.sim.city import EdgeId, NodeId
from blackout.sim.engine import Engine
from blackout.sim.entities import (
    Crew,
    HospitalStatus,
    Priority,
    SubstationState,
    Task,
    Truck,
    VehicleState,
)


@dataclass(frozen=True, slots=True)
class TruckView:
    id: str
    state: VehicleState
    at: NodeId
    next_node: NodeId | None
    elapsed: int
    route: tuple[NodeId, ...]
    load_l: int
    capacity_l: int
    task: Task | None
    work_left: int

    @property
    def anchor(self) -> NodeId:
        """Where a new route must start: the node ahead while driving a road, else here."""
        return self.at if self.next_node is None else self.next_node


@dataclass(frozen=True, slots=True)
class CrewView:
    id: str
    state: VehicleState
    at: NodeId
    next_node: NodeId | None
    elapsed: int
    route: tuple[NodeId, ...]
    substation: str | None

    @property
    def anchor(self) -> NodeId:
        return self.at if self.next_node is None else self.next_node


@dataclass(frozen=True, slots=True)
class HospitalView:
    """A hospital reading, taken at tick ``as_of`` (older than now under the delay modifier)."""

    id: str
    name: str
    node: NodeId
    zone: str | None
    status: HospitalStatus
    powered: bool
    fuel_l: int
    tank_l: int
    burn_lpm: int
    ttf_minutes: int | None
    as_of: int


@dataclass(frozen=True, slots=True)
class DepotView:
    id: str
    name: str
    node: NodeId
    stock_l: int
    as_of: int


@dataclass(frozen=True, slots=True)
class SubstationView:
    """Substation telemetry. ``work_left`` is known only once a crew is working on it."""

    id: str
    name: str
    node: NodeId
    state: SubstationState
    capacity_kw: int
    load_kw: int
    overloaded_minutes: int
    work_left: int | None
    crew: str | None


@dataclass(frozen=True, slots=True)
class ZoneView:
    id: str
    name: str
    nodes: tuple[NodeId, ...]
    priority: Priority
    demand_kw: int
    primary: str
    alternate: str | None
    shed: bool
    fed_by: str | None

    @property
    def powered(self) -> bool:
        return self.fed_by is not None


@dataclass(frozen=True, slots=True)
class Readings:
    """The delayable part of telemetry: hospital and depot readings."""

    hospitals: tuple[HospitalView, ...]
    depots: tuple[DepotView, ...]


def readings_now(engine: Engine) -> Readings:
    tick = engine.tick
    return Readings(
        hospitals=tuple(
            HospitalView(
                id=h.id,
                name=h.name,
                node=h.node,
                zone=h.zone,
                status=h.status,
                powered=h.powered,
                fuel_l=h.fuel_l,
                tank_l=h.tank_l,
                burn_lpm=h.burn_lpm,
                ttf_minutes=h.ttf_minutes,
                as_of=tick,
            )
            for h in (engine.hospitals[k] for k in sorted(engine.hospitals))
        ),
        depots=tuple(
            DepotView(id=d.id, name=d.name, node=d.node, stock_l=d.stock_l, as_of=tick)
            for d in (engine.depots[k] for k in sorted(engine.depots))
        ),
    )


class ReadingsHistory:
    """Keeps recent hospital and depot readings so they can be released ``delay`` ticks late.

    A reading is taken at every recorded tick, after that tick's events and before decisions.
    With no delay the live state is shown, so a contestant sees its own actions at once.
    """

    def __init__(self, delay: int) -> None:
        if delay < 0:
            raise ValueError(f"the telemetry delay can't be negative, got {delay}")
        self.delay = delay
        self._by_tick: dict[int, Readings] = {}

    def record(self, engine: Engine) -> None:
        if self.delay == 0:
            return
        self._by_tick[engine.tick] = readings_now(engine)
        for old in sorted(t for t in self._by_tick if t < engine.tick - self.delay):
            del self._by_tick[old]

    def released(self, engine: Engine) -> Readings:
        """The readings contestants may see at the engine's current tick."""
        if self.delay == 0 or not self._by_tick:
            return readings_now(engine)
        wanted = max(0, engine.tick - self.delay)
        available = [t for t in self._by_tick if t <= wanted]
        return self._by_tick[max(available) if available else min(self._by_tick)]


@dataclass(frozen=True, slots=True)
class Telemetry:
    trucks: tuple[TruckView, ...]
    crews: tuple[CrewView, ...]
    hospitals: tuple[HospitalView, ...]
    depots: tuple[DepotView, ...]
    substations: tuple[SubstationView, ...]
    zones: tuple[ZoneView, ...]
    known_blocks: tuple[EdgeId, ...]


def telemetry(engine: Engine, readings: Readings) -> Telemetry:
    """Every section of telemetry, unfiltered, with the given hospital and depot readings."""
    return Telemetry(
        trucks=tuple(_truck(engine.trucks[k]) for k in sorted(engine.trucks)),
        crews=tuple(_crew(engine.crews[k]) for k in sorted(engine.crews)),
        hospitals=readings.hospitals,
        depots=readings.depots,
        substations=tuple(
            SubstationView(
                id=s.id,
                name=s.name,
                node=s.node,
                state=s.state,
                capacity_kw=s.capacity_kw,
                load_kw=s.load_kw,
                overloaded_minutes=s.over_minutes,
                work_left=s.work_left if s.state is SubstationState.REPAIRING else None,
                crew=s.crew,
            )
            for s in (engine.substations[k] for k in sorted(engine.substations))
        ),
        zones=tuple(
            ZoneView(
                id=z.id,
                name=z.name,
                nodes=z.nodes,
                priority=z.priority,
                demand_kw=z.demand_kw,
                primary=z.primary,
                alternate=z.alternate,
                shed=z.shed,
                fed_by=z.fed_by,
            )
            for z in (engine.zones[k] for k in sorted(engine.zones))
        ),
        known_blocks=tuple(sorted(engine.known_blocks)),
    )


def by_id[T: (TruckView, CrewView, HospitalView, DepotView, SubstationView, ZoneView)](
    views: tuple[T, ...],
) -> Mapping[str, T]:
    return {view.id: view for view in views}


def _truck(truck: Truck) -> TruckView:
    return TruckView(
        id=truck.id,
        state=truck.state,
        at=truck.at,
        next_node=truck.next_node,
        elapsed=truck.elapsed,
        route=tuple(truck.route),
        load_l=truck.load_l,
        capacity_l=truck.capacity_l,
        task=truck.task,
        work_left=truck.work_left,
    )


def _crew(crew: Crew) -> CrewView:
    return CrewView(
        id=crew.id,
        state=crew.state,
        at=crew.at,
        next_node=crew.next_node,
        elapsed=crew.elapsed,
        route=tuple(crew.route),
        substation=crew.substation,
    )
