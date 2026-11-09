"""Building a ready ``LLMClient`` for a run: fixture mode by default, live only when allowed.

- ``fixture``: replies come from recorded responses (``tests/fixtures/llm_recordings`` by
  default); a request with no recording is an error. Nothing is spent and nothing touches the
  network. The guard runs on an in-memory ledger, so the per-run cap and the 5x-median abort
  still apply.
- ``live``: refused (``LiveRefused``) until the human test bank is locked and committed and an
  API key is set (``blackout.llm.live``). Responses go through the SQLite cache and are charged
  to ``runs/ledger.json``; ``record`` also writes them as fixtures.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blackout.llm.budget import DEFAULT_LEDGER, BudgetGuard, Ledger, load_budget_config
from blackout.llm.cache import DEFAULT_CACHE, ResponseCache
from blackout.llm.client import AnthropicTransport, LLMClient
from blackout.llm.config import ModelsConfig, load_models_config
from blackout.llm.fixtures import DEFAULT_FIXTURES, FixtureTransport, RecordingTransport, Transport

type LLMMode = Literal["fixture", "live"]


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """How an LLM contestant gets its replies. Plain data, so batch jobs can carry it."""

    mode: LLMMode = "fixture"
    fixtures: Path = DEFAULT_FIXTURES
    cache: Path | None = None  # live default: runs/llm_cache.sqlite
    ledger: Path | None = DEFAULT_LEDGER  # live only; fixture mode keeps it in memory
    record: bool = False  # live: also write every response as a fixture
    root: Path = Path()


def make_client(settings: LLMSettings, models: ModelsConfig | None = None) -> LLMClient:
    models = models or load_models_config()
    budget = load_budget_config()
    transport: Transport
    if settings.mode == "live":
        from blackout.llm.live import require_live  # imports the test guard

        key = require_live(settings.root)
        transport = AnthropicTransport(key)
        if settings.record:
            transport = RecordingTransport(transport, settings.fixtures)
        cache = ResponseCache(settings.cache or DEFAULT_CACHE)
        guard = BudgetGuard(budget, Ledger.load(settings.ledger))
    else:
        transport = FixtureTransport(settings.fixtures)
        cache = None if settings.cache is None else ResponseCache(settings.cache)
        guard = BudgetGuard(budget, Ledger.load(None))
    return LLMClient(transport, models, cache=cache, guard=guard)
