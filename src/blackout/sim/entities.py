"""Mutable engine state: trucks, hospitals and depots. Units are whole liters and minutes."""

from dataclasses import dataclass, field
from enum import StrEnum

from blackout.sim.city import NodeId


class TruckState(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    LOADING = "loading"
    UNLOADING = "unloading"
    STUCK = "stuck"
    BROKEN = "broken"


class HospitalStatus(StrEnum):
    ON_GRID = "on_grid"
    ON_GENERATOR = "on_generator"
    DARK = "dark"


class TaskKind(StrEnum):
    LOAD = "load"
    DELIVER = "deliver"
    MOVE = "move"


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


@dataclass(slots=True)
class Truck:
    """A fuel truck. While driving it is on the road from ``at`` to ``next_node``."""

    id: str
    capacity_l: int
    load_l: int
    at: NodeId
    next_node: NodeId | None = None
    elapsed: int = 0
    route: list[NodeId] = field(default_factory=list)
    state: TruckState = TruckState.IDLE
    task: Task | None = None
    work_left: int = 0
    resume_state: TruckState | None = None

    @property
    def anchor(self) -> NodeId:
        """Where a new route must start: the node ahead while driving a road, else here."""
        return self.at if self.next_node is None else self.next_node

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "capacity_l": self.capacity_l,
            "load_l": self.load_l,
            "at": self.at,
            "next_node": self.next_node,
            "elapsed": self.elapsed,
            "route": list(self.route),
            "state": self.state.value,
            "task": None if self.task is None else self.task.to_dict(),
            "work_left": self.work_left,
            "resume_state": None if self.resume_state is None else self.resume_state.value,
        }


@dataclass(slots=True)
class Hospital:
    """A hospital with a backup generator. The generator burns fuel only without grid power."""

    id: str
    name: str
    node: NodeId
    tank_l: int
    fuel_l: int
    burn_lpm: int
    grid_powered: bool
    status: HospitalStatus = HospitalStatus.ON_GENERATOR

    @property
    def ttf_minutes(self) -> int | None:
        """Minutes until the generator runs dry at the current burn rate; None while on grid."""
        return None if self.grid_powered else self.fuel_l // self.burn_lpm

    def current_status(self) -> HospitalStatus:
        if self.grid_powered:
            return HospitalStatus.ON_GRID
        return HospitalStatus.ON_GENERATOR if self.fuel_l > 0 else HospitalStatus.DARK

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "node": self.node,
            "tank_l": self.tank_l,
            "fuel_l": self.fuel_l,
            "burn_lpm": self.burn_lpm,
            "grid_powered": self.grid_powered,
            "status": self.status.value,
        }


@dataclass(slots=True)
class Depot:
    id: str
    name: str
    node: NodeId
    stock_l: int

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "node": self.node, "stock_l": self.stock_l}
