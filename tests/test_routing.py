"""Deterministic fastest paths."""

from blackout.sim.city import build_grid_city
from blackout.sim.routing import path_minutes, shortest_path

ARTERIALS = {
    "A1-B1": 2,
    "B1-C1": 2,
    "C1-E1": 2,
    "E1-M1": 2,
    "M1-M2": 2,
    "M2-M3": 2,
    "M3-M4": 2,
    "M4-M5": 2,
}


def test_path_to_the_same_place() -> None:
    assert shortest_path(build_grid_city(3, 3), "B2", "B2") == ["B2"]


def test_fast_roads_win() -> None:
    city = build_grid_city(6, 6, minutes=ARTERIALS)
    path = shortest_path(city, "A1", "M5")
    assert path is not None
    assert path == ["A1", "B1", "C1", "E1", "M1", "M2", "M3", "M4", "M5"]
    assert path_minutes(city, path) == 16


def test_avoided_roads_force_a_detour() -> None:
    city = build_grid_city(6, 6, minutes=ARTERIALS)
    path = shortest_path(city, "M3", "M5", avoid={"M3-M4"})
    assert path is not None
    assert path == ["M3", "E3", "E4", "M4", "M5"]
    assert path_minutes(city, path) == 11


def test_unreachable_target() -> None:
    city = build_grid_city(3, 3)
    assert shortest_path(city, "A1", "C3", avoid={"B3-C3", "C2-C3"}) is None


def test_ties_are_broken_by_node_id() -> None:
    # Every path from A1 to C3 takes four 3-minute roads; the lowest node IDs win each tie.
    city = build_grid_city(3, 3)
    assert shortest_path(city, "A1", "C3") == ["A1", "A2", "A3", "B3", "C3"]
