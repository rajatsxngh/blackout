"""C4 Parser + Solver (M10): the LLM reads reports into C2's belief, in fixture mode.

Done-when checks (docs/PRD.md section 12, M10, the parts that need no live call): C4 runs end
to end on a dev scenario from recorded fixtures and replays with ``blackout replay``; its run
logs are deterministic; its parse F1 is scored from the logs; CI makes no network calls.
"""

import filecmp
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from blackout.cli import app
from blackout.controllers.llm_parser import (
    LLMParserController,
    ParsedFact,
    gazetteer_text,
    known_subjects,
    parse_request,
    prompt_hash,
    report_message,
    system_prompt,
    to_fact,
)
from blackout.controllers.registry import make_llm_controller
from blackout.controllers.solver import SolverController
from blackout.env import env_for
from blackout.experiments.matrix import Matrix
from blackout.experiments.results import write_results
from blackout.experiments.runner import plan, run_batch
from blackout.info.reports import Fact, FactType, Report, view_of
from blackout.llm.budget import BudgetGuard, Ledger, load_budget_config
from blackout.llm.client import LLMClient
from blackout.llm.config import load_models_config
from blackout.llm.fixtures import FixtureMissing, FixtureTransport, Sent
from blackout.logs.reader import read_log
from blackout.logs.replay import replay
from blackout.logs.schema import SUMMARY_FILE, TRANSCRIPTS_FILE
from blackout.logs.writer import record_episode
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scoring.parse_f1 import logged_parse_counts, scenario_gazette
from tests import llm_fixtures
from tests.helpers import ROOT

MODELS = load_models_config()
FIXTURES = ROOT / "tests" / "fixtures" / "llm_recordings"
L1, L3 = llm_fixtures.SCENARIOS
runner = CliRunner()


def scenario(scenario_id: str) -> Scenario:
    return load_scenario(llm_fixtures.scenario_path(scenario_id))


def fixture_client(folder: Path = FIXTURES) -> LLMClient:
    guard = BudgetGuard(load_budget_config(), Ledger.load(None))
    return LLMClient(FixtureTransport(folder), MODELS, guard=guard)


def c4(client: LLMClient | None = None, run_index: int = 0) -> LLMParserController:
    return LLMParserController(
        client or fixture_client(),
        model=MODELS.dev_model,
        effort=MODELS.effort["parser"],
        run_index=run_index,
    )


def run(scenario_id: str, out: Path, controller: Any = None) -> Path:
    controller = controller or c4()
    loaded = scenario(scenario_id)
    record_episode(env_for(loaded, controller), controller, out)
    return out


# --- reading -------------------------------------------------------------------------------


def test_parsed_facts_resolve_through_the_gazetteer() -> None:
    known = known_subjects(scenario_gazette(scenario(L1)))

    def parsed(
        kind: str, subject: str, amount: int | None = None, state: Any = None
    ) -> Fact | None:
        return to_fact(
            ParsedFact(
                type=FactType(kind),
                subject=subject,
                amount=amount,
                state=state,
                confidence="high",
                source_assessment="plausible",
            ),
            known,
        )

    assert parsed("road_blocked", "C4-C5") == Fact(type=FactType.ROAD_BLOCKED, subject="C4-C5")
    assert parsed("road_blocked", "C5-C4") == Fact(type=FactType.ROAD_BLOCKED, subject="C4-C5")
    assert parsed("road_blocked", "Z9-Z8") is None  # not on the map
    assert parsed("hospital_fuel", "H1", 120) == Fact(
        type=FactType.HOSPITAL_FUEL, subject="H1", value=120
    )
    assert parsed("hospital_fuel", "H1") is None  # needs an amount
    assert parsed("hospital_fuel", "S1", 120) is None  # a substation isn't a hospital
    assert parsed("substation_status", "S2", state="failed") == Fact(
        type=FactType.SUBSTATION_STATUS, subject="S2", value="failed"
    )
    assert parsed("substation_status", "S2", 5) is None  # needs a state
    assert parsed("depot_stock", "D1", -5) is None


