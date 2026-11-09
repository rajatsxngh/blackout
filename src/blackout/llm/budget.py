"""The spend ledger and the budget guard (docs/PRD.md 8.8, caps in ``configs/budget.yaml``).

The ledger (``runs/ledger.json``) records every dollar spent: in total, per bucket, per
experiment and per run, plus each Message Batch. Two amounts are kept per run:

- ``spend_usd``: new money (a cache hit or a recorded fixture costs $0). The global and bucket
  caps are on spend.
- ``cost_usd``: what the run's calls cost when they were made (a cache hit counts its
  original cost). The per-run cap and the 5x-median abort are on cost, so a loop bug shows up
  even when it's replayed from the cache.

A cap breach raises ``BudgetExceeded``, which stops new runs being scheduled. A run over its
own cap, or over 5x the median of completed runs of its group (same experiment, contestant and
model), raises ``RunAborted``: it's marked aborted and re-run later, never scored as a failure.
"""

import fnmatch
import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveInt

BUDGET_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "budget.yaml"
DEFAULT_LEDGER = Path("runs") / "ledger.json"
LEDGER_VERSION = 1


class BudgetError(Exception):
    """Base class for budget refusals."""


class BudgetExceeded(BudgetError):  # noqa: N818 (a refusal, as in the PRD)
    """A global or bucket cap is reached: schedule no new runs."""


