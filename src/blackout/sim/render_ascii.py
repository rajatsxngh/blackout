"""A plain-text picture of the city for debugging (``blackout run --render ascii``).

It shows ground truth, so it's for people, never for contestants. Road markers:
  #  blocked, and the block is known
  x  blocked, but nobody has discovered it yet
  ~  believed blocked, but actually clear again
Intersections show the hospital, depot, substation, truck or crew there ("+" means more than
one). Empty intersections are "." with power and ":" without.
"""

from blackout.sim.city import AVENUES, STREETS, edge_id, node_id
from blackout.sim.engine import Engine
from blackout.sim.entities import SubstationState, Vehicle, VehicleState

_ROW_LABEL = 7
_NODE = 3
_ROAD = 6
_COLUMN = _NODE + _ROAD


def render_ascii(engine: Engine) -> str:
    """The map, then one status line per hospital, depot, substation, vehicle and zone."""
    return "\n".join([*_map_lines(engine), "", *_status_lines(engine)])


def _map_lines(engine: Engine) -> list[str]:
    city = engine.city
    at_node: dict[str, list[str]] = {}
    on_road: dict[str, list[str]] = {}
    for places in (engine.hospitals, engine.depots, engine.substations):
        for place_id in sorted(places):
            at_node.setdefault(places[place_id].node, []).append(place_id)
    for fleet in (engine.trucks, engine.crews):
        for vehicle_id in sorted(fleet):
            vehicle = fleet[vehicle_id]
            if vehicle.next_node is None:
                at_node.setdefault(vehicle.at, []).append(vehicle_id)
            else:
                on_road.setdefault(edge_id(vehicle.at, vehicle.next_node), []).append(vehicle_id)
    dark = {node for zone in engine.zones.values() if not zone.powered for node in zone.nodes}

    def marker(segment: str) -> str | None:
        blocked, known = segment in engine.blocked, segment in engine.known_blocks
        if blocked:
            return "#" if known else "x"
        return "~" if known else None

    lines = [" " * _ROW_LABEL + "".join(s.ljust(_COLUMN) for s in STREETS[: city.width]).rstrip()]
    for y in range(city.height):
        row = AVENUES[y].ljust(_ROW_LABEL)
        for x in range(city.width):
            node = node_id(x, y)
            here = at_node.get(node, [])
            if here:
                label = here[0] + ("+" if len(here) > 1 else "")
            else:
                label = ":" if node in dark else "."
            row += label.ljust(_NODE)
            if x + 1 < city.width:
                segment = edge_id(node, node_id(x + 1, y))
                road = list("----- ")
                if (mark := marker(segment)) is not None:
                    road[2] = mark
                if segment in on_road:
                    road[1 : 1 + len(on_road[segment][0])] = on_road[segment][0]
                row += "".join(road)
        lines.append(row.rstrip())
        if y + 1 < city.height:
            row = " " * _ROW_LABEL
            for x in range(city.width):
                segment = edge_id(node_id(x, y), node_id(x, y + 1))
                drawn = on_road.get(segment, [marker(segment) or "|"])[0]
                row += drawn.ljust(_COLUMN)
            lines.append(row.rstrip())
    return lines


def _status_lines(engine: Engine) -> list[str]:
    lines = [f"t={engine.tick} of {engine.horizon}"]
    for hospital_id in sorted(engine.hospitals):
        h = engine.hospitals[hospital_id]
        power = "on grid" if h.powered else f"{h.status.value}, {h.ttf_minutes} min left"
        lines.append(f"  {h.id} {h.name} at {h.node}: {h.fuel_l}/{h.tank_l} L, {power}")
    for depot_id in sorted(engine.depots):
        d = engine.depots[depot_id]
        lines.append(f"  {d.id} {d.name} at {d.node}: {d.stock_l} L in stock")
    for substation_id in sorted(engine.substations):
        s = engine.substations[substation_id]
        state = s.state.value
        if s.state is SubstationState.REPAIRING:
            state += f" by {s.crew}, {s.work_left} min left"
        elif s.over_minutes:
            state += f", OVERLOADED {s.over_minutes} min"
        lines.append(f"  {s.id} {s.name} at {s.node}: {state}, {s.load_kw}/{s.capacity_kw} kW")
    for truck_id in sorted(engine.trucks):
        t = engine.trucks[truck_id]
        doing = t.state.value
        if t.task is not None:
            doing += f", task {t.task.kind.value} {t.task.target_id}"
        lines.append(f"  {t.id} {_where(t)}: {t.load_l}/{t.capacity_l} L, {doing}{_via(t)}")
    for crew_id in sorted(engine.crews):
        c = engine.crews[crew_id]
        doing = c.state.value + (f", for {c.substation}" if c.substation is not None else "")
        lines.append(f"  {c.id} {_where(c)}: {doing}{_via(c)}")
    if engine.zones:
        zones = ", ".join(
            f"{z.id} {'shed' if z.shed else (z.fed_by or 'dark')}"
            for z in (engine.zones[k] for k in sorted(engine.zones))
        )
        lines.append(f"  zones: {zones}")
    blocked = sorted(engine.blocked | engine.known_blocks)
    lines.append(f"  roads: {', '.join(blocked) if blocked else 'none blocked'}")
    return lines


def _where(vehicle: Vehicle) -> str:
    if vehicle.next_node is None:
        return f"at {vehicle.at}"
    return f"between {vehicle.at} and {vehicle.next_node}"


def _via(vehicle: Vehicle) -> str:
    if vehicle.route and vehicle.state in (VehicleState.MOVING, VehicleState.STUCK):
        return f" via {'>'.join(vehicle.route)}"
    return ""
