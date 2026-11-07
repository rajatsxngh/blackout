"""T03, two hospitals, one truck: two hospitals are low on fuel; one loaded truck is near,
every other truck is far. Good play orders the two by deadline *and* travel, or splits the
load between them (PRD 8.6).

Both hospitals are cut off from the grid. The one loaded truck starts a few minutes from one
of them; every other truck starts empty and far from both. One hospital's fuel is drawn;
calibration sets the other's (the target, chosen at random).
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    repair_minutes,
    report_failure,
    report_fuel,
)
from blackout.scenarios.world import Draft, Infeasible, fastest
from blackout.sim.city import node_ids


class TwoHospitalsOneTruck:
    id = "T03"
    title = "Two hospitals, one truck"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T03
        city = draft.built_city()
        first, second = rng.sample(draft.hospitals, 2)
        down: set[str] = set()
        for hospital in (first, second):
            repair = repair_minutes(draft, rng, cfg.repair_minutes)
            down.add(isolate_hospital(draft, hospital.id, repair))
        target, other = (first, second) if rng.chance(50) else (second, first)
        draft.target = target.id
        fuel = rng.between(*cfg.second_fuel_minutes) * other.burn_lpm
        draft.update_hospital(other.id, fuel_l=fuel)

        def minutes(node: str, to: str) -> int:
            found = fastest(city, node, to)
            return 10**6 if found is None else found[1]

        nodes = node_ids(city.width, city.height)
        low, high = cfg.near_minutes
        near = [n for n in nodes if low <= minutes(n, first.node) <= high]
        far = [
            n
            for n in nodes
            if min(minutes(n, first.node), minutes(n, second.node)) >= cfg.far_minutes
        ]
        if not near or not far:
            raise Infeasible("no spot for the near truck or the far ones")
        loaded = rng.pick(draft.trucks)
        draft.update_truck(loaded.id, node=rng.pick(near), load_l=loaded.capacity_l)
        for truck in draft.trucks:
            if truck.id != loaded.id:
                draft.update_truck(truck.id, node=rng.pick(far), load_l=0)
        draft.story.append(
            f"{loaded.id} is loaded and a few minutes from {first.id}; every other truck is"
            f" empty and at least {cfg.far_minutes} min from both hospitals. {other.id} has"
            f" {fuel} L."
        )
        for hospital in (first, second):
            report_fuel(draft, rng, hospital.id, unpowered_from=0)
        for substation_id in sorted(down):
            repair = draft.substation(substation_id).repair_minutes
            assert repair is not None
            report_failure(draft, rng, substation_id, 0, repair)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
