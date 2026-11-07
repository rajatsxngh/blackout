"""The batch runner: every run of a matrix, in parallel processes, resumable (PRD 8.14).

Each run is logged through ``record_episode`` under
``runs/<experiment>/<scenario>/<contestant>__none__r<k>/`` and then scored from its log with
the M3 scorer (replay first, so a bad log can't produce a score).

**Resume.** Every run has a *config hash*: the scenario's digest, the contestant, the run
index, the engine rules (SimConfig) and a digest of the code and phrasing bank that decide
what happens in a run. It's written to the run's summary.json. With ``resume``, a run whose
summary.json carries the same hash (and whose log is there) is skipped; only its score is
recomputed from the log.

Non-LLM runs are deterministic, so they go to a process pool (``spawn`` on every OS, so
Linux behaves like Windows). Entry points that start a batch need the Windows spawn guard
(``if __name__ == "__main__":``); the ``blackout`` console script has one.
"""

import json
import multiprocessing
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from blackout import __version__
from blackout.controllers.keyword_parser import TRAIN_BANK_DIR
from blackout.controllers.registry import make_controller
from blackout.env import BlackoutEnv
from blackout.experiments.matrix import Matrix
from blackout.logs.schema import LOG_FILE, SUMMARY_FILE
from blackout.logs.writer import record_episode, run_dir_for
from blackout.scenarios.schema import Scenario, load_scenario, scenario_digest
from blackout.scoring.scorer import ScoreCard, score
from blackout.sim.config import SimConfig
from blackout.sim.hashing import digest

RUNS_ROOT = Path("runs")
PACKAGE = Path(__file__).resolve().parents[1]
# Code that can't change what happens inside a run, so editing it doesn't invalidate runs.
_NOT_RUN_CODE = ("cli.py", "experiments", "scoring")


@cache
def code_digest() -> str:
    """Digest of every source file that can affect a run, plus the train phrasing bank."""
    files = sorted(
        path
        for path in PACKAGE.rglob("*.py")
        if path.relative_to(PACKAGE).parts[0] not in _NOT_RUN_CODE
    )
    files += sorted(TRAIN_BANK_DIR.glob("*.yaml"))
    parts = {
        path.relative_to(PACKAGE.parents[1]).as_posix(): path.read_bytes().decode("utf-8")
        for path in files
    }
    return digest(parts)


def run_config(scenario: Scenario, contestant: str, run_index: int) -> dict[str, object]:
    """Everything that decides a run's outcome. Its digest is the run's config hash."""
    return {
        "scenario": {"id": scenario.id, "hash": scenario_digest(scenario)},
        "contestant": {"name": contestant, "model": None, "run_index": run_index},
        "engine": SimConfig().to_dict(),
        "code": {"version": __version__, "digest": code_digest()},
    }


@dataclass(frozen=True, slots=True)
class Job:
    """One run of a matrix. Plain data, so it can be sent to a worker process."""

    experiment: str
    scenario_path: Path
    scenario_id: str
    template: str | None
    level: int
    contestant: str
    run_index: int
    run_dir: Path
    config_hash: str

    @property
    def summary_path(self) -> Path:
        return self.run_dir / SUMMARY_FILE

    def is_done(self) -> bool:
        """The run is logged, with the config this job would run it with."""
        if not self.summary_path.is_file() or not (self.run_dir / LOG_FILE).is_file():
            return False
        try:
            summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return summary.get("config_hash") == self.config_hash


def plan(matrix: Matrix, *, scenario_root: Path, runs_root: Path = RUNS_ROOT) -> list[Job]:
    """Every run of ``matrix``, in a fixed order: scenario, contestant, run index."""
    jobs: list[Job] = []
    out = runs_root / matrix.experiment
    for path, scenario in matrix.scenarios(scenario_root):
        for contestant in matrix.contestants:
            for run_index in range(matrix.runs):
                config = run_config(scenario, contestant, run_index)
                jobs.append(
                    Job(
                        experiment=matrix.experiment,
                        scenario_path=path,
                        scenario_id=scenario.id,
                        template=scenario.template,
                        level=scenario.level,
                        contestant=contestant,
                        run_index=run_index,
                        run_dir=run_dir_for(out, scenario.id, contestant, run_index=run_index),
                        config_hash=digest(config),
                    )
                )
    return jobs


@dataclass(frozen=True, slots=True)
class Outcome:
    job: Job
    ran: bool  # False if resume skipped it
    card: ScoreCard


def execute(job: Job, run: bool) -> Outcome:
    """Run ``job`` (unless ``run`` is False, meaning it's already logged), then score it.

    A top-level function, so worker processes can import it.
    """
    if run:
        scenario = load_scenario(job.scenario_path)
        extra = {"config_hash": job.config_hash, "experiment": job.experiment}
        record_episode(
            BlackoutEnv(scenario),
            make_controller(job.contestant),
            job.run_dir,
            run_index=job.run_index,
            summary_extra=extra,
        )
    return Outcome(job, run, score(job.run_dir / LOG_FILE))


@dataclass(frozen=True, slots=True)
class BatchResult:
    outcomes: tuple[Outcome, ...]

    @property
    def ran(self) -> int:
        return sum(1 for o in self.outcomes if o.ran)

    @property
    def skipped(self) -> int:
        return len(self.outcomes) - self.ran


def run_batch(
    jobs: list[Job],
    *,
    resume: bool = False,
    workers: int = 1,
    on_done: Callable[[Outcome], None] | None = None,
) -> BatchResult:
    """Run (or, with ``resume``, skip completed) jobs and score them all. ``workers`` > 1 uses
    that many processes. Outcomes come back in job order whatever order they finish in."""
    todo = [(job, not (resume and job.is_done())) for job in jobs]
    outcomes: list[Outcome] = []
    for outcome in _map(todo, workers):
        outcomes.append(outcome)
        if on_done is not None:
            on_done(outcome)
    if any(o.ran for o in outcomes):
        _register(outcomes)
    return BatchResult(tuple(outcomes))


def _map(todo: list[tuple[Job, bool]], workers: int) -> Iterator[Outcome]:
    if workers <= 1 or len(todo) <= 1:
        for job, run in todo:
            yield execute(job, run)
        return
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        yield from pool.map(execute, *zip(*todo, strict=True))


def _register(outcomes: list[Outcome]) -> None:
    """Append newly run jobs to ``runs/registry.jsonl``, the index of every run (PRD 8.12)."""
    for outcome in outcomes:
        if not outcome.ran:
            continue
        job = outcome.job
        registry = job.run_dir.parents[2] / "registry.jsonl"
        line = {
            "run_id": outcome.card.run_id,
            "experiment": job.experiment,
            "run_dir": job.run_dir.as_posix(),
            "config_hash": job.config_hash,
            "prompt_hash": None,
            "run_hash": outcome.card.run_hash,
        }
        with registry.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
