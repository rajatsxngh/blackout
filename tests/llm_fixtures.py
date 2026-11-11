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

**Agent fixtures (M11).** The same idea for C5 Single and C6 Single+Solver, whose requests
depend on the whole trajectory (each turn's observation, tool results and the notes before it),
so they're *authored* by running the agent: ``Author`` answers each request as it's made and
the reply is saved under the request's cache key. The author is test code and may peek at what
a contestant may not (it reads the report truth from the scenario, like the C4 fixtures'
perfect reader); the agent itself sees only its observations. C6's author records its
verdicts, calls the solver once and accepts the plan; C5's looks around on its first turn,
then plays the Rules baseline's moves through the action tools. ``tool_use`` ids and usage
estimates come from the cache key and the request size, so authoring is deterministic. C6's
replies embed CP-SAT plans, which are deterministic per OS only (D084): they're authored on
Windows, and the tests that replay them run there.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from blackout.controllers.agents.single import SingleAgentController, SingleSolverController
from blackout.controllers.base import Actor
from blackout.controllers.llm_parser import (
    gazetteer_text,
    parse_request,
    system_prompt,
)
from blackout.controllers.rules import RulesController
from blackout.env import env_for, run_episode
from blackout.info.observation import Observation
from blackout.info.reports import Fact, FactType, Report, view_of
from blackout.llm.cache import canonical_json
from blackout.llm.client import LLMClient
from blackout.llm.config import load_models_config
from blackout.llm.fixtures import DEFAULT_FIXTURES, FixtureTransport, Sent, write_fixture
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scoring.parse_f1 import scenario_gazette
from blackout.sim.actions import AnyAction

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


# --- agents (M11) ------------------------------------------------------------------------------

AGENT_FOLDERS = {
    SingleAgentController.name: ROOT / DEFAULT_FIXTURES / "c5_synthetic",
    SingleSolverController.name: ROOT / DEFAULT_FIXTURES / "c6_synthetic",
}
AGENT_SCENARIOS = ("dev_t01_s0_l0", "dev_t02_s0_l1", "dev_t01_s0_l3")
"""Dev scenarios only (L0, L1 and L3, two templates), never the test suite."""

_PLAN = re.compile(r"^plan (\w+) at t=", re.MULTILINE)
_NO_OVERRIDES: dict[str, list[Any]] = {
    "blocked_roads": [],
    "cleared_roads": [],
    "fuel_l": [],
    "burn_lpm": [],
    "priority": [],
    "unavailable_trucks": [],
    "pin": [],
}

type Call = tuple[str, dict[str, Any]]


def tool_use(key: str, index: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{key[:16]}_{index}", "name": name, "input": args}


def action_call(action: AnyAction) -> Call:
    """The tool call that makes ``action`` (every field present, as strict mode sends it)."""
    data = action.model_dump(mode="json")
    kind = data.pop("kind")
    if "route" in data:
        data["route"] = data["route"] or None
        data["avoid_edges"] = data["avoid_edges"] or None
    return kind, data


def synthetic_claim(fact: Fact) -> dict[str, Any]:
    numeric = fact.type not in (
        FactType.ROAD_BLOCKED,
        FactType.ROAD_CLEARED,
        FactType.SUBSTATION_STATUS,
    )
    return {
        "type": fact.type.value,
        "subject": fact.subject,
        "amount": fact.value if numeric else None,
        "state": fact.value if fact.type is FactType.SUBSTATION_STATUS else None,
    }


def agent_reply(
    key: str, body: dict[str, Any], model: str, text: str, calls: list[Call], *, first: bool
) -> dict[str, Any]:
    """A ``tool_use`` reply making ``calls``, with usage estimated from the request's size."""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content += [tool_use(key, i, name, args) for i, (name, args) in enumerate(calls)]
    system = sum(len(block["text"]) for block in body["system"])
    prefix = (len(canonical_json(body.get("tools", []))) + system) // 4
    return {
        "id": f"msg_synthetic_{key[:16]}",
        "model": model,
        "stop_reason": "tool_use",
        "stop_details": None,
        "content": content,
        "usage": {
            "input_tokens": len(canonical_json(body["messages"])) // 4,
            "output_tokens": len(canonical_json(content)) // 4 + 40,  # and a little thinking
            "cache_creation_input_tokens": prefix if first else 0,
            "cache_read_input_tokens": 0 if first else prefix,
        },
        "fallback": False,
    }


class Author:
    """Answers an agent's requests with synthetic replies (see the module docstring)."""

    def __init__(self, scenario: Scenario, solver: bool, model: str) -> None:
        self.solver = solver
        self.model = model
        self.reports = {e.id: e for e in scenario.events if isinstance(e, Report)}
        self.rules = RulesController()
        self.observation: Observation | None = None
        self.decision = -1
        self.calls = 0

    def turn(self, observation: Observation) -> None:
        self.observation = observation
        self.decision += 1

    def verdicts(self) -> list[Call]:
        """``update_belief`` for every new report, as a perfect reader would judge it."""
        assert self.observation is not None
        calls: list[Call] = []
        for view in self.observation.reports:
            report = self.reports[view.id]
            truth = report.truth
            if not truth.is_true or truth.injection is not None or not report.facts:
                calls.append(
                    ("update_belief", {"report_id": view.id, "verdict": "reject", "facts": []})
                )
                continue
            facts = [synthetic_claim(f) for f in report.facts]
            calls.append(
                ("update_belief", {"report_id": view.id, "verdict": "accept", "facts": facts})
            )
        return calls

    def reply(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        call = (len(body["messages"]) + 1) // 2  # 1 for the opening request
        if self.solver:
            text, calls = self._solver_turn(body, call)
        else:
            text, calls = self._own_turn(call)
        first = self.calls == 0
        self.calls += 1
        return agent_reply(key, body, self.model, text, calls, first=first)

    def _solver_turn(self, body: dict[str, Any], call: int) -> tuple[str, list[Call]]:
        assert self.observation is not None
        if call == 1:
            calls = self.verdicts()
            if self.decision == 0:
                at_risk = {"query": "at_risk", "subject": None, "mode": None, "max_slack": 60}
                calls.append(("query_graph", at_risk))
            calls.append(("call_solver", {"overrides": _NO_OVERRIDES}))
            return "Recording the new reports, then planning.", calls
        plans = []
        for result in body["messages"][-1]["content"]:
            found = _PLAN.search(str(result.get("content", "")))
            if found and not result.get("is_error"):
                plans.append(found.group(1))
        calls = []
        if plans:
            accept = {"plan_id": plans[-1], "reason": "plan fits my beliefs", "edits": None}
            calls.append(("accept_plan", accept))
        notes = f"t{self.observation.tick}: carried out the solver's plan; watch the reports."
        calls.append(("end_turn", {"notes": notes}))
        return "The plan looks right; carrying it out.", calls

    def _own_turn(self, call: int) -> tuple[str, list[Call]]:
        assert self.observation is not None
        if self.decision == 0 and call == 1:
            at_risk = {"query": "at_risk", "subject": None, "mode": None, "max_slack": None}
            return "Looking around first.", [
                ("get_status", {"scope": "all"}),
                ("query_graph", at_risk),
            ]
        calls = self.verdicts() if call == 1 else []
        calls += [action_call(a) for a in self.rules.decide(self.observation)]
        notes = f"t{self.observation.tick}: serving hospitals earliest deadline first."
        calls.append(("end_turn", {"notes": notes}))
        return "Acting on the most urgent needs.", calls


class AuthoringTransport:
    """Asks the author for each reply and saves it as a synthetic fixture."""

    source = "fixture"

    def __init__(self, author: Author, folder: Path) -> None:
        self.author = author
        self.folder = folder

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        response = self.author.reply(key, body)
        write_fixture(self.folder, key, body, response, latency_ms=0, origin="synthetic")
        return Sent(response, 0)


def write_agent_fixtures(
    scenario: Scenario, contestant: str, folder: Path, *, run_index: int = 0
) -> None:
    """Run the agent on ``scenario`` with the author answering, saving every reply."""
    models = load_models_config()
    model, effort = models.dev_model, models.effort["agent"]
    solver = contestant == SingleSolverController.name
    base: type[SingleAgentController | SingleSolverController] = (
        SingleSolverController if solver else SingleAgentController
    )
    author = Author(scenario, solver, model)

    class Watched(base):  # type: ignore[misc,valid-type]  # the same contestant, but the author sees each turn's observation
        def decide_live(self, observation: Observation, actor: Actor) -> None:
            author.turn(observation)
            super().decide_live(observation, actor)

    client = LLMClient(AuthoringTransport(author, folder), models)
    agent = Watched(client, model=model, effort=effort, run_index=run_index)
    run_episode(env_for(scenario, agent), agent)


def regenerate_agents(folders: dict[str, Path] | None = None) -> int:
    written = 0
    for contestant, folder in (folders or AGENT_FOLDERS).items():
        if folder.is_dir():
            shutil.rmtree(folder)
        for scenario_id in AGENT_SCENARIOS:
            write_agent_fixtures(load_scenario(scenario_path(scenario_id)), contestant, folder)
        written += len(list(folder.glob("*.json")))
    return written


if __name__ == "__main__":
    written = regenerate()
    print(f"wrote {len(written)} synthetic parser fixtures to {FOLDER.relative_to(ROOT)}")
    count = regenerate_agents()
    names = ", ".join(f.name for f in AGENT_FOLDERS.values())
    print(f"wrote {count} synthetic agent fixtures to {names}")
