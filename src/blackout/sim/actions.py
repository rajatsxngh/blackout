"""Actions contestants can take, and the result of trying one (docs/PRD.md section 8.3).

Movement actions (dispatch_truck, reroute, assign_crew) give a vehicle a path; a vehicle gets at
most one per decision point. Switching actions (shed_load, restore_load) act on a zone instantly.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from blackout.sim.entities import TaskKind

REASON_MAX_CHARS = 140


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(
        default="", max_length=REASON_MAX_CHARS, description="Why, in a few words (for replays)."
    )


class _Movement(_Action):
    route: tuple[str, ...] | None = Field(
        default=None,
        description="Exact path as intersection IDs, starting where the vehicle can next turn.",
    )
    avoid_edges: tuple[str, ...] = Field(
        default=(), description="Road segments to avoid when the engine plans the path."
    )

    @model_validator(mode="after")
    def _route_or_avoid(self) -> Self:
        if self.route is not None and self.avoid_edges:
            raise ValueError("give either route or avoid_edges, not both")
        return self


class DispatchTruck(_Movement):
    """Send a truck somewhere, and say what to do there."""

    kind: Literal["dispatch_truck"] = "dispatch_truck"
    truck_id: str
    task: TaskKind
    target_id: str = Field(description="A depot to load at, a hospital to deliver to, or a node.")
    amount_l: PositiveInt | None = Field(default=None, description="Liters; default is the most.")


class Reroute(_Movement):
    """Give a truck or crew a new path to the place it's already heading for."""

    kind: Literal["reroute"] = "reroute"
    vehicle_id: str


class AssignCrew(_Movement):
    """Send a crew to a substation to repair it (if failed) or reset it (if tripped)."""

    kind: Literal["assign_crew"] = "assign_crew"
    crew_id: str
    substation_id: str


class ShedLoad(_Action):
    """Disconnect a zone, taking its demand off the substation that feeds it."""

    kind: Literal["shed_load"] = "shed_load"
    zone_id: str


class RestoreLoad(_Action):
    """Reconnect a zone that was shed."""

    kind: Literal["restore_load"] = "restore_load"
    zone_id: str


type MovementAction = DispatchTruck | Reroute | AssignCrew
type SwitchAction = ShedLoad | RestoreLoad
type AnyAction = DispatchTruck | Reroute | AssignCrew | ShedLoad | RestoreLoad

Action = Annotated[
    DispatchTruck | Reroute | AssignCrew | ShedLoad | RestoreLoad, Field(discriminator="kind")
]


def vehicle_of(action: MovementAction) -> str:
    """The ID of the vehicle a movement action moves."""
    if isinstance(action, DispatchTruck):
        return action.truck_id
    if isinstance(action, AssignCrew):
        return action.crew_id
    return action.vehicle_id


class RejectReason(StrEnum):
    UNKNOWN_VEHICLE = "unknown_vehicle"
    CONFLICT = "conflict"
    VEHICLE_BROKEN = "vehicle_broken"
    VEHICLE_BUSY = "vehicle_busy"
    NO_TASK = "no_task"
    INVALID_TARGET = "invalid_target"
    TRUCK_FULL = "truck_full"
    DEPOT_EMPTY = "depot_empty"
    EMPTY_TRUCK = "empty_truck"
    UNKNOWN_EDGE = "unknown_edge"
    BAD_ROUTE = "bad_route"
    NO_ROUTE = "no_route"
    UNKNOWN_ZONE = "unknown_zone"
    ZONE_ALREADY_SHED = "zone_already_shed"
    ZONE_NOT_SHED = "zone_not_shed"


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What happened when a contestant tried an action. Never reveals hidden facts."""

    action: AnyAction
    accepted: bool
    message: str
    reason: RejectReason | None = None
    route: tuple[str, ...] = ()
    eta_minutes: int | None = None

    def summary(self) -> str:
        """One line for logs and the CLI, e.g. ``T1 deliver H1: ok, ...``."""
        action = self.action
        if isinstance(action, DispatchTruck):
            what = f"{action.truck_id} {action.task.value} {action.target_id}"
        elif isinstance(action, AssignCrew):
            what = f"{action.crew_id} repair {action.substation_id}"
        elif isinstance(action, Reroute):
            what = f"{action.vehicle_id} reroute"
        elif isinstance(action, ShedLoad):
            what = f"shed {action.zone_id}"
        else:
            what = f"restore {action.zone_id}"
        outcome = "ok" if self.accepted else f"REJECTED ({self.reason})"
        return f"{what}: {outcome}, {self.message}"