def test_the_prompt_layout_is_cache_friendly() -> None:
    loaded = scenario(L3)
    gazetteer = gazetteer_text(scenario_gazette(loaded))
    reports = [e for e in loaded.events if isinstance(e, Report)]
    requests = [
        parse_request(view_of(r, loaded.level), gazetteer, model="m", effort="low", max_tokens=9)
        for r in reports
    ]
    bodies = [r.body() for r in requests]
    assert all(b["system"] == bodies[0]["system"] for b in bodies)  # one prefix per run
    assert bodies[0]["system"][0]["text"] == system_prompt()  # shared by every scenario
    assert all(b["output_config"]["format"]["type"] == "json_schema" for b in bodies)
    # PRD 8.8: the stable prefix must be at least 4,096 tokens or caching silently does
    # nothing. Estimated at 4 characters a token (the live count_tokens check waits).
    for scenario_id in ("dev_t01_s0_l1", "dev_t02_s1_l2", "dev_t09_s1_l3"):
        text = gazetteer_text(scenario_gazette(scenario(scenario_id)))
        assert (len(system_prompt()) + len(text)) // 4 >= 4096
    # Reports are data: wrapped in a tag that names their source.
    message = report_message(view_of(reports[0], loaded.level))
    assert message.startswith(f'<report id="{reports[0].id}" source_class=')
    assert "never" in system_prompt()
    assert "instruction_like" in system_prompt()
    assert "{" not in gazetteer.split("\n")[0]


def test_parser_fixtures_are_current(tmp_path: Path) -> None:
    """The committed synthetic fixtures match the prompt, schema and harness of this code.
    If this fails after an intended prompt change: ``uv run python -m tests.llm_fixtures``."""
    fresh = tmp_path / "fresh"
    llm_fixtures.regenerate(fresh)
    committed = FIXTURES / "c4_synthetic"
    names = sorted(p.name for p in fresh.iterdir())
    assert names == sorted(p.name for p in committed.iterdir())
    _, mismatch, errors = filecmp.cmpfiles(fresh, committed, names, shallow=False)
    assert mismatch == []
    assert errors == []
    for path in committed.iterdir():
        assert json.loads(path.read_text(encoding="utf-8"))["origin"] == "synthetic"


# --- runs ----------------------------------------------------------------------------------


