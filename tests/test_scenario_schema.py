"""Scenario files are validated before the engine ever sees them."""

from typing import Any

import pytest
from pydantic import ValidationError

from blackout.scenarios.schema import load_scenario
from tests.helpers import DEMO, make_scenario


def test_demo_scenario_loads() -> None:
    scenario = load_scenario(DEMO)
    assert scenario.id == "hello_blackout"
    assert [h.id for h in scenario.hospitals] == ["H1", "H2"]
    assert scenario.events[0].kind == "road_block"


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
