"""``confirm_report``: verified dispatch checks a report's claims against the truth.

The reply arrives ``confirm_delay_ticks`` (8) after the request and describes the world at
reply time, not request time. It's the one place the info layer reads hidden truth on a
contestant's behalf, which is why each run gets only ``confirm_limit`` (4) of them.
"""

from blackout.info.reports import ClaimCheck, ConfirmReply, Fact, FactType, Report, close_enough
from blackout.sim.engine import Engine
from blackout.sim.entities import SubstationState


def check_claims(engine: Engine, report: Report, requested_at: int) -> ConfirmReply:
    return ConfirmReply(
        report_id=report.id,
        requested_at=requested_at,
        tick=engine.tick,
        checks=tuple(_check(engine, fact) for fact in report.facts),
    )


def _check(engine: Engine, fact: Fact) -> ClaimCheck:
    subject = fact.subject
    match fact.type:
        case FactType.ROAD_BLOCKED | FactType.ROAD_CLEARED:
            blocked = subject in engine.blocked
            holds = blocked == (fact.type is FactType.ROAD_BLOCKED)
            return ClaimCheck(fact, holds, "blocked" if blocked else "open")
        case FactType.SUBSTATION_STATUS:
            state = engine.substations[subject].state.value
            return ClaimCheck(fact, fact.value == state, state)
        case FactType.REPAIR_ETA:
            substation = engine.substations[subject]
            if substation.state is SubstationState.REPAIRING:
                actual = substation.work_left
            elif substation.state is SubstationState.FAILED:
                actual = substation.repair_minutes or 0
            elif substation.state is SubstationState.TRIPPED:
                actual = engine.config.reset_minutes
            else:
                actual = 0
        case FactType.HOSPITAL_FUEL:
            actual = engine.hospitals[subject].fuel_l
        case FactType.BURN_SPIKE:
            actual = engine.hospitals[subject].burn_lpm
        case FactType.DEPOT_STOCK:
            actual = engine.depots[subject].stock_l
    assert isinstance(fact.value, int)
    return ClaimCheck(fact, close_enough(fact.value, actual), actual)