class RunAborted(BudgetError):  # noqa: N818
    """This run is over its own cap or 5x the median: stop it and re-run it later."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bucket(_Config):
    name: str
    cap_usd: NonNegativeFloat
    experiments: tuple[str, ...] = Field(min_length=1)


class BudgetConfig(_Config):
    global_cap_usd: NonNegativeFloat
    run_cap_usd: NonNegativeFloat
    buckets: tuple[Bucket, ...] = Field(min_length=1)
    median_abort_factor: PositiveInt = 5
    median_min_runs: PositiveInt = 3
    infra_fault_rerun_pct: int = Field(default=10, ge=0, le=100)

    def bucket_for(self, experiment: str) -> Bucket:
        for bucket in self.buckets:
            if any(fnmatch.fnmatchcase(experiment, p) for p in bucket.experiments):
                return bucket
        names = ", ".join(b.name for b in self.buckets)
        raise BudgetError(
            f"experiment {experiment!r} is in no budget bucket ({names}): add it to"
            " configs/budget.yaml"
        )


def load_budget_config(path: Path = BUDGET_CONFIG) -> BudgetConfig:
    with path.open(encoding="utf-8") as handle:
        return BudgetConfig.model_validate(yaml.safe_load(handle))


def _usd(value: float) -> float:
    return round(value, 6)


@dataclass(slots=True)
class Ledger:
    """The spend record. ``path`` None keeps it in memory (tests, fixture-mode runs)."""

    path: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = DEFAULT_LEDGER) -> "Ledger":
        ledger = cls(path)
        if path is not None and path.is_file():
            ledger.data = json.loads(path.read_text(encoding="utf-8"))
            if ledger.data.get("version") != LEDGER_VERSION:
                raise BudgetError(f"{path}: ledger version {ledger.data.get('version')!r}")
        ledger.data.setdefault("version", LEDGER_VERSION)
        ledger.data.setdefault("spend_usd", 0.0)
        ledger.data.setdefault("buckets", {})
        ledger.data.setdefault("experiments", {})
        ledger.data.setdefault("batches", {})
        return ledger

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def spend_usd(self) -> float:
        return float(self.data["spend_usd"])

    def bucket_spend(self, bucket: str) -> float:
        return float(self.data["buckets"].get(bucket, 0.0))

    def experiment(self, experiment: str, bucket: str) -> dict[str, Any]:
        return self.data["experiments"].setdefault(
            experiment, {"bucket": bucket, "spend_usd": 0.0, "runs": {}}
        )

    def add_spend(self, experiment: str, bucket: str, spend: float) -> None:
        self.data["spend_usd"] = _usd(self.spend_usd + spend)
        self.data["buckets"][bucket] = _usd(self.bucket_spend(bucket) + spend)
        entry = self.experiment(experiment, bucket)
        entry["spend_usd"] = _usd(entry["spend_usd"] + spend)


class BudgetGuard:
    """Checks the caps before work starts and charges every call to the ledger."""

    def __init__(self, config: BudgetConfig, ledger: Ledger) -> None:
        self.config = config
        self.ledger = ledger

    # --- caps --------------------------------------------------------------------------------

    def problems(self, experiment: str, extra_usd: float = 0.0) -> list[str]:
        """Why no more money may be spent on ``experiment`` (empty if it may)."""
        bucket = self.config.bucket_for(experiment)
        out = []
        total = self.ledger.spend_usd + extra_usd
        if total >= self.config.global_cap_usd:
            out.append(f"global cap ${self.config.global_cap_usd:.2f} reached (${total:.4f})")
        spent = self.ledger.bucket_spend(bucket.name) + extra_usd
        if spent >= bucket.cap_usd:
            out.append(f"bucket {bucket.name!r} cap ${bucket.cap_usd:.2f} reached (${spent:.4f})")
        return out

    def _require(self, experiment: str, extra_usd: float = 0.0) -> None:
        problems = self.problems(experiment, extra_usd)
        if problems:
            raise BudgetExceeded("; ".join(problems))

    def reserve(self, experiment: str, estimate_usd: float) -> None:
        """Refuse work whose estimated cost would breach a cap (Message Batches)."""
        self._require(experiment, estimate_usd)

    # --- runs --------------------------------------------------------------------------------

    def _run(self, experiment: str, run_id: str) -> dict[str, Any]:
        bucket = self.config.bucket_for(experiment).name
        runs = self.ledger.experiment(experiment, bucket)["runs"]
        if run_id not in runs:
            raise BudgetError(f"run {run_id!r} wasn't started through the budget guard")
        return runs[run_id]

    def start_run(self, experiment: str, run_id: str, group: str) -> None:
        """Begin (or restart) a run; refused once a cap is reached."""
        self._require(experiment)
        bucket = self.config.bucket_for(experiment).name
        runs = self.ledger.experiment(experiment, bucket)["runs"]
        attempts = int(runs.get(run_id, {}).get("attempts", 0)) + 1
        runs[run_id] = {
            "group": group,
            "status": "running",
            "attempts": attempts,
            "calls": 0,
            "cost_usd": 0.0,
            "spend_usd": 0.0,
        }
        self.ledger.save()

    def check_call(self, experiment: str, run_id: str) -> None:
        """Before a paid call: the caps hold and the run hasn't been aborted."""
        if self._run(experiment, run_id)["status"] == "aborted":
            raise RunAborted(f"run {run_id} was aborted")
        self._require(experiment)

    def charge(self, experiment: str, run_id: str, *, cost_usd: float, spend_usd: float) -> None:
        """Record one call; abort the run if it's over its cap or 5x its group's median."""
        bucket = self.config.bucket_for(experiment).name
        run = self._run(experiment, run_id)
        run["calls"] += 1
        run["cost_usd"] = _usd(run["cost_usd"] + cost_usd)
        run["spend_usd"] = _usd(run["spend_usd"] + spend_usd)
        self.ledger.add_spend(experiment, bucket, spend_usd)
        why = self._abort_reason(experiment, run)
        if why is not None:
            run["status"] = "aborted"
            run["abort_reason"] = why
        self.ledger.save()
        if why is not None:
            raise RunAborted(f"run {run_id}: {why}")

    def _abort_reason(self, experiment: str, run: dict[str, Any]) -> str | None:
        cost = float(run["cost_usd"])
        if cost > self.config.run_cap_usd:
            return f"cost ${cost:.4f} is over the per-run cap ${self.config.run_cap_usd:.2f}"
        median = self.median_cost(experiment, run["group"])
        factor = self.config.median_abort_factor
        if median is not None and median > 0 and cost > factor * median:
            return f"cost ${cost:.4f} is over {factor}x the median ${median:.4f} (a loop bug?)"
        return None

    def median_cost(self, experiment: str, group: str) -> float | None:
        """Median cost of the group's completed runs, once there are enough of them."""
        bucket = self.config.bucket_for(experiment).name
        runs = self.ledger.experiment(experiment, bucket)["runs"].values()
        costs = [
            float(r["cost_usd"]) for r in runs if r["group"] == group and r["status"] == "done"
        ]
        if len(costs) < self.config.median_min_runs:
            return None
        return statistics.median(costs)

    def finish_run(self, experiment: str, run_id: str) -> None:
        run = self._run(experiment, run_id)
        if run["status"] != "aborted":
            run["status"] = "done"
        self.ledger.save()

    # --- batches -----------------------------------------------------------------------------

    def charge_batch(
        self, experiment: str, batch_id: str, *, spend_usd: float, requests: int
    ) -> None:
        bucket = self.config.bucket_for(experiment).name
        self.ledger.data["batches"][batch_id] = {
            "experiment": experiment,
            "requests": requests,
            "spend_usd": _usd(spend_usd),
        }
        self.ledger.add_spend(experiment, bucket, spend_usd)
        self.ledger.save()
