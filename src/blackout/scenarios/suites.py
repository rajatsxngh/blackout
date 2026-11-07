"""Scenario suites: which templates, seeds and levels make up each one, and their files.

Each suite is written as one JSON file per scenario plus a ``manifest.json`` of hashes
(docs/PRD.md section 8.6). Only dev-v0 exists so far; dev and test are generated and frozen
in M8 (test with the human-written phrasing bank).
"""

import json
from dataclasses import dataclass
from pathlib import Path

from blackout.scenarios.config import CityConfig
from blackout.scenarios.generator import GENERATOR_VERSION, Request
from blackout.scenarios.schema import Scenario, Split
from blackout.scenarios.seeding import stable_seed
from blackout.scenarios.text.banks import PhrasingBank
from blackout.sim.hashing import digest

MANIFEST = "manifest.json"


@dataclass(frozen=True, slots=True)
class Suite:
    name: Split
    prefix: str
    templates: tuple[str, ...]
    seeds: int
    levels: tuple[int, ...]

    def requests(self) -> list[Request]:
        return [
            Request(
                template=template,
                seed=stable_seed(self.name, template, index),
                scenario_id=f"{self.prefix}_{template.lower()}_s{index}",
                split=self.name,
                levels=self.levels,
            )
            for template in self.templates
            for index in range(self.seeds)
        ]


SUITES: dict[str, Suite] = {
    "dev-v0": Suite("dev-v0", "dev0", ("T01", "T02", "T04"), seeds=3, levels=(0, 1)),
}


def manifest(
    suite: Suite, scenarios: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> dict[str, object]:
    entries = []
    for scenario in sorted(scenarios, key=lambda s: s.id):
        assert scenario.generator is not None
        assert scenario.validity is not None
        entries.append(
            {
                "id": scenario.id,
                "template": scenario.template,
                "seed": scenario.seed,
                "level": scenario.level,
                "content_hash": scenario.content_hash,
                "world_hash": scenario.generator.world_hash,
                "attempt": scenario.generator.attempt,
                "do_nothing_critical_downtime": scenario.validity.do_nothing_critical_downtime,
            }
        )
    return {
        "suite": suite.name,
        "generator_version": GENERATOR_VERSION,
        "config_digest": config.digest(),
        "bank": bank.name,
        "bank_digest": digest(bank.model_dump(mode="json")),
        "scenarios": entries,
    }


def scenario_json(scenario: Scenario) -> str:
    return json.dumps(scenario.model_dump(mode="json"), indent=2) + "\n"


def manifest_json(data: dict[str, object]) -> str:
    return json.dumps(data, indent=2) + "\n"


def write_suite(out: Path, scenarios: list[Scenario], data: dict[str, object]) -> list[Path]:
    """Write every scenario and the manifest. Files listed in an older manifest that this
    one no longer lists are removed (and nothing else in ``out`` is touched)."""
    out.mkdir(parents=True, exist_ok=True)
    keep = {f"{s.id}.json" for s in scenarios}
    old = out / MANIFEST
    if old.is_file():
        previous = json.loads(old.read_text(encoding="utf-8"))
        for entry in previous.get("scenarios", []):
            stale = out / f"{entry['id']}.json"
            if stale.name not in keep and stale.is_file():
                stale.unlink()
    written = []
    for scenario in scenarios:
        path = out / f"{scenario.id}.json"
        path.write_text(scenario_json(scenario), encoding="utf-8", newline="\n")
        written.append(path)
    (out / MANIFEST).write_text(manifest_json(data), encoding="utf-8", newline="\n")
    return written


def differences(out: Path, scenarios: list[Scenario], data: dict[str, object]) -> list[str]:
    """What differs between freshly generated scenarios and the files in ``out``."""
    problems = []
    for scenario in scenarios:
        path = out / f"{scenario.id}.json"
        if not path.is_file():
            problems.append(f"missing {path}")
        elif path.read_text(encoding="utf-8") != scenario_json(scenario):
            problems.append(f"{path} differs from a fresh generation")
    manifest_path = out / MANIFEST
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != manifest_json(
        data
    ):
        problems.append(f"{manifest_path} is missing or differs")
    return problems
