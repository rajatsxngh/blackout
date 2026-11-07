"""Observations: the only window contestants have onto the world (invariant I2).

M1-M2 provide the full view: every piece of telemetry, with no delay and no role filtering. It
still never includes hidden truth: road blocks nobody has discovered, how long a failed
substation will take to repair (until a crew is on site), or future events. M4 adds roles,
telemetry delays and radio reports on top of this.
"""

from dataclasses import asdict, dataclass, field, fields

from blackout.sim.city import City, EdgeId, NodeId
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
from blackout.sim.hashing import digest
from blackout.sim.triggers import Trigger


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


@dataclass(frozen=True, slots=True)
class DepotView:
    id: str
    name: str
    node: NodeId
    stock_l: int


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
class Observation:
    role: str
    tick: int
    horizon: int
    triggers: tuple[Trigger, ...]
    trucks: tuple[TruckView, ...]
    crews: tuple[CrewView, ...]
    hospitals: tuple[HospitalView, ...]
    depots: tuple[DepotView, ...]
    substations: tuple[SubstationView, ...]
    zones: tuple[ZoneView, ...]
    known_blocks: tuple[EdgeId, ...]
    city: City = field(repr=False)

    def truck(self, truck_id: str) -> TruckView:
        return next(t for t in self.trucks if t.id == truck_id)

    def crew(self, crew_id: str) -> CrewView:
        return next(c for c in self.crews if c.id == crew_id)

    def hospital(self, hospital_id: str) -> HospitalView:
        return next(h for h in self.hospitals if h.id == hospital_id)

    def depot(self, depot_id: str) -> DepotView:
        return next(d for d in self.depots if d.id == depot_id)

    def substation(self, substation_id: str) -> SubstationView:
        return next(s for s in self.substations if s.id == substation_id)

    def zone(self, zone_id: str) -> ZoneView:
        return next(z for z in self.zones if z.id == zone_id)

    def to_dict(self) -> dict[str, object]:
        """Everything in the observation except the static map, in JSON-ready form."""
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self) if f.name != "city"}

    def digest(self) -> str:
        """A content hash, so run logs can record exactly what a contestant was shown."""
        return digest(self.to_dict())


def _plain(value: object) -> object:
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)  # type: ignore[arg-type]
    return value


def full_view(engine: Engine, role: str, triggers: tuple[Trigger, ...]) -> Observation:
    """Everything telemetry shows right now. Excludes hidden truth and future events."""
    return Observation(
        role=role,
        tick=engine.tick,
        horizon=engine.horizon,
        triggers=triggers,
        trucks=tuple(_truck(engine.trucks[k]) for k in sorted(engine.trucks)),
        crews=tuple(_crew(engine.crews[k]) for k in sorted(engine.crews)),
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
            )
            for h in (engine.hospitals[k] for k in sorted(engine.hospitals))
        ),
        depots=tuple(
            DepotView(id=d.id, name=d.name, node=d.node, stock_l=d.stock_l)
            for d in (engine.depots[k] for k in sorted(engine.depots))
        ),
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
        city=engine.city,
    )


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
