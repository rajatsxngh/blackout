"""Checks an action using only what the contestant can see, never hidden truth.

The checks use the fleet's real-time status, public depot stock, and the road blocks that are
already known. A route through an undiscovered block is accepted: the truck finds the block
when it gets there.
"""

from collections.abc import Container, Sequence
from dataclasses import dataclass
from itertools import pairwise

from blackout.sim.actions import DispatchTruck, RejectReason, Reroute, vehicle_of
from blackout.sim.city import City, NodeId
from blackout.sim.engine import Engine
from blackout.sim.entities import TaskKind, TruckState
from blackout.sim.routing import shortest_path


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of a check. For accepted movement, ``route`` starts at the truck's anchor."""

    reason: RejectReason | None
    message: str = ""
    route: tuple[NodeId, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.reason is None


def check(engine: Engine, action: DispatchTruck | Reroute, moved: Container[str]) -> Verdict:
    """Validate ``action``. ``moved`` holds vehicles already given a route this decision point."""
    vehicle = vehicle_of(action)
    truck = engine.trucks.get(vehicle)
    if truck is None:
        return Verdict(RejectReason.UNKNOWN_VEHICLE, f"there is no vehicle {vehicle!r}")
    if vehicle in moved:
        return Verdict(RejectReason.CONFLICT, f"{vehicle} was already given a route this turn")
    if truck.state is TruckState.BROKEN:
        return Verdict(RejectReason.VEHICLE_BROKEN, f"{vehicle} is broken down")
    if truck.state in (TruckState.LOADING, TruckState.UNLOADING):
        busy = f"{vehicle} is {truck.state.value} for {truck.work_left} more min"
        return Verdict(RejectReason.VEHICLE_BUSY, busy)

    if isinstance(action, DispatchTruck):
        target = _dispatch_target(engine, action)
        if isinstance(target, Verdict):
            return target
    elif truck.task is None:
        return Verdict(RejectReason.NO_TASK, f"{vehicle} has no task to reroute; dispatch it")
    else:
        target = truck.task.target_node

    unknown = sorted({e for e in action.avoid_edges if e not in engine.city.edges})
    if unknown:
        return Verdict(RejectReason.UNKNOWN_EDGE, f"no such road segment: {', '.join(unknown)}")
    if action.route is not None:
        problem = route_problem(engine.city, action.route, truck.anchor, target)
        if problem is not None:
            return Verdict(RejectReason.BAD_ROUTE, problem)
        return Verdict(None, route=tuple(action.route))
    closed = engine.known_blocks | set(action.avoid_edges)
    path = shortest_path(engine.city, truck.anchor, target, closed)
    if path is None:
        no_route = f"no route from {truck.anchor} to {target} avoiding known and listed blocks"
        return Verdict(RejectReason.NO_ROUTE, no_route)
    return Verdict(None, route=tuple(path))


def route_problem(city: City, route: Sequence[NodeId], start: NodeId, end: NodeId) -> str | None:
    """Why ``route`` isn't a usable path from ``start`` to ``end``, or None if it is."""
    if not route:
        return "the route is empty"
    unknown = [node for node in route if node not in city.nodes]
    if unknown:
        return f"unknown intersection in route: {', '.join(unknown)}"
    if route[0] != start:
        return f"the route must start at {start}, where the vehicle can next turn"
    if route[-1] != end:
        return f"the route must end at {end}"
    for a, b in pairwise(route):
        if city.edge_between(a, b) is None:
            return f"{a} and {b} are not joined by a road"
    return None


def _dispatch_target(engine: Engine, action: DispatchTruck) -> NodeId | Verdict:
    """The node a dispatch drives to, or a rejection if the task doesn't fit the target."""
    truck = engine.trucks[action.truck_id]
    target_id = action.target_id
    if action.task is TaskKind.LOAD:
        depot = engine.depots.get(target_id)
        if depot is None:
            return Verdict(RejectReason.INVALID_TARGET, f"{target_id!r} is not a depot")
        if truck.load_l >= truck.capacity_l:
            return Verdict(RejectReason.TRUCK_FULL, f"{truck.id} is already full")
        if depot.stock_l == 0:
            return Verdict(RejectReason.DEPOT_EMPTY, f"{depot.id} has no fuel left")
        return depot.node
    if action.task is TaskKind.DELIVER:
        hospital = engine.hospitals.get(target_id)
        if hospital is None:
            return Verdict(RejectReason.INVALID_TARGET, f"{target_id!r} is not a hospital")
        if truck.load_l == 0:
            return Verdict(RejectReason.EMPTY_TRUCK, f"{truck.id} has no fuel to deliver")
        return hospital.node
    if target_id not in engine.city.nodes:
        return Verdict(RejectReason.INVALID_TARGET, f"{target_id!r} is not an intersection")
    return target_id
