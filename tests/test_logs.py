"""Run logs: what gets written, that it's deterministic, and that bad logs are rejected clearly."""

import json
from pathlib import Path
from typing import Any

import pytest

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import Script, ScriptedController
from blackout.logs.reader import read_log
from blackout.logs.replay import replay
from blackout.logs.schema import LOG_FILE, SUMMARY_FILE, LogFormatError, UnsupportedLogVersionError
from blackout.scenarios.schema import load_scenario, scenario_digest
from blackout.scoring.scorer import score
from tests.helpers import (
    CASCADE,
    DEMO,
    handmade_controller,
    make_scenario,
    read_lines,
    record,
    report,
    write_lines,
)


@pytest.fixture
def demo_log(tmp_path: Path) -> Path:
    return record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path


def test_a_run_leaves_a_small_complete_log(demo_log: Path) -> None:
    log = read_log(demo_log)
    assert demo_log.name == LOG_FILE
    assert demo_log.stat().st_size < 64 * 1024  # far below the 1 MB budget
    assert log.header.run_id == "hello_blackout/scripted__none__r0"
    assert [tick.t for tick in log.ticks] == list(range(130))
    assert len(log.decisions) == 14
    assert log.end is not None
    assert (log.end.t, log.end.reason, log.end.decision_points) == (129, "stable", 14)


def test_the_header_embeds_the_scenario(demo_log: Path) -> None:
    header = read_log(demo_log).header
    scenario = load_scenario(DEMO)
    assert header.scenario.hash == scenario_digest(scenario)
    assert read_log(demo_log).scenario() == scenario
    assert header.contestant.name == "scripted"
    assert header.engine.config["trip_grace_minutes"] == 3


def test_events_and_decisions_are_recorded_where_they_happened(tmp_path: Path) -> None:
    demo = read_log(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)
    assert demo.ticks[0].ev == [{"edge": "M3-M4", "kind": "road_block", "tick": 0}]
    stuck = next(d for d in demo.decisions if d.t == 18)
    assert [t.kind for t in stuck.triggers] == ["road_status", "vehicle_stuck"]
    (reroute,) = stuck.actions
    assert reroute.raw["kind"] == "reroute"
    assert (reroute.accepted, reroute.route) == (True, ["M3", "E3", "E4", "M4", "M5"])
    assert stuck.llm is None  # reserved until M10

    cascade = read_log(record(CASCADE, DoNothingController(), tmp_path).log_path)
    assert cascade.ticks[10].ev[0]["kind"] == "substation_fail"


def test_identical_runs_write_identical_logs(tmp_path: Path) -> None:
    first = record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path / "a").log_path
    second = record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path / "b").log_path
    assert first.read_bytes() == second.read_bytes()


