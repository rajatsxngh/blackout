"""Scenario templates: physical situations (docs/PRD.md section 8.6), T01 to T10."""

from blackout.scenarios.templates.common import Template
from blackout.scenarios.templates.t01 import BlockedFuelRoute
from blackout.scenarios.templates.t02 import Cascade
from blackout.scenarios.templates.t03 import TwoHospitalsOneTruck
from blackout.scenarios.templates.t04 import BreakdownMidDelivery
from blackout.scenarios.templates.t05 import BurnSpike
from blackout.scenarios.templates.t06 import DepotShortage
from blackout.scenarios.templates.t07 import CrewDilemma
from blackout.scenarios.templates.t08 import RoadReopening
from blackout.scenarios.templates.t09 import Gridlock
from blackout.scenarios.templates.t10 import CompoundStorm

TEMPLATES: dict[str, Template] = {
    t.id: t
    for t in (
        BlockedFuelRoute(),
        Cascade(),
        TwoHospitalsOneTruck(),
        BreakdownMidDelivery(),
        BurnSpike(),
        DepotShortage(),
        CrewDilemma(),
        RoadReopening(),
        Gridlock(),
        CompoundStorm(),
    )
}

__all__ = ["TEMPLATES", "Template"]
