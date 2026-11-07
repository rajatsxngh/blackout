"""T07, crew dilemma: two substations are down, one quick to fix and one slow, and only one
crew is free. Good play orders the repairs by critical load restored per minute of work
(Smith's rule), not by load alone (PRD 8.6).

Each substation is the only working feeder of a hospital's zone, so both hospitals run on
their generators until their substation is fixed. When the two disagree, the slow repair
carries more priority-weighted load, so "biggest load first" and "most load per minute
first" pick differently. The fleet is small (two trucks) and the depots hold little, so
fuel alone can't paper over a bad repair order. The quick repair's hospital is the target
whose fuel calibration sets; the other starts with a modest tank. Both repairs are reported
with an estimate.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    report_failure,
    report_fuel,
    scatter_fleet,
)
from blackout.scenarios.world import Draft, Infeasible
from blackout.sim.config import SimConfig

SIM = SimConfig()


class CrewDilemma:
    id = "T07"
    title = "Crew dilemma"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T07
        by_feeder: dict[str, list[str]] = {}
        for hospital in draft.hospitals:
            by_feeder.setdefault(draft.zone_of(hospital.node).primary, []).append(hospital.id)
        if len(by_feeder) < 2:
            raise Infeasible("every hospital hangs off one substation")
        a, b = rng.sample(sorted(by_feeder), 2)

        def weighted(substation_id: str) -> int:
            return sum(
                z.demand_kw * SIM.weight(z.priority)  # type: ignore[arg-type]
                for z in draft.zones
                if z.primary == substation_id
            )

        quick, slow = (a, b) if weighted(a) <= weighted(b) else (b, a)
        step = draft.config.repair_step_minutes
        repairs = {
            quick: rng.between(*cfg.short_repair_minutes, step),
            slow: rng.between(*cfg.long_repair_minutes, step),
        }
        target = rng.pick(by_feeder[quick])
        other = rng.pick(by_feeder[slow])
        draft.target = target
        for hospital_id, substation_id in ((target, quick), (other, slow)):
            isolate_hospital(draft, hospital_id, repairs[substation_id])
        burn = draft.hospital(other).burn_lpm
        draft.update_hospital(other, fuel_l=rng.between(*cfg.second_fuel_minutes) * burn)
        draft.crews = draft.crews[:1]
        draft.trucks = draft.trucks[: cfg.trucks]
        step_l = draft.config.depots.stock_step_l
        draft.depots = [
            d.model_copy(update={"stock_l": rng.between(*cfg.depot_stock_l, step_l)})
            for d in draft.depots
        ]
        scatter_fleet(draft, rng)
        draft.story.append(
            f"One crew for two repairs: {quick} ({repairs[quick]} min, weighted load"
            f" {weighted(quick)}) and {slow} ({repairs[slow]} min, weighted load"
            f" {weighted(slow)}). Two trucks; depots hold"
            f" {', '.join(f'{d.stock_l} L' for d in draft.depots)}."
        )
        for hospital_id in (target, other):
            report_fuel(draft, rng, hospital_id, unpowered_from=0)
        for substation_id in (quick, slow):
            report_failure(draft, rng, substation_id, 0, repairs[substation_id])

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
