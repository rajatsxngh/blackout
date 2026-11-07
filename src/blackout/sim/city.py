"""The road grid: intersections, road segments, travel times, names and the gazetteer.

North-south streets (columns, west to east) are Alder, Birch, Cedar, Elm, Maple, Oak, Pine and
Spruce. East-west avenues (rows, north to south) are 1st, 2nd, 3rd and so on. A node ID is the
street initial plus the avenue number, so "E4" is Elm & 4th. An edge ID joins its two node IDs
in sorted order: "E3-E4" is Elm St between 3rd and 4th, and "C4-E4" is 4th Ave between Cedar
and Elm. The street initials are alphabetical from west to east, so sorted order is map order.
"""

from collections.abc import Mapping
from dataclasses import dataclass

STREETS = ("Alder", "Birch", "Cedar", "Elm", "Maple", "Oak", "Pine", "Spruce")
AVENUES = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th")
MAX_WIDTH = len(STREETS)
MAX_HEIGHT = len(AVENUES)

type NodeId = str
type EdgeId = str


def node_id(x: int, y: int) -> NodeId:
    """ID of the intersection in column ``x`` (a street) and row ``y`` (an avenue), 0-based."""
    return f"{STREETS[x][0]}{y + 1}"


def edge_id(a: NodeId, b: NodeId) -> EdgeId:
    """ID of the road segment joining two adjacent intersections, in either order."""
    first, second = sorted((a, b))
    return f"{first}-{second}"


def node_ids(width: int, height: int) -> list[NodeId]:
    """All intersection IDs of a grid, sorted."""
    _check_size(width, height)
    return sorted(node_id(x, y) for x in range(width) for y in range(height))


def edge_ids(width: int, height: int) -> list[EdgeId]:
    """All road segment IDs of a grid, sorted."""
    _check_size(width, height)
    return [edge_id(a, b) for a, b in _segments(width, height)]


@dataclass(frozen=True, slots=True)
class Node:
    id: NodeId
    x: int
    y: int
    name: str


@dataclass(frozen=True, slots=True)
class Edge:
    id: EdgeId
    a: NodeId
    b: NodeId
    minutes: int
    name: str


@dataclass(frozen=True, slots=True)
class City:
    """An immutable road map. All of it is public: every contestant may see it."""

    width: int
    height: int
    nodes: Mapping[NodeId, Node]
    edges: Mapping[EdgeId, Edge]
    neighbors: Mapping[NodeId, tuple[NodeId, ...]]
    landmarks: Mapping[str, NodeId]
    gazetteer: Mapping[str, str]

    def edge_between(self, a: NodeId, b: NodeId) -> Edge | None:
        """The road segment joining ``a`` and ``b``, or None if they aren't adjacent."""
        return self.edges.get(edge_id(a, b))


def build_grid_city(
    width: int,
    height: int,
    *,
    default_minutes: int = 3,
    minutes: Mapping[EdgeId, int] | None = None,
    landmarks: Mapping[str, NodeId] | None = None,
) -> City:
    """Build a ``width`` x ``height`` grid with per-segment travel times (in whole minutes)."""
    _check_size(width, height)
    if default_minutes < 1:
        raise ValueError(f"travel times must be at least 1 minute, got {default_minutes}")
    overrides = dict(minutes or {})
    places = dict(landmarks or {})

    nodes = {
        node_id(x, y): Node(node_id(x, y), x, y, f"{STREETS[x]} & {AVENUES[y]}")
        for x in range(width)
        for y in range(height)
    }
    nodes = dict(sorted(nodes.items()))

    edges: dict[EdgeId, Edge] = {}
    for a, b in _segments(width, height):
        segment = edge_id(a, b)
        travel = overrides.pop(segment, default_minutes)
        if travel < 1:
            raise ValueError(f"travel time for {segment} must be at least 1 minute, got {travel}")
        edges[segment] = Edge(segment, a, b, travel, _edge_name(nodes[a], nodes[b]))
    if overrides:
        raise ValueError(f"travel times given for unknown road segments: {sorted(overrides)}")

    unknown_places = sorted(name for name, node in places.items() if node not in nodes)
    if unknown_places:
        raise ValueError(f"landmarks placed at unknown intersections: {unknown_places}")

    adjacent: dict[NodeId, list[NodeId]] = {node: [] for node in nodes}
    for edge in edges.values():
        adjacent[edge.a].append(edge.b)
        adjacent[edge.b].append(edge.a)

    return City(
        width=width,
        height=height,
        nodes=nodes,
        edges=edges,
        neighbors={node: tuple(sorted(around)) for node, around in adjacent.items()},
        landmarks=dict(sorted(places.items())),
        gazetteer=_gazetteer(nodes, edges, places),
    )


def _check_size(width: int, height: int) -> None:
    if not (2 <= width <= MAX_WIDTH and 2 <= height <= MAX_HEIGHT):
        raise ValueError(
            f"grid must be 2..{MAX_WIDTH} wide and 2..{MAX_HEIGHT} tall, got {width}x{height}"
        )


def _segments(width: int, height: int) -> list[tuple[NodeId, NodeId]]:
    """Adjacent intersection pairs, lower ID first, sorted."""
    pairs: list[tuple[NodeId, NodeId]] = []
    for x in range(width):
        for y in range(height):
            if x + 1 < width:
                pairs.append((node_id(x, y), node_id(x + 1, y)))
            if y + 1 < height:
                pairs.append((node_id(x, y), node_id(x, y + 1)))
    return sorted(pairs)


def _edge_name(a: Node, b: Node) -> str:
    if a.x == b.x:
        return f"{STREETS[a.x]} St between {AVENUES[a.y]} and {AVENUES[b.y]}"
    return f"{AVENUES[a.y]} Ave between {STREETS[a.x]} and {STREETS[b.x]}"


def _gazetteer(
    nodes: Mapping[NodeId, Node], edges: Mapping[EdgeId, Edge], landmarks: Mapping[str, NodeId]
) -> dict[str, str]:
    """Lower-case place names mapped to node or edge IDs (the report text engine adds more)."""
    names: dict[str, str] = {}
    for node in nodes.values():
        street, avenue = STREETS[node.x], AVENUES[node.y]
        for alias in (
            f"{street} & {avenue}",
            f"{avenue} & {street}",
            f"{street} and {avenue}",
            f"{avenue} and {street}",
            f"{street} st & {avenue} ave",
            node.id,
        ):
            names[alias.lower()] = node.id
    for edge in edges.values():
        a, b = nodes[edge.a], nodes[edge.b]
        if a.x == b.x:
            road, first, second, kind = STREETS[a.x], AVENUES[a.y], AVENUES[b.y], "st"
        else:
            road, first, second, kind = AVENUES[a.y], STREETS[a.x], STREETS[b.x], "ave"
        for alias in (
            f"{road} {kind} between {first} and {second}",
            f"{road} {kind} between {second} and {first}",
            f"{road} between {first} and {second}",
            f"{road} between {second} and {first}",
            edge.id,
        ):
            names[alias.lower()] = edge.id
    for place, node in landmarks.items():
        names[place.lower()] = node
    return dict(sorted(names.items()))
