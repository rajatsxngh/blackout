"""Shared test configuration."""

import os
from collections.abc import Iterator

import pytest
from hypothesis import settings

from blackout.llm.client import AnthropicTransport

# "ci" is derandomized, so property tests explore the same examples on every run and every
# machine. "dev" keeps Hypothesis's normal random exploration. CI sets HYPOTHESIS_PROFILE=ci.
# "thorough" is for an occasional deep run: HYPOTHESIS_PROFILE=thorough uv run pytest
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("dev", deadline=None)
settings.register_profile("thorough", max_examples=2000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True, scope="session")
def _no_llm_network() -> Iterator[None]:
    """Tests never call the API (CLAUDE.md): the live transport fails loudly if reached.
    Session-scoped, so Hypothesis tests don't see a function-scoped fixture."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a test tried to call the Anthropic API; use fixtures or a fake")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AnthropicTransport, "send", refuse)
        patch.setattr(AnthropicTransport, "count_tokens", refuse)
        yield
