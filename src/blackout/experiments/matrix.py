"""Experiment matrices: which contestants run on which scenarios, how often (PRD 8.11).

A matrix is a YAML file in ``configs/matrices/``:

    experiment: dev_baselines      # runs go to runs/<experiment>/, tables to results/<experiment>/
    suite: dev-v0                  # scenarios/<suite>/ with its manifest.json
    contestants: [do-nothing, rules]
    runs: 1                        # run indices per scenario and contestant
    levels: [0, 1]                 # optional filters
    templates: [T01, T02]
    scenarios: [dev_t04_s0_l0]     # optional: only these scenario IDs

LLM contestants (``llm-parser``) also take:

    models: [claude-haiku-5-5]     # default: the dev model of configs/models.yaml
    effort: low                    # default: the contestant's effort in configs/models.yaml
    llm_mode: fixture              # fixture (recorded replies, the default) or live
    fixtures: tests/fixtures/llm_recordings   # fixture mode: where the recordings are
"""

import json
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from blackout.controllers.registry import LLM_CONTESTANTS, known_contestants
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scenarios.suites import MANIFEST

SCENARIO_ROOT = Path("scenarios")


class Matrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    suite: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    contestants: tuple[str, ...] = Field(min_length=1)
    runs: PositiveInt = 1
    levels: tuple[int, ...] | None = None
    templates: tuple[str, ...] | None = None
    scenarios_only: tuple[str, ...] | None = Field(default=None, alias="scenarios")
    models: tuple[str, ...] | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    llm_mode: Literal["fixture", "live"] = "fixture"
    fixtures: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        known = known_contestants()
        unknown = [c for c in self.contestants if c not in known]
        if unknown:
            raise ValueError(f"unknown contestants {unknown}; batch can run {', '.join(known)}")
        if self.models is not None and not any(c in LLM_CONTESTANTS for c in self.contestants):
            raise ValueError("models are given, but no contestant uses a model")
        if len(set(self.contestants)) != len(self.contestants):
            raise ValueError("contestants are listed more than once")
        if self.scenarios_only is not None and not self.scenarios_only:
            raise ValueError("scenarios, if given, must list at least one ID")
        return self

    def scenarios(self, root: Path = SCENARIO_ROOT) -> list[tuple[Path, Scenario]]:
        """The suite's scenarios that pass the filters, in manifest order."""
        folder = root / self.suite
        manifest = folder / MANIFEST
        if not manifest.is_file():
            raise FileNotFoundError(f"no {MANIFEST} in {folder}: generate the suite first")
        listed = json.loads(manifest.read_text(encoding="utf-8"))["scenarios"]
        chosen: list[tuple[Path, Scenario]] = []
        for entry in listed:
            if self.levels is not None and entry["level"] not in self.levels:
                continue
            if self.templates is not None and entry["template"] not in self.templates:
                continue
            if self.scenarios_only is not None and entry["id"] not in self.scenarios_only:
                continue
            path = folder / f"{entry['id']}.json"
            scenario = load_scenario(path)
            if scenario.content_hash != entry["content_hash"]:
                raise ValueError(f"{path} doesn't match its manifest entry")
            chosen.append((path, scenario))
        return chosen


def load_matrix(path: Path) -> Matrix:
    with path.open(encoding="utf-8") as handle:
        return Matrix.model_validate(yaml.safe_load(handle))
