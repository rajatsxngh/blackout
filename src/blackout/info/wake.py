"""Wake triggers computed from a role's own observation stream (invariants I2 and I6).

``changes`` compares two consecutive observations of one role and names what changed, so a
role can only be woken by something it can see. A hidden event (a road blocked where nobody has
driven yet, a fuel level the delayed telemetry hasn't released) changes nothing a role
observes, so it can't wake anyone. The baseline is the observation *after* the role's own
actions, so the immediate effects of an action never wake the role that took it (D027).

Report, confirm-reply, time-to-failure and heartbeat triggers come from the info layer; this
module covers the telemetry.
"""

from blackout.info.observation import Observation
from blackout.info.telemetry import by_id
from blackout.sim.city import edge_id
from blackout.sim.entities import SubstationState, VehicleState
from blackout.sim.triggers import Trigger, TriggerKind

_ACTIVE = (VehicleState.MOVING, VehicleState.STUCK, VehicleState.BROKEN)
_WORKING = (VehicleState.LOADING, VehicleState.UNLOADING)


def changes(before: Observation, after: Observation, *, trip_grace: int) -> list[Trigger]:
    """Triggers for what changed between two observations of the same role."""
    found: list[Trigger] = []
    found += _vehicle_changes(before, after)
    found += _substation_changes(before, after, trip_grace)
    found += _zone_changes(before, after)
    found += _hospital_changes(before, after)
    for segment in sorted(set(after.known_blocks) - set(before.known_blocks)):
        name = after.city.edges[segment].name
        found.append(Trigger(TriggerKind.ROAD_STATUS, segment, f"blocked: {name}"))
    return found


def _where(at: str, next_node: str | None) -> str:
    return f"at {at}" if next_node is None else f"between {at} and {next_node}"


def _vehicle_changes(before: Observation, after: Observation) -> list[Trigger]:
    found: list[Trigger] = []
    was_trucks, was_crews = by_id(before.trucks), by_id(before.crews)
    now_substations = by_id(after.substations)
    for truck in after.trucks:
        was = was_trucks.get(truck.id)
        if was is None or was.state is truck.state:
            continue
        found += _movement(truck.id, was.state, truck.state, truck.at, truck.next_node)
        if truck.state is VehicleState.STUCK:
            ahead = edge_id(truck.at, truck.route[0])
            detail = f"road ahead blocked: {ahead} ({after.city.edges[ahead].name})"
            found.append(Trigger(TriggerKind.VEHICLE_STUCK, truck.id, detail))
        if was.state in _WORKING and truck.state is VehicleState.IDLE and was.task is not None:
            if was.state is VehicleState.LOADING:
                detail = f"loaded {truck.load_l - was.load_l} L at {was.task.target_id}"
            else:
                detail = f"delivered {was.load_l - truck.load_l} L to {was.task.target_id}"
            found.append(Trigger(TriggerKind.TASK_COMPLETED, truck.id, detail))
    for crew in after.crews:
        was = was_crews.get(crew.id)
        if was is None or was.state is crew.state:
            continue
        found += _movement(crew.id, was.state, crew.state, crew.at, crew.next_node)
        if crew.state is VehicleState.STUCK:
            ahead = edge_id(crew.at, crew.route[0])
            detail = f"road ahead blocked: {ahead} ({after.city.edges[ahead].name})"
            found.append(Trigger(TriggerKind.VEHICLE_STUCK, crew.id, detail))
        finished = now_substations.get(was.substation or "")
        if (
            was.state is VehicleState.REPAIRING
            and finished is not None
            and finished.state is SubstationState.ONLINE
        ):
            found.append(Trigger(TriggerKind.TASK_COMPLETED, crew.id, f"{finished.id} back online"))
    return found


def _movement(
    vehicle_id: str, was: VehicleState, now: VehicleState, at: str, next_node: str | None
) -> list[Trigger]:
    where = _where(at, next_node)
    if now is VehicleState.BROKEN:
        return [Trigger(TriggerKind.VEHICLE_BROKEN, vehicle_id, f"broke down {where}")]
    if was is VehicleState.BROKEN:
        return [Trigger(TriggerKind.VEHICLE_REPAIRED, vehicle_id, f"running again {where}")]
    if was is VehicleState.MOVING and now not in _ACTIVE:
        return [Trigger(TriggerKind.VEHICLE_ARRIVED, vehicle_id, f"reached {at}")]
    return []


def _substation_changes(before: Observation, after: Observation, trip_grace: int) -> list[Trigger]:
    found: list[Trigger] = []
    was_substations = by_id(before.substations)
    for now in after.substations:
        was = was_substations.get(now.id)
        if was is None:
            continue
        if was.overloaded_minutes == 0 and now.overloaded_minutes > 0:
            load = f"{now.load_kw} kW on a {now.capacity_kw} kW substation"
            detail = f"{load}; it trips after {trip_grace} min overloaded"
            found.append(Trigger(TriggerKind.OVERLOAD, now.id, detail))
        if was.state is now.state:
            continue
        if now.state is SubstationState.FAILED:
            lost = f"; {was.crew}'s work on it was lost" if was.crew is not None else ""
            detail = f"failed{lost}"
        elif now.state is SubstationState.TRIPPED:
            detail = f"tripped after {trip_grace} min overloaded"
        elif now.state is SubstationState.REPAIRING:
            job = "repair" if was.state is SubstationState.FAILED else "reset"
            detail = f"{now.crew} started the {job}: {now.work_left} min"
        else:
            detail = "online"
        found.append(Trigger(TriggerKind.SUBSTATION_STATUS, now.id, detail))
    return found


def _zone_changes(before: Observation, after: Observation) -> list[Trigger]:
    found: list[Trigger] = []
    was_zones = by_id(before.zones)
    for now in after.zones:
        was = was_zones.get(now.id)
        if was is None or was.powered == now.powered:
            continue
        detail = f"powered by {now.fed_by}" if now.powered else "shed" if now.shed else "no power"
        found.append(Trigger(TriggerKind.ZONE_STATUS, now.id, detail))
    return found


def _hospital_changes(before: Observation, after: Observation) -> list[Trigger]:
    was_hospitals = by_id(before.hospitals)
    return [
        Trigger(TriggerKind.HOSPITAL_STATUS, now.id, f"now {now.status.value}")
        for now in after.hospitals
        if (was := was_hospitals.get(now.id)) is not None and was.status is not now.status
    ]
