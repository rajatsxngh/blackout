"""Scoring a run from its log alone (docs/PRD.md section 8.10, invariant I5).

The scorer never trusts a number that a contestant, or the process that ran it, reported:

* outcome metrics come from replaying the log with the referee (the engine), after every
  state hash, decision point and action outcome has been checked;
* process metrics are counted directly from the decision records.

Both are cross-checked against the log's end record. Any difference is a discrepancy.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from blackout.logs.reader import RunLog, read_log
from blackout.logs.replay import replay


@dataclass(frozen=True, slots=True)
class ScoreCard:
    run_id: str
    scenario_id: str
    contestant: str
    model: str | None
    run_index: int
    final_tick: int
    end_reason: str
    run_hash: str
    # Outcomes, recomputed by replaying the log.
    critical_downtime_min: int
    hospitals_lost_power: int
    dark_minutes: dict[str, int]
    unmet_kw_min: int
    unmet_weighted_kw_min: int
    zone_outage_minutes: dict[str, int]
    power_restored_at: int | None
    fuel_loaded_l: int
    fuel_delivered_l: int
    fuel_burned_l: int
    truck_drive_minutes: int
    truck_road_segments: int
    crew_drive_minutes: int
    stuck_events: int
    trips: int
    repairs_completed: int
    # Process, counted from the decision records.
    decision_points: int
    actions: int
    actions_accepted: int
    actions_rejected: dict[str, int]
    actions_by_kind: dict[str, int]
    invalid_action_rate: float | None
    # Model usage (filled from the reserved llm fields once M10 records them).
    llm_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cost_usd: float | None
    thinking_time_ms: int | None
    # Cross-checks against the end record.
    discrepancies: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["discrepancies"] = list(self.discrepancies)
        return data


@dataclass(frozen=True, slots=True)
class _Counts:
    decision_points: int
    actions: int
    accepted: int
    rejected: dict[str, int]
    by_kind: dict[str, int]


def score(source: RunLog | Path) -> ScoreCard:
    """Score one run. Raises a LogError if the log can't be read or doesn't replay."""
    log = source if isinstance(source, RunLog) else read_log(source)
    replayed = replay(log)
    m = replayed.metrics
    counts = _count(log)
    llm = [record.llm for record in log.decisions if record.llm is not None]
    done = replayed.episode.done
    header = log.header
    return ScoreCard(
        run_id=header.run_id,
        scenario_id=header.scenario.id,
        contestant=header.contestant.name,
        model=header.contestant.model,
        run_index=header.contestant.run_index,
        final_tick=done.tick,
        end_reason=done.reason,
        run_hash=replayed.episode.run_hash,
        critical_downtime_min=m.critical_downtime,
        hospitals_lost_power=m.hospitals_lost_power,
        dark_minutes=dict(sorted(m.dark_minutes.items())),
        unmet_kw_min=m.unmet_kw_min,
        unmet_weighted_kw_min=m.unmet_weighted_kw_min,
        zone_outage_minutes=dict(sorted(m.zone_outage_minutes.items())),
        power_restored_at=replayed.power_restored_at,
        fuel_loaded_l=m.fuel_loaded_l,
        fuel_delivered_l=m.fuel_delivered_l,
        fuel_burned_l=m.fuel_burned_l,
        truck_drive_minutes=m.drive_minutes,
        truck_road_segments=m.edges_traversed,
        crew_drive_minutes=m.crew_drive_minutes,
        stuck_events=m.stuck_events,
        trips=m.trips,
        repairs_completed=m.repairs_completed,
        decision_points=counts.decision_points,
        actions=counts.actions,
        actions_accepted=counts.accepted,
        actions_rejected=counts.rejected,
        actions_by_kind=counts.by_kind,
        invalid_action_rate=(
            sum(counts.rejected.values()) / counts.actions if counts.actions else None
        ),
        llm_calls=sum(r.calls for r in llm) if llm else None,
        input_tokens=sum(r.input_tokens for r in llm) if llm else None,
        output_tokens=sum(r.output_tokens for r in llm) if llm else None,
        cache_read_input_tokens=sum(r.cache_read_input_tokens for r in llm) if llm else None,
        cost_usd=sum(r.cost_usd for r in llm) if llm else None,
        thinking_time_ms=sum(r.latency_ms for r in llm) if llm else None,
        discrepancies=_cross_check(log, replayed.report, counts),
    )


def _count(log: RunLog) -> _Counts:
    actions = [action for record in log.decisions for action in record.actions]
    rejected = Counter(a.reason for a in actions if not a.accepted and a.reason is not None)
    kinds = Counter(str(a.raw.get("kind")) for a in actions)
    return _Counts(
        decision_points=len(log.decisions),
        actions=len(actions),
        accepted=sum(1 for a in actions if a.accepted),
        rejected=dict(sorted(rejected.items())),
        by_kind=dict(sorted(kinds.items())),
    )


def _cross_check(log: RunLog, report: dict[str, object], counts: _Counts) -> tuple[str, ...]:
    end = log.end
    if end is None:
        return ("the log has no end record, so the run didn't finish",)
    problems = [
        f"{key}: the end record says {end.metrics.get(key)!r}, the replay gives {value!r}"
        for key, value in sorted(report.items())
        if end.metrics.get(key) != value
    ]
    problems += [
        f"{key}: in the end record but not in the replay's report"
        for key in sorted(set(end.metrics) - set(report))
    ]
    counted = {
        "decision_points": counts.decision_points,
        "actions_accepted": counts.accepted,
        "actions_rejected": counts.rejected,
    }
    problems += [
        f"{key}: the decision records give {value!r}, the end record says {end.metrics.get(key)!r}"
        for key, value in counted.items()
        if end.metrics.get(key) != value
    ]
    if end.decision_points != counts.decision_points:
        problems.append(
            f"the end record counts {end.decision_points} decision points,"
            f" the log has {counts.decision_points}"
        )
    return tuple(problems)
