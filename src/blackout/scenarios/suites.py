"""Scenario suites: which templates, seeds, levels and tiers make up each one, and their files.

Each suite is written as one JSON file per scenario plus a ``manifest.json`` of hashes
(docs/PRD.md section 8.6):

- **dev-v0** (M5): T01, T02, T04 x L0/L1 x 3 seeds, made by generator v1. It's frozen as it
  was (``FROZEN``): the M8 generator makes different worlds, and M5-M7's tests and results
  are about these. M8 only migrated its files to schema v5 (D071).
- **dev** (frozen in M8): T01, T02, T04, T05, T07, T08, T09 x L0-L3 x 2 seeds = 56.
- **test** (frozen in M8, held out): T03, T06, T10 x L0-L3 x 3 seeds = 36, rendered with the
  human-written test phrasing bank (``data/phrasing/test``), which must match its lock file.

Every suite has a tier plan: one tier per world (template x seed), in request order, that
calibration must hit. Each plan meets the PRD's 20/50/30 easy/medium/hard mix within ten
points (tested), and puts hard worlds where each template can make one.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from blackout.scenarios.config import CityConfig
from blackout.scenarios.generator import GENERATOR_VERSION, Request
from blackout.scenarios.schema import Scenario, Split, Tier
from blackout.scenarios.seeding import stable_seed
from blackout.scenarios.text.banks import TRAIN_BANK, PhrasingBank
from blackout.sim.hashing import digest

MANIFEST = "manifest.json"
TEST_BANK = Path("data") / "phrasing" / "test"
TIER_TARGET_PCT: dict[Tier, int] = {"easy": 20, "medium": 50, "hard": 30}
TIER_TOLERANCE_PCT = 10

E, M, H = "easy", "medium", "hard"

FROZEN: dict[str, str] = {
    "dev-v0": "dev-v0 is frozen as generator v1 made it (M5); it can't be regenerated (D071)",
}


@dataclass(frozen=True, slots=True)
class Suite:
    name: Split
    prefix: str
    templates: tuple[str, ...]
    seeds: int
    levels: tuple[int, ...]
    tiers: tuple[Tier, ...]  # one per world: template-major, then seed
    bank: Path = TRAIN_BANK

    def __post_init__(self) -> None:
        if len(self.tiers) != len(self.templates) * self.seeds:
            raise ValueError(f"{self.name}: one tier per world is needed")

    def requests(self) -> list[Request]:
        worlds = [(t, i) for t in self.templates for i in range(self.seeds)]
        return [
            Request(
                template=template,
                seed=stable_seed(self.name, template, index),
                scenario_id=f"{self.prefix}_{template.lower()}_s{index}",
                split=self.name,
                levels=self.levels,
                tier=tier,
            )
            for (template, index), tier in zip(worlds, self.tiers, strict=True)
        ]

    def tier_mix(self) -> dict[Tier, int]:
        return {tier: self.tiers.count(tier) for tier in TIER_TARGET_PCT}


SUITES: dict[str, Suite] = {
    "dev": Suite(
        "dev",
        "dev",
        ("T01", "T02", "T04", "T05", "T07", "T08", "T09"),
        seeds=2,
        levels=(0, 1, 2, 3),
        tiers=(M, M, M, E, H, M, E, M, H, M, H, E, H, M),
    ),
    "test": Suite(
        "test",
        "test",
        ("T03", "T06", "T10"),
        seeds=3,
        levels=(0, 1, 2, 3),
        tiers=(H, M, E, H, M, M, H, M, E),
        bank=TEST_BANK,
    ),
}


def manifest(
    suite: Suite, scenarios: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> dict[str, object]:
    entries = []
    for scenario in sorted(scenarios, key=lambda s: s.id):
        assert scenario.generator is not None
        assert scenario.validity is not None
        v = scenario.validity
        entries.append(
            {
                "id": scenario.id,
                "template": scenario.template,
                "seed": scenario.seed,
                "level": scenario.level,
                "tier": scenario.tier,
                "content_hash": scenario.content_hash,
                "world_hash": scenario.generator.world_hash,
                "attempt": scenario.generator.attempt,
                "do_nothing_critical_downtime": v.do_nothing_critical_downtime,
                "reference_critical_downtime": v.reference_critical_downtime,
                "reference_min_slack": v.reference_min_slack,
                "rules_l0_critical_downtime": v.rules_l0_critical_downtime,
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
