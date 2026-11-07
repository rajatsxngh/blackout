"""Scenario files are validated before the engine ever sees them."""

from typing import Any

import pytest
from pydantic import ValidationError

from blackout.scenarios.schema import load_scenario
from tests.helpers import CASCADE, DEMO, grid, make_scenario, substation, zone


def test_handmade_scenarios_load() -> None:
    demo = load_scenario(DEMO)
    assert demo.id == "hello_blackout"
    assert [h.id for h in demo.hospitals] == ["H1", "H2"]
    assert (len(demo.substations), len(demo.zones), len(demo.crews)) == (3, 9, 1)
    assert demo.events[0].kind == "road_block"
    cascade = load_scenario(CASCADE)
    assert cascade.events[0].kind == "substation_fail"


def test_version_1_files_are_rejected() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        make_scenario(schema_version=1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            grid(zones=[{**zone("ZA", "A", 4000, "residential", "S1", None), "nodes": ["Q7"]}]),
            "unknown intersection 'Q7'",
        ),
        (
            grid(
                zones=[
                    zone("ZA", "A", 4000, "residential", "S1", None),
                    {**zone("ZB", "B", 4000, "residential", "S1", None), "nodes": ["A1"]},
                ]
            ),
            "A1 is in both ZA and ZB",
        ),
        (grid(zones=[zone("ZA", "A", 4000, "residential", "S7", None)]), "unknown substation 'S7'"),
        (
            grid(zones=[zone("ZA", "A", 4000, "residential", "S1", "S1")]),
            "same primary and alternate",
        ),
        (grid(zones=[zone("ZC", "C", 5000, "residential", "S2", None)]), "must be critical"),
        (grid(substations=[substation("S1", "A2", 9000, state="failed")]), "repair_minutes"),
        (grid(substations=[substation("S1", "A2", 9000, repair_minutes=5)]), "repair_minutes"),
        (
            grid(
                events=[
                    {"kind": "substation_fail", "tick": 1, "substation": "S7", "repair_minutes": 5}
                ]
            ),
            "unknown substation 'S7'",
        ),
        (grid(crews=[{"id": "ZA", "node": "B2"}]), "duplicate id 'ZA'"),
    ],
)
def test_invalid_grids_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_scenario(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trucks": [{"id": "T1", "node": "Z9"}]}, "unknown intersection"),
        (
            {"depots": [{"id": "T1", "name": "Clash", "node": "A1", "stock_l": 5}]},
            "duplicate id 'T1'",
        ),
        ({"events": [{"kind": "road_block", "tick": 0, "edge": "A1-C3"}]}, "unknown road segment"),
        ({"events": [{"kind": "truck_breakdown", "tick": 0, "truck": "T7"}]}, "unknown truck"),
        (
            {"events": [{"kind": "burn_rate_change", "tick": 0, "hospital": "H7", "burn_lpm": 3}]},
            "unknown hospital",
        ),
        ({"events": [{"kind": "road_block", "tick": 60, "edge": "A1-A2"}]}, "before the horizon"),
        (
            {"city": {"width": 3, "height": 3, "travel_minutes": {"A1-C1": 1}}},
            "unknown road segment",
        ),
        (
            {
                "hospitals": [
                    {
                        "id": "H1",
                        "name": "X",
                        "node": "C3",
                        "tank_l": 100,
                        "fuel_l": 200,
                        "burn_lpm": 1,
                    }
                ]
            },
            "exceeds tank_l",
        ),
        (
            {"trucks": [{"id": "T1", "node": "A1", "capacity_l": 100, "load_l": 101}]},
            "exceeds capacity_l",
        ),
        ({"city": {"width": 12, "height": 3}}, "less than or equal to 8"),
        ({"surprise": True}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_scenarios_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_scenario(**overrides)
