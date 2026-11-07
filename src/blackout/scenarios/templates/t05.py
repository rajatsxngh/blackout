"""T05, burn spike: a hospital's generator suddenly burns faster; staff report it as it
happens. Good play re-prioritizes: the spiking hospital's deadline moves up (PRD 8.6).

Two hospitals are cut off from the grid and the fleet is spread over the city. Some minutes
in, one of them (the target, whose fuel calibration sets) jumps to 4-5 L/min. The other
starts with a modest tank. Telemetry shows the new rate at once (D020), so the staff report
confirms it rather than warning ahead of it.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    repair_minutes,
    report_failure,
    report_fuel,
    scatter_fleet,
)
from blackout.scenarios.templates.parts import spike
from blackout.scenarios.world import Draft


class BurnSpike:
    id = "T05"
    title = "Burn spike"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T05
        target, other = rng.sample(draft.hospitals, 2)
        draft.target = target.id
        down: set[str] = set()
        for hospital in (target, other):
            repair = repair_minutes(draft, rng, cfg.repair_minutes)
            down.add(isolate_hospital(draft, hospital.id, repair))
        draft.update_hospital(
            other.id, fuel_l=rng.between(*cfg.second_fuel_minutes) * other.burn_lpm
        )
        scatter_fleet(draft, rng)
        spike(draft, rng, target.id, tick=cfg.spike_tick, lpm=cfg.spike_lpm)
        for hospital in (target, other):
            report_fuel(draft, rng, hospital.id, unpowered_from=0)
        for substation_id in sorted(down):
            repair = draft.substation(substation_id).repair_minutes
            assert repair is not None
            report_failure(draft, rng, substation_id, 0, repair)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
