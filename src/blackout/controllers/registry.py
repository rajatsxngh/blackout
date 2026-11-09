"""Contestants that can be built from their name (for ``blackout run`` and ``batch``).

The non-LLM contestants need nothing else. LLM contestants (``LLM_CONTESTANTS``) also need a
model, an effort and an ``LLMClient`` (``make_llm_controller``). The scripted demo controller
needs a script, so it isn't here.
"""

from collections.abc import Callable

from blackout.controllers.base import Controller
from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.llm_parser import LLMParserController
from blackout.controllers.rules import RulesController
from blackout.controllers.solver import ReferenceController, SolverController
from blackout.llm.client import LLMClient
from blackout.llm.config import Effort

CONTESTANTS: dict[str, Callable[[], Controller]] = {
    DoNothingController.name: DoNothingController,
    RulesController.name: RulesController,
    SolverController.name: SolverController,
    ReferenceController.name: ReferenceController,
}

LLM_CONTESTANTS: dict[str, str] = {LLMParserController.name: "parser"}
"""LLM contestant -> the purpose its effort and max_tokens are configured under."""


def known_contestants() -> list[str]:
    return [*CONTESTANTS, *LLM_CONTESTANTS]


def make_controller(name: str) -> Controller:
    """A fresh non-LLM contestant called ``name``; a KeyError names the ones that exist."""
    if name in LLM_CONTESTANTS:
        raise KeyError(f"{name!r} is an LLM contestant: build it with make_llm_controller")
    if name not in CONTESTANTS:
        raise KeyError(f"unknown contestant {name!r}; available: {', '.join(known_contestants())}")
    return CONTESTANTS[name]()


def make_llm_controller(
    name: str, client: LLMClient, *, model: str, effort: Effort, run_index: int = 0
) -> Controller:
    if name == LLMParserController.name:
        return LLMParserController(client, model=model, effort=effort, run_index=run_index)
    raise KeyError(f"unknown LLM contestant {name!r}; available: {', '.join(LLM_CONTESTANTS)}")
