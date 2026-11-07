"""Observations: the only window contestants have onto the world (invariant I2).

M1 provides the full view: every piece of telemetry, with no delay and no role filtering. It
still never includes hidden truth, such as road blocks nobody has discovered yet. M4 adds
roles, telemetry delays and radio reports on top of this.
"""

from dataclasses import dataclass, field

from blackout.sim.city import City, EdgeId, NodeId
from blackout.sim.engine import Engine
from blackout.sim.entities import HospitalStatus, Task, TruckState
from blackout.sim.triggers import Trigger


@dataclass(frozen=True, slots=True)
class TruckView:
    id: str
    state: TruckState
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
class HospitalView:
    id: str
    name: str
    node: NodeId
    status: HospitalStatus
    fuel_l: int
    tank_l: int
    burn_lpm: int
    grid_powered: bool
    ttf_minutes: int | None


@dataclass(frozen=True, slots=True)
class DepotView:
    id: str
    name: str
    node: NodeId
    stock_l: int


@dataclass(frozen=True, slots=True)
class Observation:
    role: str
    tick: int
    horizon: int
    triggers: tuple[Trigger, ...]
    trucks: tuple[TruckView, ...]
    hospitals: tuple[HospitalView, ...]
    depots: tuple[DepotView, ...]
    known_blocks: tuple[EdgeId, ...]
    city: City = field(repr=False)

    def truck(self, truck_id: str) -> TruckView:
        return next(t for t in self.trucks if t.id == truck_id)

    def hospital(self, hospital_id: str) -> HospitalView:
        return next(h for h in self.hospitals if h.id == hospital_id)

    def depot(self, depot_id: str) -> DepotView:
        return next(d for d in self.depots if d.id == depot_id)


def full_view(engine: Engine, role: str, triggers: tuple[Trigger, ...]) -> Observation:
    """Everything telemetry shows right now. Excludes hidden road blocks and future events."""
    trucks = tuple(
        TruckView(
            id=t.id,
            state=t.state,
            at=t.at,
            next_node=t.next_node,
            elapsed=t.elapsed,
            route=tuple(t.route),
            load_l=t.load_l,
            capacity_l=t.capacity_l,
            task=t.task,
            work_left=t.work_left,
        )
        for t in (engine.trucks[k] for k in sorted(engine.trucks))
    )
    hospitals = tuple(
        HospitalView(
            id=h.id,
            name=h.name,
            node=h.node,
            status=h.status,
            fuel_l=h.fuel_l,
            tank_l=h.tank_l,
            burn_lpm=h.burn_lpm,
            grid_powered=h.grid_powered,
            ttf_minutes=h.ttf_minutes,
        )
        for h in (engine.hospitals[k] for k in sorted(engine.hospitals))
    )
    depots = tuple(
        DepotView(id=d.id, name=d.name, node=d.node, stock_l=d.stock_l)
        for d in (engine.depots[k] for k in sorted(engine.depots))
    )
    return Observation(
        role=role,
        tick=engine.tick,
        horizon=engine.horizon,
        triggers=triggers,
        trucks=trucks,
        hospitals=hospitals,
        depots=depots,
        known_blocks=tuple(sorted(engine.known_blocks)),
        city=engine.city,
    )