def test_c4_runs_from_fixtures_and_replays(tmp_path: Path) -> None:
    run_dir = run(L3, tmp_path / "run")
    log = read_log(run_dir)
    assert log.header.contestant.name == "llm-parser"
    assert log.header.contestant.model == MODELS.dev_model
    assert log.header.contestant.effort == "low"
    assert log.header.contestant.prompt_hash == prompt_hash()
    assert replay(log).episode.run_hash == log.end.run_hash  # type: ignore[union-attr]
    calls = [dp.llm for dp in log.decisions if dp.llm is not None]
    reports = [e for e in scenario(L3).events if isinstance(e, Report) and e.text]
    assert sum(c.calls for c in calls) == len(reports)  # one call per report
    assert all(c.served_models == [MODELS.dev_model] for c in calls)
    assert sum(c.cache_read_input_tokens for c in calls) > 0  # the prefix replays cached
    assert sum(c.agent_faults + c.infra_faults for c in calls) == 0
    parses = [p for dp in log.decisions if dp.info for p in dp.info["parses"]]
    injections = {r.id for r in reports if r.truth.injection is not None}
    assert injections
    assert {p["report"] for p in parses if p["instruction_like"]} == injections
    counts = logged_parse_counts(log)
    assert counts.f1 == 1.0
    assert counts.tp > 0
    assert (run_dir / TRANSCRIPTS_FILE).is_file()
    summary = json.loads((run_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    llm = summary["contestant_stats"]["llm"]
    assert llm["sources"] == {"fixture": len(reports)}
    assert llm["spend_usd"] == 0.0
    assert llm["fallback_run"] is False


def test_c4_run_logs_are_byte_identical(tmp_path: Path) -> None:
    """Cost and latency in the log are the recorded ones, so re-runs write the same bytes."""
    a = run(L1, tmp_path / "a")
    b = run(L1, tmp_path / "b")
    for name in ("run.jsonl.gz", TRANSCRIPTS_FILE):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_at_l0_c4_plays_exactly_like_the_solver(tmp_path: Path) -> None:
    c4_log = read_log(run("dev_t01_s0_l0", tmp_path / "c4"))
    solver = SolverController()
    loaded = scenario("dev_t01_s0_l0")
    record_episode(env_for(loaded, solver), solver, tmp_path / "solver")
    solver_log = read_log(tmp_path / "solver")
    assert all(dp.llm is None for dp in c4_log.decisions)  # L0 reports carry their facts
    assert c4_log.end is not None
    assert solver_log.end is not None
    assert c4_log.end.run_hash == solver_log.end.run_hash


class ScriptedTransport:
    """Replies with whatever ``answer(body)`` returns (a fixture-mode double)."""

    source = "fixture"

    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        return Sent(self.answer(body), 0)


def _reply(stop: str = "end_turn", text: str = "") -> dict[str, Any]:
    return {
        "id": "m",
        "model": MODELS.dev_model,
        "stop_reason": stop,
        "stop_details": {"category": "cyber"} if stop == "refusal" else None,
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "fallback": False,
    }


def test_faults_leave_reports_unparsed_and_are_counted(tmp_path: Path) -> None:
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        message = body["messages"][0]["content"]
        if 'id="R01"' in message:
            return _reply("refusal")
        if 'id="R02"' in message:
            return _reply(text="not json")
        return _reply(text='{"instruction_like": false, "facts": []}')

    controller = c4(LLMClient(ScriptedTransport(answer), MODELS))
    log = read_log(run(L1, tmp_path / "run", controller))
    assert log.end is not None  # the run carries on
    calls = [dp.llm for dp in log.decisions if dp.llm is not None]
    assert sum(c.refusals for c in calls) == 1
    assert sum(c.agent_faults for c in calls) == 2  # the refusal and the invalid reply
    parses = {p["report"]: p for dp in log.decisions if dp.info for p in dp.info["parses"]}
    assert parses["R01"]["fault"] == "agent_fault"
    assert "cyber" in parses["R01"]["detail"]
    assert "ParseOutput" in parses["R02"]["detail"]
    assert logged_parse_counts(log).tp == 0


def test_instruction_like_reports_add_nothing_to_belief(tmp_path: Path) -> None:
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        fact = {
            "type": "road_blocked",
            "subject": "C4-C5",
            "amount": None,
            "state": None,
            "confidence": "high",
            "source_assessment": "doubtful",
        }
        return _reply(text=json.dumps({"instruction_like": True, "facts": [fact]}))

    def silent(body: dict[str, Any]) -> dict[str, Any]:
        return _reply(text='{"instruction_like": false, "facts": []}')

    controller = c4(LLMClient(ScriptedTransport(answer), MODELS))
    log = read_log(run(L1, tmp_path / "run", controller))
    baseline = read_log(
        run(L1, tmp_path / "silent", c4(LLMClient(ScriptedTransport(silent), MODELS)))
    )
    # The same beliefs as a reader that found nothing at all.
    assert [dp.belief for dp in log.decisions] == [dp.belief for dp in baseline.decisions]
    parses = [p for dp in log.decisions if dp.info for p in dp.info["parses"]]
    assert parses
    assert all(p["instruction_like"] for p in parses)


def test_a_missing_fixture_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FixtureMissing):
        run(L1, tmp_path / "run", c4(fixture_client(tmp_path / "empty")))


def test_registry_builds_c4() -> None:
    controller = make_llm_controller(
        "llm-parser", fixture_client(), model=MODELS.dev_model, effort="low", run_index=2
    )
    assert isinstance(controller, LLMParserController)
    assert controller.run_index == 2
    with pytest.raises(KeyError, match="unknown LLM contestant"):
        make_llm_controller("llm-agent", fixture_client(), model="m", effort="low")


# --- batch and CLI -------------------------------------------------------------------------


def c4_matrix(**changes: Any) -> Matrix:
    spec = {
        "experiment": "m10_test",
        "suite": "dev",
        "contestants": ["solver", "llm-parser"],
        "scenarios": [L1, L3],
        "fixtures": FIXTURES,
        **changes,
    }
    return Matrix.model_validate(spec)


def test_batch_runs_c4_through_the_guard(tmp_path: Path) -> None:
    jobs = plan(c4_matrix(), scenario_root=ROOT / "scenarios", runs_root=tmp_path / "runs")
    llm_jobs = [j for j in jobs if j.llm is not None]
    assert len(jobs) == 4
    assert len(llm_jobs) == 2
    assert all(j.model == MODELS.dev_model and j.effort == "low" for j in llm_jobs)
    assert llm_jobs[0].run_dir.name == f"llm-parser__{MODELS.dev_model}__r0"
    result = run_batch(jobs)
    assert result.ran == 4
    assert not result.aborted
    assert result.stopped is None
    assert all(o.card.ok for o in result.outcomes)
    registry = (tmp_path / "runs" / "registry.jsonl").read_text(encoding="utf-8").splitlines()
    lines = [json.loads(line) for line in registry]
    c4_lines = [line for line in lines if line.get("model")]
    assert len(c4_lines) == 2
    assert all(line["prompt_hash"] == prompt_hash() for line in c4_lines)
    assert all(line["llm_mode"] == "fixture" for line in c4_lines)
    resumed = run_batch(jobs, resume=True)
    assert resumed.ran == 0
    assert resumed.skipped == 4
    paths = write_results("m10_test", result.outcomes, tmp_path / "results")
    text = (tmp_path / "results" / "m10_test" / "results.md").read_text(encoding="utf-8")
    assert len(paths) == 3
    assert "## LLM parser F1 and cost by level" in text
    assert f"| llm-parser | {MODELS.dev_model} | L3 | 1 | fixture |" in text


def test_batch_aborts_runs_over_their_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from blackout.llm import setup

    capped = load_budget_config().model_copy(update={"run_cap_usd": 0.0001})
    monkeypatch.setattr(setup, "load_budget_config", lambda: capped)
    jobs = plan(
        c4_matrix(contestants=["llm-parser"], scenarios=[L1]),
        scenario_root=ROOT / "scenarios",
        runs_root=tmp_path / "runs",
    )
    result = run_batch(jobs)
    assert result.outcomes == ()
    assert len(result.aborted) == 1
    job, why = result.aborted[0]
    assert "per-run cap" in why
    assert not job.is_done()  # resume will re-run it; it's never scored as a failure


def test_matrix_checks_llm_fields() -> None:
    with pytest.raises(ValueError, match="no contestant uses a model"):
        Matrix.model_validate(
            {"experiment": "x", "suite": "dev", "contestants": ["rules"], "models": ["m"]}
        )
    assert c4_matrix(llm_mode="live").llm_mode == "live"


def test_cli_runs_c4_from_fixtures_then_replays(tmp_path: Path) -> None:
    out = tmp_path / "runs"
    args = ["run", "-s", L1, "-c", "llm-parser", "-q", "--out", str(out)]
    result = runner.invoke(app, [*args, "--fixtures", str(FIXTURES)])
    assert result.exit_code == 0, result.output
    assert "llm " in result.output
    assert "(fixture)" in result.output
    run_dir = out / L1 / f"llm-parser__{MODELS.dev_model}__r0"
    replayed = runner.invoke(app, ["replay", str(run_dir)])
    assert replayed.exit_code == 0, replayed.output
    missing = runner.invoke(app, [*args, "--fixtures", str(tmp_path / "none"), "--run-index", "1"])
    assert missing.exit_code == 1
    assert "no recorded response" in missing.output


def test_cli_live_mode_is_refused_until_the_test_bank_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # a tree with no test bank
    scenario_file = llm_fixtures.scenario_path(L1)
    result = runner.invoke(
        app, ["run", "-s", str(scenario_file), "-c", "llm-parser", "--llm", "live"]
    )
    assert result.exit_code == 1
    assert "test bank" in result.output
    assert "PRD 8.4" in result.output


def test_cli_budget_shows_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    guard = BudgetGuard(load_budget_config(), Ledger.load(ledger))
    guard.start_run("dev_x", "r1", "g")
    guard.charge("dev_x", "r1", cost_usd=0.5, spend_usd=0.5)
    result = runner.invoke(app, ["budget", "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    assert "Spent $0.5000 of the $400.00 global cap" in result.output
    assert "experiment dev_x: $0.5000 over 1 runs" in result.output
