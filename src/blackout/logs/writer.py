"""Writing a run log while the run happens, then its summary.json (docs/PRD.md section 8.12)."""

import gzip
import io
import json
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel

from blackout import __version__
from blackout.controllers.base import Controller
from blackout.env import ROLE_ALL, BlackoutEnv, DecisionRequest, EpisodeResult, run_episode
from blackout.logs.schema import (
    LOG_FILE,
    SUMMARY_FILE,
    ActionRecord,
    ContestantRef,
    DecisionPointRecord,
    EndRecord,
    EngineRef,
    HeaderRecord,
    ScenarioRef,
    TickRecord,
    TriggerRecord,
)
from blackout.scenarios.schema import scenario_digest
from blackout.sim.actions import ActionResult
from blackout.sim.engine import Engine
from blackout.sim.events import event_dict
from blackout.sim.hashing import digest


def log_line(record: BaseModel) -> str:
    """One record as a line of canonical JSON: sorted keys, no spaces, ASCII only."""
    data = record.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


class RunLogWriter:
    """Writes records to a gzip file whose bytes depend only on the records written."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.records = 0
        self._file = path.open("wb")
        # mtime=0 and an empty file name keep the gzip header identical from run to run.
        self._gzip = gzip.GzipFile(filename="", mode="wb", fileobj=self._file, mtime=0)
        self._text = io.TextIOWrapper(self._gzip, encoding="ascii", newline="\n")

    def write(self, record: BaseModel) -> None:
        self._text.write(log_line(record))
        self.records += 1

    def close(self) -> None:
        self._text.close()  # flushes and closes the gzip stream, which writes its trailer
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_id_for(scenario_id: str, contestant: str, model: str | None, run_index: int) -> str:
    return f"{scenario_id}/{contestant}__{model or 'none'}__r{run_index}"


def run_dir_for(
    out: Path, scenario_id: str, contestant: str, *, model: str | None = None, run_index: int = 0
) -> Path:
    """Where a run's files go: ``<out>/<scenario>/<contestant>__<model>__r<k>/``."""
    return out.joinpath(*run_id_for(scenario_id, contestant, model, run_index).split("/"))


def header_record(
    env: BlackoutEnv,
    contestant: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    run_index: int = 0,
) -> HeaderRecord:
    scenario = env.scenario
    config = env.config.to_dict()
    return HeaderRecord(
        t=0,
        run_id=run_id_for(scenario.id, contestant, model, run_index),
        scenario=ScenarioRef(
            id=scenario.id, hash=scenario_digest(scenario), spec=scenario.model_dump(mode="json")
        ),
        contestant=ContestantRef(name=contestant, model=model, effort=effort, run_index=run_index),
        engine=EngineRef(version=__version__, config=config, config_hash=digest(config)),
    )


def decision_record(
    request: DecisionRequest, results: tuple[ActionResult, ...]
) -> DecisionPointRecord:
    observation = request.observations[ROLE_ALL]
    return DecisionPointRecord(
        t=request.tick,
        i=request.index,
        role=ROLE_ALL,
        triggers=[
            TriggerRecord(kind=t.kind.value, subject=t.subject, detail=t.detail)
            for t in request.triggers
        ],
        obs=observation.digest(),
        actions=[
            ActionRecord(
                raw=result.action.model_dump(mode="json"),
                accepted=result.accepted,
                reason=None if result.reason is None else result.reason.value,
                message=result.message,
                route=list(result.route),
                eta_minutes=result.eta_minutes,
            )
            for result in results
        ],
    )


@dataclass(frozen=True, slots=True)
class RecordedRun:
    episode: EpisodeResult
    run_dir: Path
    log_path: Path
    summary: dict[str, object]


def record_episode(
    env: BlackoutEnv,
    controller: Controller,
    run_dir: Path,
    *,
    model: str | None = None,
    effort: str | None = None,
    run_index: int = 0,
    on_decision: Callable[[DecisionRequest, tuple[ActionResult, ...]], None] | None = None,
) -> RecordedRun:
    """Run ``controller`` on a fresh ``env``, logging everything to ``run_dir``."""
    log_path = run_dir / LOG_FILE
    header = header_record(env, controller.name, model=model, effort=effort, run_index=run_index)
    with RunLogWriter(log_path) as writer:
        writer.write(header)

        def on_tick(engine: Engine) -> None:
            events = [event_dict(item) for item in engine.applied]
            writer.write(TickRecord(t=engine.tick, h=env.tick_hashes[-1], ev=events))

        def decided(request: DecisionRequest, results: tuple[ActionResult, ...]) -> None:
            writer.write(decision_record(request, results))
            if on_decision is not None:
                on_decision(request, results)

        env.add_tick_listener(on_tick)
        try:
            episode = run_episode(env, controller, on_decision=decided)
        finally:
            env.remove_tick_listener(on_tick)
        writer.write(
            EndRecord(
                t=episode.done.tick,
                reason=episode.done.reason,
                decision_points=len(episode.decisions),
                run_hash=episode.run_hash,
                metrics=dict(episode.done.metrics),
            )
        )
        records = writer.records
    summary = _summary(header, episode, log_path, records)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    (run_dir / SUMMARY_FILE).write_text(text, encoding="utf-8", newline="\n")
    return RecordedRun(episode, run_dir, log_path, summary)


def _summary(
    header: HeaderRecord, episode: EpisodeResult, log_path: Path, records: int
) -> dict[str, object]:
    """Everything about the run worth a glance, plus the machine-specific facts the log omits."""
    done = episode.done
    return {
        "run_id": header.run_id,
        "scenario": {"id": header.scenario.id, "hash": header.scenario.hash},
        "contestant": header.contestant.model_dump(mode="json"),
        "end": {"tick": done.tick, "reason": done.reason},
        "decision_points": len(episode.decisions),
        "run_hash": episode.run_hash,
        "metrics": dict(done.metrics),
        "log": {"file": LOG_FILE, "records": records, "bytes": log_path.stat().st_size},
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": {"python": platform.python_version(), "platform": platform.platform()},
        "code": {"version": __version__, **_git_state()},
    }


@cache
def _git_state() -> dict[str, object]:
    """The commit the code came from, if it's running from a git checkout (best effort)."""

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                cwd=Path(__file__).resolve().parent,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain")
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
    }
