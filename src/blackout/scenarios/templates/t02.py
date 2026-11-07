"""T02, cascade: substation A fails, its zones move to B, and B trips after the grace period
unless load is shed. Good play sheds residential load fast and sends a crew (PRD 8.6).

A is a substation that feeds a hospital's zone, and that zone has no working backup, so the
hospital is on its generator from the failure until a crew repairs A (calibration sets its
fuel). Every other zone A feeds falls back to B. B's capacity is set so that the transfer
overloads it by an amount that shedding residential zones can cover. The other hospitals
start with modest fuel, so letting B trip costs hospital-minutes too.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    repair_minutes,
    report_depot,
    report_failure,
    report_fuel,
    scatter_fleet,
)
from blackout.scenarios.world import Draft, Infeasible
from blackout.sim.events import SubstationFail


class Cascade:
    id = "T02"
    title = "Cascade"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T02
        feeders = sorted({draft.zone_of(h.node).primary for h in draft.hospitals})
        failing = draft.substation(rng.pick(feeders))
        fed = [h for h in draft.hospitals if draft.zone_of(h.node).primary == failing.id]
        target = rng.pick(fed)
        draft.target = target.id
        isolated = draft.zone_of(target.node)
        draft.update_zone(isolated.id, alternate=None)

        column = int(failing.id.removeprefix("S")) - 1
        count = draft.config.substation_count
        neighbors = [c for c in (column - 1, column + 1) if 0 <= c < count]
        backup = draft.substation(f"S{rng.pick(neighbors) + 1}")
        moved = [z for z in draft.zones if z.primary == failing.id and z.id != isolated.id]
        for zone in moved:
            draft.update_zone(zone.id, alternate=backup.id)
        own = [z for z in draft.zones if z.primary == backup.id]
        normal = sum(z.demand_kw for z in own)
        transferred = sum(z.demand_kw for z in moved)
        sheddable = sum(z.demand_kw for z in [*own, *moved] if z.priority == "residential")
        headroom = draft.config.substations.headroom_kw
        low, high = cfg.overload_kw
        high = min(high, sheddable, transferred - headroom)
        if high < low:
            raise Infeasible("the transfer can't overload the backup by a sheddable amount")
        overload = rng.between(low, high, draft.config.zones.demand_step_kw)
        capacity = normal + transferred - overload
        draft.update_substation(backup.id, capacity_kw=capacity)

        for hospital in draft.hospitals:
            if hospital.id != target.id:
                draft.update_hospital(hospital.id, fuel_l=rng.between(*cfg.hospital_fuel_l, 10))
        scatter_fleet(draft, rng)

        failed_at = rng.between(*cfg.fail_tick)
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        draft.events.append(
            SubstationFail(tick=failed_at, substation=failing.id, repair_minutes=repair)
        )
        draft.story.append(
            f"{failing.name} ({failing.id}) fails at t={failed_at} ({repair} min of crew work),"
            f" leaving {target.id} on its generator. Its other zones fall back to {backup.name}"
            f" ({backup.id}), {overload} kW over its {capacity} kW capacity; shedding"
            f" residential load ({sheddable} kW) prevents the trip."
        )
        for hospital in draft.hospitals:
            report_fuel(draft, rng, hospital.id, unpowered_from=None)
        report_failure(draft, rng, failing.id, failed_at, repair)
        report_depot(draft, rng)

    def problem(self, validity: Validity) -> str | None:
        if validity.do_nothing_trips == 0:
            return "nothing trips under do-nothing"
        return do_nothing_problem(validity)
