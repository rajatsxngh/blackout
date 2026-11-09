"""C2 Solver and C3 Reference (docs/PRD.md section 8.7).

**C2 Solver** re-plans at every decision point:

1. Belief: telemetry, plus report facts (the keyword parser reads them at L1+) through the
   source filter (``policy.SourceFilter``): verified and field facts are believed, unverified
   ones only once corroborated.
2. If believing a parked unverified report would change what the plan does now, it asks
   ``confirm_report`` about it (while confirmations last).
3. ``solve_plan`` plans power, crews and fuel, and proposes the actions that change now.
   Only each truck's first trip is committed (rolling horizon).

**C3 Reference** runs the same planning code on ground truth: it builds a fresh belief from the
engine's current state at each decision point (every block, true fuel, true repair times), but
it knows nothing about future events. It's the only contestant allowed truth, and it must run
in an env built with ``wake_on_truth=True`` (``uses_truth`` tells the runners).
"""

import copy
from collections.abc import Callable, Sequence

from blackout.controllers.keyword_parser import KeywordParser
from blackout.controllers.solver.contract import SolverResult, solve_plan
from blackout.controllers.solver.policy import SourceFilter
from blackout.info.belief import CERTAIN, BeliefState, Estimate
from blackout.info.observation import Observation, build_observation
from blackout.info.reports import Fact, ReportView
from blackout.info.roles import ROLE_ALL
from blackout.info.telemetry import Telemetry, readings_now, telemetry
from blackout.sim.actions import AnyAction, ConfirmReport
from blackout.sim.config import SimConfig
from blackout.sim.engine import Engine
from blackout.sim.entities import SubstationState

TRUTH = "truth"


class SolverController:
    """C2: the strongest non-AI baseline. ``solve_stats`` keeps one entry per solve, for
    solve-time reporting (summary.json only; wall time never reaches the run log)."""

    name = "solver"

    def __init__(self, config: SimConfig | None = None) -> None:
        self.config = config or SimConfig()
        self.reset()

    def reset(self) -> None:
        self.belief = BeliefState()
        self.last: SolverResult | None = None
        self.solve_stats: list[dict[str, object]] = []
        self._parser: KeywordParser | None = None
        self._filter = SourceFilter()
        self._asked: set[str] = set()

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        self._update(observation)
        plan = self._plan(self.belief)
        confirms = self._confirms(observation, plan)
        self.last = plan
        return [*confirms, *plan.actions]

    def run_stats(self) -> dict[str, object]:
        """Solve-time figures for summary.json: count, p50, p95 and max wall seconds."""
        times = sorted(float(s["wall_time_s"]) for s in self.solve_stats)  # type: ignore[arg-type]
        statuses: dict[str, int] = {}
        for s in self.solve_stats:
            statuses[str(s["status"])] = statuses.get(str(s["status"]), 0) + 1
        return {
            "solves": len(times),
            "statuses": dict(sorted(statuses.items())),
            "wall_time_s": {
                "p50": _percentile(times, 50),
                "p95": _percentile(times, 95),
                "max": times[-1] if times else 0.0,
            },
        }

    # --- belief ----------------------------------------------------------------------------

    def _update(self, observation: Observation) -> None:
        belief = self.belief
        belief.apply_telemetry(observation)
        for report in observation.reports:
            belief.reports[report.id] = report
            self._filter.take(belief, self._facts(observation, report), report)
        for reply in observation.confirmations:
            belief.apply_confirm(reply)
        replied = [r.report_id for r in observation.confirmations]
        self._filter.recheck(belief, replied)

    def _facts(self, observation: Observation, report: ReportView) -> Sequence[Fact]:
        """What ``report`` claims: its facts at L0, else what the keyword parser reads (C4
        overrides this with the LLM parser)."""
        if report.facts is not None:
            return [*report.facts]
        if not report.text:
            return []
        self._parser = self._parser or KeywordParser.for_observation(observation)
        return self._parser.parse(report.text)

    # --- planning --------------------------------------------------------------------------

    def _plan(self, belief: BeliefState) -> SolverResult:
        plan = solve_plan(belief, config=self.config, previous=self.last)
        stats = plan.stats
        self.solve_stats.append(
            {
                "tick": plan.tick,
                "status": stats.status,
                "wall_time_s": stats.wall_time_s,
                "deterministic_time": stats.deterministic_time,
                "options": stats.options,
            }
        )
        return plan

    def _confirms(self, observation: Observation, plan: SolverResult) -> list[ConfirmReport]:
        """Ask about parked reports that would change the plan's actions if believed."""
        left = observation.confirms_left
        asks: list[ConfirmReport] = []
        by_report: dict[str, list[Fact]] = {}
        for claim in self._filter.parked:
            by_report.setdefault(claim.report.id, []).append(claim.fact)
        for report_id in sorted(by_report):
            if len(asks) >= left:
                break
            if report_id in self._asked or report_id in observation.confirms_pending:
                continue
            report = self.belief.reports[report_id]
            trial = copy.deepcopy(self.belief)
            trial.apply_facts(by_report[report_id], lambda f, r: True, report=report)
            alternative = solve_plan(trial, config=self.config, previous=self.last)
            if _moves(alternative) != _moves(plan):
                self._asked.add(report_id)
                why = f"unverified {report_id} would change the plan: check it first"
                asks.append(ConfirmReport(report_id=report_id, reason=why))
        return asks


