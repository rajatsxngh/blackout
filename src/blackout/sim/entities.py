"""Mutable engine state. Units are whole liters, minutes and kilowatts."""

from dataclasses import dataclass, field
from enum import StrEnum

from blackout.sim.city import NodeId


class VehicleState(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    STUCK = "stuck"
    LOADING = "loading"
    UNLOADING = "unloading"
    REPAIRING = "repairing"
    BROKEN = "broken"


class HospitalStatus(StrEnum):
    ON_GRID = "on_grid"
    ON_GENERATOR = "on_generator"
    DARK = "dark"


class TaskKind(StrEnum):
    LOAD = "load"
    DELIVER = "deliver"
    MOVE = "move"


class SubstationState(StrEnum):
    ONLINE = "online"
    FAILED = "failed"
    TRIPPED = "tripped"
    REPAIRING = "repairing"


class Priority(StrEnum):
    CRITICAL = "critical"
    ESSENTIAL = "essential"
    RESIDENTIAL = "residential"


@dataclass(frozen=True, slots=True)
class Task:
    """What a truck is doing: where it's going, and what it does when it gets there."""

    kind: TaskKind
    target_id: str
    target_node: NodeId
    amount_l: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "target_id": self.target_id,
            "target_node": self.target_node,
            "amount_l": self.amount_l,
        }


@dataclass(slots=True, kw_only=True)
class Vehicle:
    """Anything that drives the road grid. While driving it's between ``at`` and ``next_node``."""

    id: str
    at: NodeId
    next_node: NodeId | None = None
    elapsed: int = 0
    route: list[NodeId] = field(default_factory=list)
    state: VehicleState = VehicleState.IDLE

    @property
    def anchor(self) -> NodeId:
        """Where a new route must start: the node ahead while driving a road, else here."""
        return self.at if self.next_node is None else self.next_node

    def _position(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "next_node": self.next_node,
            "elapsed": self.elapsed,
            "route": list(self.route),
            "state": self.state.value,
        }


@dataclass(slots=True, kw_only=True)
class Truck(Vehicle):
    """A fuel truck."""

    capacity_l: int
    load_l: int = 0
    task: Task | None = None
    work_left: int = 0
    resume_state: VehicleState | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            **self._position(),
            "capacity_l": self.capacity_l,
            "load_l": self.load_l,
            "task": None if self.task is None else self.task.to_dict(),
            "work_left": self.work_left,
            "resume_state": None if self.resume_state is None else self.resume_state.value,
        }


@dataclass(slots=True, kw_only=True)
class Crew(Vehicle):
    """A repair crew. ``substation`` is where it's heading, or what it's working on."""

    substation: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {**self._position(), "substation": self.substation}


@dataclass(slots=True, kw_only=True)
class Hospital:
    """A hospital with a backup generator. The generator burns fuel only while unpowered."""

    id: str
    name: str
    node: NodeId
    tank_l: int
    fuel_l: int
    burn_lpm: int
    zone: str | None = None  # None means off the grid: always on generator
    powered: bool = False
    status: HospitalStatus = HospitalStatus.ON_GENERATOR

    @property
    def ttf_minutes(self) -> int | None:
        """Minutes until the generator runs dry at the current burn rate; None while powered."""
        return None if self.powered else self.fuel_l // self.burn_lpm

    def current_status(self) -> HospitalStatus:
        if self.powered:
            return HospitalStatus.ON_GRID
        return HospitalStatus.ON_GENERATOR if self.fuel_l > 0 else HospitalStatus.DARK

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "node": self.node,
            "tank_l": self.tank_l,
            "fuel_l": self.fuel_l,
            "burn_lpm": self.burn_lpm,
            "zone": self.zone,
            "powered": self.powered,
            "status": self.status.value,
        }


@dataclass(slots=True, kw_only=True)
class Depot:
    id: str
    name: str
    node: NodeId
    stock_l: int

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "node": self.node, "stock_l": self.stock_l}


@dataclass(slots=True, kw_only=True)
class Substation:
    """A substation. ``repair_minutes`` is hidden truth: how long its damage takes to fix."""

    id: str
    name: str
    node: NodeId
    capacity_kw: int
    state: SubstationState = SubstationState.ONLINE
    repair_minutes: int | None = None
    work_left: int = 0
    crew: str | None = None
    over_minutes: int = 0
    load_kw: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "node": self.node,
            "capacity_kw": self.capacity_kw,
            "state": self.state.value,
            "repair_minutes": self.repair_minutes,
            "work_left": self.work_left,
            "crew": self.crew,
            "over_minutes": self.over_minutes,
            "load_kw": self.load_kw,
        }


@dataclass(slots=True, kw_only=True)
class Zone:
    """A power zone: fed by its primary substation, else its alternate, unless shed."""

    id: str
    name: str
    nodes: tuple[NodeId, ...]
    demand_kw: int
    priority: Priority
    primary: str
    alternate: str | None = None
    shed: bool = False
    fed_by: str | None = None

    @property
    def powered(self) -> bool:
        return self.fed_by is not None

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "shed": self.shed, "fed_by": self.fed_by}
