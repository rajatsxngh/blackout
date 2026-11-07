"""The road grid: IDs, names, travel times and the gazetteer."""

import pytest

from blackout.sim.city import build_grid_city, edge_id, edge_ids, node_ids


def test_default_grid_has_36_intersections_and_60_roads() -> None:
    city = build_grid_city(6, 6)
    assert len(city.nodes) == 36
    assert len(city.edges) == 60
    assert sorted(city.nodes) == node_ids(6, 6)
    assert sorted(city.edges) == edge_ids(6, 6)


def test_ids_and_names_follow_streets_and_avenues() -> None:
    city = build_grid_city(6, 6)
    assert city.nodes["E4"].name == "Elm & 4th"
    assert (city.nodes["E4"].x, city.nodes["E4"].y) == (3, 3)
    assert city.edges["E3-E4"].name == "Elm St between 3rd and 4th"
    assert city.edges["C4-E4"].name == "4th Ave between Cedar and Elm"


def test_edge_id_ignores_direction() -> None:
    assert edge_id("E4", "E3") == edge_id("E3", "E4") == "E3-E4"


def test_neighbors_are_sorted() -> None:
    city = build_grid_city(6, 6)
    assert city.neighbors["E4"] == ("C4", "E3", "E5", "M4")
    assert city.neighbors["A1"] == ("A2", "B1")


def test_travel_time_overrides() -> None:
    city = build_grid_city(3, 3, default_minutes=4, minutes={"A1-B1": 1})
    assert city.edges["A1-B1"].minutes == 1
    assert city.edges["B1-C1"].minutes == 4
    assert city.edge_between("B1", "A1") is city.edges["A1-B1"]
    assert city.edge_between("A1", "C1") is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"minutes": {"A1-C1": 2}}, "unknown road segments"),
        ({"minutes": {"A1-B1": 0}}, "at least 1 minute"),
        ({"default_minutes": 0}, "at least 1 minute"),
        ({"landmarks": {"Nowhere": "Z9"}}, "unknown intersections"),
    ],
)
def test_bad_city_specs_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_grid_city(3, 3, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(("width", "height"), [(1, 6), (9, 6), (6, 10)])
def test_bad_grid_sizes_are_rejected(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="grid must be"):
        build_grid_city(width, height)


def test_gazetteer_resolves_common_names() -> None:
    city = build_grid_city(6, 6, landmarks={"City Hall": "C3"})
    names = city.gazetteer
    assert names["elm & 4th"] == "E4"
    assert names["4th and elm"] == "E4"
    assert names["elm st between 3rd and 4th"] == "E3-E4"
    assert names["elm between 4th and 3rd"] == "E3-E4"
    assert names["4th ave between cedar and elm"] == "C4-E4"
    assert names["city hall"] == "C3"
    assert names["e3-e4"] == "E3-E4"