class ReferenceController(SolverController):
    """C3: the solver fed ground truth (current state only, no foresight)."""

    name = "reference"
    uses_truth = True

    def __init__(self, config: SimConfig | None = None) -> None:
        super().__init__(config)
        self._engine: Callable[[], Engine] | None = None

    def attach(self, engine: Callable[[], Engine]) -> None:
        """Give the reference read access to the engine (called by ``run_episode``)."""
        self._engine = engine

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        if self._engine is None:
            raise RuntimeError("the reference reads ground truth: attach() it to the env first")
        self.belief = truth_belief(self._engine(), observation)
        plan = self._plan(self.belief)
        self.last = plan
        return list(plan.actions)


def truth_belief(engine: Engine, observation: Observation) -> BeliefState:
    """A belief that is exactly the engine's current state: live readings, every block
    (found or not), and the true crew minutes each failed substation needs."""
    seen = telemetry(engine, readings_now(engine))
    full = Telemetry(
        trucks=seen.trucks,
        crews=seen.crews,
        hospitals=seen.hospitals,
        depots=seen.depots,
        substations=seen.substations,
        zones=seen.zones,
        known_blocks=tuple(sorted(engine.blocked)),
    )
    truth = build_observation(
        ROLE_ALL, engine.tick, engine.horizon, engine.city, full, triggers=observation.triggers
    )
    belief = BeliefState()
    belief.apply_telemetry(truth)
    for substation_id in sorted(engine.substations):
        s = engine.substations[substation_id]
        if s.state is SubstationState.FAILED and s.repair_minutes is not None:
            estimate = Estimate(s.repair_minutes, engine.tick, TRUTH, CERTAIN)
            belief.substations[substation_id].eta_minutes = estimate
    return belief


def _percentile(sorted_values: Sequence[float], pct: int) -> float:
    """Nearest-rank percentile (0.0 for no values)."""
    if not sorted_values:
        return 0.0
    rank = max(1, -(-pct * len(sorted_values) // 100))
    return sorted_values[rank - 1]


def _moves(plan: SolverResult) -> list[object]:
    """What a plan does now: its actions (without reasons or the roads they avoid, which any
    new block report changes) and the route of every truck's first trip."""
    actions = [a.model_dump(mode="json", exclude={"reason", "avoid_edges"}) for a in plan.actions]
    routes = [(t.truck_id, t.trips[0].route if t.trips else ()) for t in plan.trucks]
    return [*actions, *routes]
