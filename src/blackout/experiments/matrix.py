"""Experiment matrices: which contestants run on which scenarios, how often (PRD 8.11).

A matrix is a YAML file in ``configs/matrices/``:

    experiment: dev_baselines      # runs go to runs/<experiment>/, tables to results/<experiment>/
    suite: dev-v0                  # scenarios/<suite>/ with its manifest.json
    contestants: [do-nothing, rules]
    runs: 1                        # run indices per scenario and contestant
    levels: [0, 1]                 # optional filters
    templates: [T01, T02]
"""

import json
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from blackout.controllers.registry import CONTESTANTS
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scenarios.suites import MANIFEST

SCENARIO_ROOT = Path("scenarios")
HELD_OUT_SUITES = frozenset({"test"})
"""Suites the batch runner refuses: the test suite needs a committed pre-registration (M12)."""


class Matrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    suite: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    contestants: tuple[str, ...] = Field(min_length=1)
    runs: PositiveInt = 1
    levels: tuple[int, ...] | None = None
    templates: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = [c for c in self.contestants if c not in CONTESTANTS]
        if unknown:
            raise ValueError(
                f"unknown contestants {unknown}; batch can run {', '.join(CONTESTANTS)}"
            )
        if len(set(self.contestants)) != len(self.contestants):
            raise ValueError("contestants are listed more than once")
        if self.suite in HELD_OUT_SUITES:
            raise ValueError(
                f"suite {self.suite!r} is held out: it needs docs/PREREGISTRATION.md (M12)"
            )
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
            path = folder / f"{entry['id']}.json"
            scenario = load_scenario(path)
            if scenario.content_hash != entry["content_hash"]:
                raise ValueError(f"{path} doesn't match its manifest entry")
            chosen.append((path, scenario))
        return chosen


def load_matrix(path: Path) -> Matrix:
    with path.open(encoding="utf-8") as handle:
        return Matrix.model_validate(yaml.safe_load(handle))
