"""Deterministic fastest-path search on the road grid.

Ties between equally fast paths are broken by node ID, so every run on every platform picks the
same path. (NetworkX arrives in M4 for the dependency graph, where ordering doesn't matter.)
"""

import heapq
from collections.abc import Container, Sequence
from itertools import pairwise

from blackout.sim.city import City, EdgeId, NodeId, edge_id


def shortest_path(
    city: City, source: NodeId, target: NodeId, avoid: Container[EdgeId] = ()
) -> list[NodeId] | None:
    """Fastest path from ``source`` to ``target`` as nodes, including both ends.

    Road segments in ``avoid`` are treated as closed. Returns None if ``target`` can't be reached.
    """
    if source == target:
        return [source]
    best: dict[NodeId, int] = {source: 0}
    came_from: dict[NodeId, NodeId] = {}
    frontier: list[tuple[int, NodeId]] = [(0, source)]
    settled: set[NodeId] = set()
    while frontier:
        minutes, node = heapq.heappop(frontier)
        if node in settled:
            continue
        if node == target:
            break
        settled.add(node)
        for neighbor in city.neighbors[node]:
            segment = edge_id(node, neighbor)
            if segment in avoid:
                continue
            arrival = minutes + city.edges[segment].minutes
            if neighbor not in best or arrival < best[neighbor]:
                best[neighbor] = arrival
                came_from[neighbor] = node
                heapq.heappush(frontier, (arrival, neighbor))
    if target not in came_from:
        return None
    path = [target]
    while path[-1] != source:
        path.append(came_from[path[-1]])
    path.reverse()
    return path


def path_minutes(city: City, path: Sequence[NodeId]) -> int:
    """Total driving minutes along ``path``. Consecutive nodes must be adjacent."""
    return sum(city.edges[edge_id(a, b)].minutes for a, b in pairwise(path))
