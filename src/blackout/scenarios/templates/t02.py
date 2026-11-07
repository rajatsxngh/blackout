"""T02, cascade: substation A fails, its zones move to B, and B trips after the grace period
unless load is shed. Good play sheds residential load fast and sends a crew (PRD 8.6).

Every zone A feeds falls back to B. B's capacity is set so that the transfer overloads it by
an amount that shedding B's residential zones can cover, while B carries its normal load with
the usual headroom. Hospital fuel is low enough that the dark city costs hospital-minutes.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    repair_minutes,
    report_depot,
    report_failure,
    report_fuel,
)
from blackout.scenarios.world import Draft, Infeasible
from blackout.sim.events import SubstationFail


class Cascade:
    id = "T02"
    title = "Cascade"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T02
        failing = rng.pick(draft.substations)
        column = int(failing.id.removeprefix("S")) - 1
        neighbors = [c for c in (column - 1, column + 1) if 0 <= c < draft.config.substation_count]
        backup = draft.substation(f"S{rng.pick(neighbors) + 1}")
        moved = [z for z in draft.zones if z.primary == failing.id]
        for zone in moved:
            draft.update_zone(zone.id, alternate=backup.id)

        after = [z for z in draft.zones if z.primary in (failing.id, backup.id)]
        normal = sum(z.demand_kw for z in draft.zones if z.primary == backup.id)
        transferred = sum(z.demand_kw for z in moved)
        sheddable = sum(z.demand_kw for z in after if z.priority == "residential")
        headroom = draft.config.substations.headroom_kw
        low, high = cfg.overload_kw
        high = min(high, sheddable, transferred - headroom)
        step = draft.config.zones.demand_step_kw
        if high < low:
            raise Infeasible("the transfer can't overload the backup by a sheddable amount")
        overload = rng.between(low, high, step)
        capacity = normal + transferred - overload
        draft.update_substation(backup.id, capacity_kw=capacity)

        for hospital in draft.hospitals:
            draft.update_hospital(hospital.id, fuel_l=rng.between(*cfg.hospital_fuel_l, 10))

        failed_at = rng.between(*cfg.fail_tick)
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        draft.events.append(
            SubstationFail(tick=failed_at, substation=failing.id, repair_minutes=repair)
        )
        draft.story.append(
            f"{failing.name} ({failing.id}) fails at t={failed_at} ({repair} min of crew work)."
            f" Its zones fall back to {backup.name} ({backup.id}), {overload} kW over its"
            f" {capacity} kW capacity; shedding residential load ({sheddable} kW) prevents the"
            " trip."
        )

        at_risk = [h for h in draft.hospitals if draft.zone_of(h.node) in after]
        for hospital in at_risk or draft.hospitals[:1]:
            report_fuel(draft, rng, hospital.id, unpowered_from=None)
        report_failure(draft, rng, failing.id, failed_at, repair)
        report_depot(draft, rng)

    def problem(self, validity: Validity) -> str | None:
        if validity.do_nothing_trips == 0:
            return "nothing trips under do-nothing"
        return do_nothing_problem(validity)
