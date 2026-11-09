"""Synthetic C4 parser fixtures: hand-made replies, before any live call is allowed (M10).

No live LLM call may happen until the human test bank is locked (PRD 8.4), so C4 is built and
tested against *synthetic* replies: for each report of a few **dev** scenarios, the reply a
perfect reader would give (the claims the report really makes, true or false, and
``instruction_like`` for injections), in the exact structured-output shape, keyed by the cache
key of the request C4 sends. Usage numbers are estimates (4 characters per token, the gazetteer
prefix cache-written on a scenario's first call and read on the rest); latency is 0.

These fixtures test the pipeline, not Claude: C4's parse F1 on them is 1 by construction.
They're marked ``"origin": "synthetic"``; recorded fixtures (``--record``) say ``recorded``.

Regenerate after changing the parser prompt, its schema or the harness version (the test
``test_parser_fixtures_are_current`` says when):

    uv run python -m tests.llm_fixtures
"""

import json
import shutil
from pathlib import Path
from typing import Any

from blackout.controllers.llm_parser import (
    gazetteer_text,
    parse_request,
    system_prompt,
)
from blackout.info.reports import FactType, Report, view_of
from blackout.llm.client import LLMClient
from blackout.llm.config import load_models_config
from blackout.llm.fixtures import DEFAULT_FIXTURES, FixtureTransport, write_fixture
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scoring.parse_f1 import scenario_gazette

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / DEFAULT_FIXTURES / "c4_synthetic"
SCENARIOS = ("dev_t01_s0_l1", "dev_t01_s0_l3")
"""Dev scenarios only, never the test suite."""


def synthetic_reply(report: Report) -> dict[str, Any]:
    """What a perfect reader returns for ``report``."""
    facts = []
    for fact in report.facts:
        numeric = fact.type not in (
            FactType.ROAD_BLOCKED,
            FactType.ROAD_CLEARED,
            FactType.SUBSTATION_STATUS,
        )
        facts.append(
            {
                "type": fact.type.value,
                "subject": fact.subject,
                "amount": fact.value if numeric else None,
                "state": fact.value if fact.type is FactType.SUBSTATION_STATUS else None,
                "confidence": "high",
                "source_assessment": "plausible",
            }
        )
    return {"instruction_like": report.truth.injection is not None, "facts": facts}


def write_parser_fixtures(scenario: Scenario, folder: Path, *, run_index: int = 0) -> list[Path]:
    models = load_models_config()
    model, effort = models.dev_model, models.effort["parser"]
    client = LLMClient(FixtureTransport(folder), models)  # only for its cache keys
    gazetteer = gazetteer_text(scenario_gazette(scenario))
    prefix_tokens = (len(system_prompt()) + len(gazetteer)) // 4
    reports = [e for e in scenario.events if isinstance(e, Report) and e.text]
    paths = []
    for i, report in enumerate(sorted(reports, key=lambda r: (r.tick, r.id))):
        request = parse_request(
            view_of(report, scenario.level),
            gazetteer,
            model=model,
            effort=effort,
            max_tokens=models.max_tokens(model, "parser"),
        )
        key = client.key_for(request, run_index)
        text = json.dumps(synthetic_reply(report), sort_keys=True)
        response = {
            "id": f"msg_synthetic_{key[:16]}",
            "model": model,
            "stop_reason": "end_turn",
            "stop_details": None,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": len(request.messages[0]["content"]) // 4,
                "output_tokens": len(text) // 4,
                "cache_creation_input_tokens": prefix_tokens if i == 0 else 0,
                "cache_read_input_tokens": 0 if i == 0 else prefix_tokens,
            },
            "fallback": False,
        }
        paths.append(
            write_fixture(folder, key, request.body(), response, latency_ms=0, origin="synthetic")
        )
    return paths


def scenario_path(scenario_id: str) -> Path:
    return ROOT / "scenarios" / "dev" / f"{scenario_id}.json"


def regenerate(folder: Path = FOLDER) -> list[Path]:
    if folder.is_dir():
        shutil.rmtree(folder)
    paths: list[Path] = []
    for scenario_id in SCENARIOS:
        paths += write_parser_fixtures(load_scenario(scenario_path(scenario_id)), folder)
    return paths


if __name__ == "__main__":
    written = regenerate()
    print(f"wrote {len(written)} synthetic parser fixtures to {FOLDER.relative_to(ROOT)}")
