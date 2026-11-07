"""Shared test configuration."""

import os

from hypothesis import settings

# "ci" is derandomized, so property tests explore the same examples on every run and every
# machine. "dev" keeps Hypothesis's normal random exploration. CI sets HYPOTHESIS_PROFILE=ci.
# "thorough" is for an occasional deep run: HYPOTHESIS_PROFILE=thorough uv run pytest
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("dev", deadline=None)
settings.register_profile("thorough", max_examples=2000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