def test_the_summary_holds_what_the_log_leaves_out(tmp_path: Path) -> None:
    recorded = record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path)
    summary = json.loads((recorded.run_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["run_id"] == "hello_blackout/scripted__none__r0"
    assert summary["metrics"]["critical_downtime_min"] == 0
    assert summary["log"]["records"] == 1 + 130 + 14 + 1
    assert {"recorded_at", "machine", "code"} <= set(summary)
    text = "".join(json.dumps(line) for line in read_lines(recorded.log_path))
    assert "recorded_at" not in text  # nothing machine- or time-specific in the log itself


def broken(records: list[dict[str, Any]], case: str) -> list[dict[str, Any]]:
    """A copy of a log's records with one thing wrong with it."""
    copy = [dict(r) for r in records]
    if case == "future version":
        copy[1]["v"] = 99
    elif case == "missing version":
        del copy[1]["v"]
    elif case == "no header":
        copy = copy[1:]
    elif case == "two headers":
        copy.insert(1, copy[0])
    elif case == "tick gap":
        del copy[3]  # header, tick 0, decision point 0, then tick 1
    elif case == "bad record":
        del copy[1]["h"]
    elif case == "after the end":
        copy.append(copy[1])
    elif case == "end mismatch":
        copy[-1]["t"] = 7
    return copy


@pytest.mark.parametrize(
    ("case", "error", "message"),
    [
        ("future version", UnsupportedLogVersionError, "version 99 isn't supported"),
        ("missing version", UnsupportedLogVersionError, "version None isn't supported"),
        ("no header", LogFormatError, "the first record must be the header"),
        ("two headers", LogFormatError, "a second header"),
        ("tick gap", LogFormatError, "expected tick 1, found 2"),
        ("bad record", LogFormatError, "invalid 'tick' record at h: Field required"),
        ("after the end", LogFormatError, "a record comes after the end record"),
        ("end mismatch", LogFormatError, "the end record doesn't match the last tick"),
    ],
)
def test_bad_logs_are_rejected_clearly(
    demo_log: Path, tmp_path: Path, case: str, error: type[Exception], message: str
) -> None:
    path = write_lines(broken(read_lines(demo_log), case), tmp_path / "broken.jsonl")
    with pytest.raises(error, match=message):
        read_log(path)


def test_unreadable_files_are_rejected_clearly(tmp_path: Path) -> None:
    with pytest.raises(LogFormatError, match="no run log at"):
        read_log(tmp_path / "missing")
    (tmp_path / "empty.jsonl").write_text("", encoding="ascii")
    with pytest.raises(LogFormatError, match="the run log is empty"):
        read_log(tmp_path / "empty.jsonl")
    (tmp_path / "garbage.jsonl").write_text("{nope\n", encoding="ascii")
    with pytest.raises(LogFormatError, match=r"garbage\.jsonl:1: not valid JSON"):
        read_log(tmp_path / "garbage.jsonl")
    (tmp_path / "list.jsonl").write_text("[1, 2]\n", encoding="ascii")
    with pytest.raises(LogFormatError, match="a record must be a JSON object"):
        read_log(tmp_path / "list.jsonl")


def test_decision_records_carry_the_contestants_belief(tmp_path: Path) -> None:
    demo = read_log(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)
    before = next(d for d in demo.decisions if d.t == 5)
    stuck = next(d for d in demo.decisions if d.t == 18)
    assert before.belief is not None
    assert stuck.belief is not None
    assert before.belief["roads"] == {}
    assert stuck.belief["roads"]["M3-M4"]["value"] == "blocked"
    assert stuck.belief["hospitals"]["H1"]["fuel_l"]["value"] == 80 - 2 * 18


def test_reports_and_confirmations_are_logged_replayed_and_scored(tmp_path: Path) -> None:
    scenario = make_scenario(events=[report("R1", 2), report("R2", 3, source_class="unverified")])
    script = Script.model_validate(
        {
            "steps": [
                {"at_tick": 2, "action": {"kind": "confirm_report", "report_id": "R1"}},
                {"at_tick": 2, "action": {"kind": "confirm_report", "report_id": "R2"}},
            ]
        }
    )
    recorded = record(scenario, ScriptedController(script), tmp_path)
    log = read_log(recorded.log_path)
    assert [ev["kind"] for t in log.ticks for ev in t.ev] == ["report", "report", "confirm_reply"]
    first = log.decisions[1]
    assert [(a.accepted, a.reason) for a in first.actions] == [
        (True, None),
        (False, "unknown_report"),
    ]
    assert replay(log).episode.run_hash == recorded.episode.run_hash
    card = score(log)
    assert card.ok
    assert card.actions_by_kind == {"confirm_report": 2}
    assert card.actions_rejected == {"unknown_report": 1}


def test_a_log_of_an_older_scenario_schema_is_refused_clearly(tmp_path: Path) -> None:
    records = read_lines(record(DEMO, DoNothingController(), tmp_path).log_path)
    records[0]["scenario"]["spec"]["schema_version"] = 2
    log = read_log(write_lines(records, tmp_path / "old.jsonl"))
    with pytest.raises(LogFormatError, match="schema version 2"):
        log.scenario()
