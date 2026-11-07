"""Contestants that can be built from their name alone (for ``blackout run`` and ``batch``).

These are the non-LLM contestants; the scripted demo controller needs a script, so it isn't
here. LLM contestants arrive with M10-M11.
"""

from collections.abc import Callable

from blackout.controllers.base import Controller
from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.rules import RulesController

CONTESTANTS: dict[str, Callable[[], Controller]] = {
    DoNothingController.name: DoNothingController,
    RulesController.name: RulesController,
}


def make_controller(name: str) -> Controller:
    """A fresh contestant called ``name``; a KeyError names the ones that exist."""
    if name not in CONTESTANTS:
        raise KeyError(f"unknown contestant {name!r}; available: {', '.join(CONTESTANTS)}")
    return CONTESTANTS[name]()
