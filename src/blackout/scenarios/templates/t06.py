"""T06, depot shortage: the depot nearest the hospital is nearly empty. Good play fetches
fuel from the far depot in time instead of making a short trip for a few liters (PRD 8.6).

A hospital is cut off from the grid. The depot with the fastest drive to it holds only a
few hundred liters (its staff say so early on); the other depot is full. The trucks start
empty, parked at the nearly empty depot. At L2 the stock claims disagree (a false one joins
the true one). Calibration sets the hospital's fuel.
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
from blackout.scenarios.templates.parts import shortage
from blackout.scenarios.world import Draft


class DepotShortage:
    id = "T06"
    title = "Depot shortage"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T06
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        low_depot = shortage(draft, rng, low_l=cfg.low_stock_l, far_l=cfg.far_stock_l)
        node = next(d.node for d in draft.depots if d.id == low_depot)
        for truck in draft.trucks:
            draft.update_truck(truck.id, node=node, load_l=0)
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
