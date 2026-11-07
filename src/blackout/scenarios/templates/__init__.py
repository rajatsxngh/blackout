"""Scenario templates: physical situations (docs/PRD.md section 8.6). M5 has T01, T02, T04."""

from blackout.scenarios.templates.common import Template
from blackout.scenarios.templates.t01 import BlockedFuelRoute
from blackout.scenarios.templates.t02 import Cascade
from blackout.scenarios.templates.t04 import BreakdownMidDelivery

TEMPLATES: dict[str, Template] = {
    t.id: t for t in (BlockedFuelRoute(), Cascade(), BreakdownMidDelivery())
}

__all__ = ["TEMPLATES", "Template"]
