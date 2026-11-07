"""A plain-text picture of the city for debugging (``blackout run --render ascii``).

It shows ground truth, so it's for people, never for contestants. Road markers:
  #  blocked, and the block is known
  x  blocked, but nobody has discovered it yet
  ~  believed blocked, but actually clear again
Intersections show the hospital, depot or truck there ("+" means more than one).
"""

from blackout.sim.city import AVENUES, STREETS, edge_id, node_id
from blackout.sim.engine import Engine
from blackout.sim.entities import TruckState

_ROW_LABEL = 7
_NODE = 3
_ROAD = 6
_COLUMN = _NODE + _ROAD


def render_ascii(engine: Engine) -> str:
    """The map, then one status line per hospital, depot and truck."""
    return "\n".join([*_map_lines(engine), "", *_status_lines(engine)])


def _map_lines(engine: Engine) -> list[str]:
    city = engine.city
    at_node: dict[str, list[str]] = {}
    on_road: dict[str, list[str]] = {}
    for hospital_id in sorted(engine.hospitals):
        at_node.setdefault(engine.hospitals[hospital_id].node, []).append(hospital_id)
    for depot_id in sorted(engine.depots):
        at_node.setdefault(engine.depots[depot_id].node, []).append(depot_id)
    for truck_id in sorted(engine.trucks):
        truck = engine.trucks[truck_id]
        if truck.next_node is None:
            at_node.setdefault(truck.at, []).append(truck_id)
        else:
            on_road.setdefault(edge_id(truck.at, truck.next_node), []).append(truck_id)

    def marker(segment: str) -> str | None:
        blocked, known = segment in engine.blocked, segment in engine.known_blocks
        if blocked:
            return "#" if known else "x"
        return "~" if known else None

    lines = [" " * _ROW_LABEL + "".join(s.ljust(_COLUMN) for s in STREETS[: city.width]).rstrip()]
    for y in range(city.height):
        row = AVENUES[y].ljust(_ROW_LABEL)
        for x in range(city.width):
            here = at_node.get(node_id(x, y), [])
            label = "." if not here else here[0] + ("+" if len(here) > 1 else "")
            row += label.ljust(_NODE)
            if x + 1 < city.width:
                segment = edge_id(node_id(x, y), node_id(x + 1, y))
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
        power = "on grid" if h.grid_powered else f"{h.status.value}, {h.ttf_minutes} min left"
        lines.append(f"  {h.id} {h.name} at {h.node}: {h.fuel_l}/{h.tank_l} L, {power}")
    for depot_id in sorted(engine.depots):
        d = engine.depots[depot_id]
        lines.append(f"  {d.id} {d.name} at {d.node}: {d.stock_l} L in stock")
    for truck_id in sorted(engine.trucks):
        t = engine.trucks[truck_id]
        where = f"at {t.at}" if t.next_node is None else f"between {t.at} and {t.next_node}"
        doing = t.state.value
        if t.task is not None:
            doing += f", task {t.task.kind.value} {t.task.target_id}"
        if t.route and t.state in (TruckState.MOVING, TruckState.STUCK):
            doing += f" via {'>'.join(t.route)}"
        lines.append(f"  {t.id} {where}: {t.load_l}/{t.capacity_l} L, {doing}")
    blocked = sorted(engine.blocked | engine.known_blocks)
    lines.append(f"  roads: {', '.join(blocked) if blocked else 'none blocked'}")
    return lines
