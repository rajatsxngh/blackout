"""The batch runner: every run of a matrix, in parallel processes, resumable (PRD 8.14).

Each run is logged through ``record_episode`` under
``runs/<experiment>/<scenario>/<contestant>__<model>__r<k>/`` and then scored from its log with
the M3 scorer (replay first, so a bad log can't produce a score).

**Resume.** Every run has a *config hash*: the scenario's digest, the contestant, the run
index, the engine rules (SimConfig) and a digest of the code and phrasing bank that decide
what happens in a run. It's written to the run's summary.json. With ``resume``, a run whose
summary.json carries the same hash (and whose log is there) is skipped; only its score is
recomputed from the log.

Non-LLM runs are deterministic, so they go to a process pool (``spawn`` on every OS, so
Linux behaves like Windows). Entry points that start a batch need the Windows spawn guard
(``if __name__ == "__main__":``); the ``blackout`` console script has one.

**LLM runs** (M10) run one at a time in this process, each through the budget guard: it's
started in the ledger, every call is charged to it, and it's finished when the run ends. A
run the guard aborts (over its cap, or 5x its group's median) raises ``RunAborted``; it gets no
summary.json, so ``--resume`` re-runs it, and it's listed as aborted, never scored. A cap
breach (``BudgetExceeded``) stops the batch scheduling new runs. A run with more than 10% infra
faults gets ``rerun_needed`` in its summary, so resume re-runs it (the cache keeps the calls
that worked). Their config hash adds the model, effort, prompt hash, harness version and
reply mode (fixture or live).
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
from blackout.controllers.registry import LLM_CONTESTANTS, make_controller, make_llm_controller
from blackout.env import env_for
from blackout.experiments.matrix import Matrix
from blackout.llm.budget import BudgetExceeded, RunAborted
from blackout.llm.client import HARNESS_VERSION
from blackout.llm.config import Effort, load_models_config
from blackout.llm.setup import LLMSettings, make_client
from blackout.logs.schema import LOG_FILE, SUMMARY_FILE
from blackout.logs.writer import record_episode, run_dir_for, run_id_for
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


def llm_prompt_hash() -> str:
    from blackout.controllers.llm_parser import prompt_hash

    return prompt_hash()


def run_config(
    scenario: Scenario,
    contestant: str,
    run_index: int,
    *,
    model: str | None = None,
    effort: str | None = None,
    llm: LLMSettings | None = None,
) -> dict[str, object]:
    """Everything that decides a run's outcome. Its digest is the run's config hash."""
    config: dict[str, object] = {
        "scenario": {"id": scenario.id, "hash": scenario_digest(scenario)},
        "contestant": {"name": contestant, "model": model, "run_index": run_index},
        "engine": SimConfig().to_dict(),
        "code": {"version": __version__, "digest": code_digest()},
    }
    if llm is not None:
        config["llm"] = {
            "effort": effort,
            "prompt_hash": llm_prompt_hash(),
            "harness": HARNESS_VERSION,
            "mode": llm.mode,
        }
    return config


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
    model: str | None = None
    effort: Effort | None = None
    llm: LLMSettings | None = None

    @property
    def run_id(self) -> str:
        return run_id_for(self.scenario_id, self.contestant, self.model, self.run_index)

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
        return summary.get("config_hash") == self.config_hash and not summary.get("rerun_needed")


class HeldOut(ValueError):  # noqa: N818 (a refusal)
    """The test guard refused the suite (blackout.experiments.guard)."""


def plan(
    matrix: Matrix, *, scenario_root: Path, runs_root: Path = RUNS_ROOT, root: Path = Path()
) -> list[Job]:
    """Every run of ``matrix``, in a fixed order: scenario, contestant, model, run index. A
    held-out suite is refused (``HeldOut``) unless the test guard lets it through."""
    from blackout.experiments.guard import guard_problems  # the guard hashes this module

    problems = guard_problems(matrix.suite, root)
    if problems:
        raise HeldOut("; ".join(problems))
    jobs: list[Job] = []
    out = runs_root / matrix.experiment
    models = load_models_config()
    llm = LLMSettings(mode=matrix.llm_mode, fixtures=matrix.fixtures or LLMSettings().fixtures)
    for path, scenario in matrix.scenarios(scenario_root):
        for contestant in matrix.contestants:
            purpose = LLM_CONTESTANTS.get(contestant)
            choices: list[tuple[str | None, Effort | None]] = [(None, None)]
            settings = None
            if purpose is not None:
                effort = matrix.effort or models.effort[purpose]
                choices = [(m, effort) for m in matrix.models or (models.dev_model,)]
                settings = llm
            for model, effort in choices:
                for run_index in range(matrix.runs):
                    config = run_config(
                        scenario, contestant, run_index, model=model, effort=effort, llm=settings
                    )
                    run_dir = run_dir_for(
                        out, scenario.id, contestant, model=model, run_index=run_index
                    )
                    jobs.append(
                        Job(
                            experiment=matrix.experiment,
                            scenario_path=path,
                            scenario_id=scenario.id,
                            template=scenario.template,
                            level=scenario.level,
                            contestant=contestant,
                            run_index=run_index,
                            run_dir=run_dir,
                            config_hash=digest(config),
                            model=model,
                            effort=effort,
                            llm=settings,
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
        if job.llm is not None:
            _run_llm(job)
        else:
            scenario = load_scenario(job.scenario_path)
            controller = make_controller(job.contestant)
            extra: dict[str, object] = {
                "config_hash": job.config_hash,
                "experiment": job.experiment,
            }
            record_episode(
                env_for(scenario, controller),
                controller,
                job.run_dir,
                run_index=job.run_index,
                summary_extra=extra,
            )
    return Outcome(job, run, score(job.run_dir / LOG_FILE))


def _run_llm(job: Job) -> None:
    """One LLM run through the budget guard (``RunAborted``/``BudgetExceeded`` propagate)."""
    assert job.llm is not None
    assert job.model is not None
    assert job.effort is not None
    scenario = load_scenario(job.scenario_path)
    client = make_client(job.llm)
    guard = client.guard
    assert guard is not None
    controller = make_llm_controller(
        job.contestant, client, model=job.model, effort=job.effort, run_index=job.run_index
    )
    job.summary_path.unlink(missing_ok=True)  # an aborted run must not look done
    guard.start_run(job.experiment, job.run_id, f"{job.contestant}/{job.model}")
    client.context = (job.experiment, job.run_id)
    extra: dict[str, object] = {"config_hash": job.config_hash, "experiment": job.experiment}
    recorded = record_episode(
        env_for(scenario, controller),
        controller,
        job.run_dir,
        run_index=job.run_index,
        summary_extra=extra,
    )
    guard.finish_run(job.experiment, job.run_id)
    stats = recorded.summary.get("contestant_stats")
    llm = stats.get("llm", {}) if isinstance(stats, dict) else {}
    if float(llm.get("infra_fault_pct", 0.0)) > guard.config.infra_fault_rerun_pct:
        data = {**recorded.summary, "rerun_needed": True}
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        job.summary_path.write_text(text, encoding="utf-8", newline="\n")


@dataclass(frozen=True, slots=True)
class BatchResult:
    outcomes: tuple[Outcome, ...]
    aborted: tuple[tuple[Job, str], ...] = ()  # LLM runs the guard stopped: re-run, not scored
    stopped: str | None = None  # why the batch stopped scheduling runs (a cap breach)

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
    that many processes for non-LLM runs. Outcomes come back in job order."""
    todo = [(job, not (resume and job.is_done())) for job in jobs]
    plain = [(job, run) for job, run in todo if job.llm is None]
    llm = [(job, run) for job, run in todo if job.llm is not None]
    done: dict[Path, Outcome] = {}
    for outcome in _map(plain, workers):
        done[outcome.job.run_dir] = outcome
        if on_done is not None:
            on_done(outcome)
    aborted: list[tuple[Job, str]] = []
    stopped: str | None = None
    for job, run in llm:  # one at a time, in this process: they share the ledger
        if stopped is not None and run:
            aborted.append((job, f"not started: {stopped}"))
            continue
        try:
            outcome = execute(job, run)
        except RunAborted as error:
            aborted.append((job, str(error)))
            continue
        except BudgetExceeded as error:
            stopped = str(error)
            aborted.append((job, stopped))
            continue
        done[outcome.job.run_dir] = outcome
        if on_done is not None:
            on_done(outcome)
    outcomes = [done[job.run_dir] for job, _ in todo if job.run_dir in done]
    if any(o.ran for o in outcomes):
        _register(outcomes)
    return BatchResult(tuple(outcomes), tuple(aborted), stopped)


def _map(todo: list[tuple[Job, bool]], workers: int) -> Iterator[Outcome]:
    if workers <= 1 or len(todo) <= 1:
        for job, run in todo:
            yield execute(job, run)
        return
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        yield from pool.map(execute, *zip(*todo, strict=True))


def _register(outcomes: list[Outcome]) -> None:
    """Append newly run jobs to ``runs/registry.jsonl``, the index of every run (PRD 8.12):
    its config hash and, for LLM runs, model, effort and prompt hash."""
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
            "prompt_hash": None if job.llm is None else llm_prompt_hash(),
            "run_hash": outcome.card.run_hash,
        }
        if job.llm is not None:
            line |= {"model": job.model, "effort": job.effort, "llm_mode": job.llm.mode}
        with registry.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
