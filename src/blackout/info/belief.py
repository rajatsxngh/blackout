"""What a contestant thinks is true (docs/PRD.md section 8.5).

A ``BeliefState`` is built only from observations, so it may be wrong or out of date: it
holds what telemetry, trusted reports and confirm replies have said, each value with its
provenance (source, confidence, and the tick it describes). Every contestant keeps its own,
per role.

When two pieces of information disagree, the one describing the later tick wins; on a tie the
more reliable one does. So a fresh report beats delayed telemetry, a later confirm reply beats
both, and discovering a block (telemetry) is later overridden by a report that it cleared.
Confidence is a whole percentage (telemetry and confirm replies are 100).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from blackout.info.observation import Observation, plain
from blackout.info.reports import RELIABILITY_PCT, ConfirmReply, Fact, FactType, ReportView
from blackout.info.roles import ROLE_ALL, Section, sees
from blackout.info.telemetry import (
    CrewView,
    DepotView,
    HospitalView,
    SubstationView,
    TruckView,
    ZoneView,
)
from blackout.sim.city import City, EdgeId

TELEMETRY = "telemetry"
CERTAIN = 100

BLOCKED = "blocked"
OPEN = "open"


@dataclass(frozen=True, slots=True)
class Estimate:
    """A believed value: what it is, as of which tick, according to whom, and how sure."""

    value: int | str
    as_of: int
    source: str
    confidence: int

    def beats(self, other: "Estimate | None") -> bool:
        """True if this should replace ``other``: it's newer, or as new and as reliable."""
        if other is None or self.as_of > other.as_of:
            return True
        return self.as_of == other.as_of and self.confidence >= other.confidence


type TrustPolicy = Callable[[Fact, ReportView], bool]
"""Decides whether to believe one fact from one report."""


def trust_all(fact: Fact, report: ReportView) -> bool:
    """Believe everything (the naive rules baseline, PRD section 8.7)."""
    return True


def trust_at_least(confidence: int) -> TrustPolicy:
    """Believe facts from sources at least this reliable (e.g. 85: verified and field)."""

    def policy(fact: Fact, report: ReportView) -> bool:
        return RELIABILITY_PCT[report.source_class] >= confidence

    return policy


@dataclass(slots=True)
class HospitalBelief:
    reading: HospitalView | None = None  # the latest telemetry, if this role gets any
    fuel_l: Estimate | None = None
    burn_lpm: Estimate | None = None


@dataclass(slots=True)
class SubstationBelief:
    reading: SubstationView | None = None
    state: Estimate | None = None
    eta_minutes: Estimate | None = None  # crew minutes left to fix it


@dataclass(slots=True)
class DepotBelief:
    reading: DepotView | None = None
    stock_l: Estimate | None = None


@dataclass(slots=True)
class BeliefState:
    """One role's picture of the world. Update it with ``observe`` at every decision point."""

    role: str = ROLE_ALL
    tick: int = 0
    city: City | None = None
    roads: dict[EdgeId, Estimate] = field(default_factory=dict)  # "blocked" or "open"
    hospitals: dict[str, HospitalBelief] = field(default_factory=dict)
    substations: dict[str, SubstationBelief] = field(default_factory=dict)
    depots: dict[str, DepotBelief] = field(default_factory=dict)
    trucks: dict[str, TruckView] = field(default_factory=dict)
    crews: dict[str, CrewView] = field(default_factory=dict)
    zones: dict[str, ZoneView] = field(default_factory=dict)
    reports: dict[str, ReportView] = field(default_factory=dict)
    pending_confirms: tuple[str, ...] = ()
    confirms_left: int = 0
    _discovered: dict[EdgeId, int] = field(default_factory=dict)  # block -> first seen

    # --- updates ---------------------------------------------------------------------------

    def observe(self, observation: Observation, policy: TrustPolicy = trust_all) -> None:
        """Telemetry, then the structured facts of new reports, then confirm replies."""
        self.apply_telemetry(observation)
        for report in observation.reports:
            self.reports[report.id] = report
            if report.facts is not None:
                self.apply_facts(report.facts, policy, report=report)
        for reply in observation.confirmations:
            self.apply_confirm(reply)

    def apply_telemetry(self, observation: Observation) -> None:
        """Take in the telemetry sections the role can see. Telemetry is always true."""
        role, tick = observation.role, observation.tick
        self.role, self.tick, self.city = role, tick, observation.city
        if sees(role, Section.TRUCKS):
            self.trucks = {t.id: t for t in observation.trucks}
        if sees(role, Section.CREWS):
            self.crews = {c.id: c for c in observation.crews}
        if sees(role, Section.ZONES):
            self.zones = {z.id: z for z in observation.zones}
        for h in observation.hospitals:
            belief = self.hospitals.setdefault(h.id, HospitalBelief())
            belief.reading = h
            self._set_hospital(belief, "fuel_l", Estimate(h.fuel_l, h.as_of, TELEMETRY, CERTAIN))
            self._set_hospital(
                belief, "burn_lpm", Estimate(h.burn_lpm, h.as_of, TELEMETRY, CERTAIN)
            )
        for d in observation.depots:
            belief = self.depots.setdefault(d.id, DepotBelief())
            belief.reading = d
            stock = Estimate(d.stock_l, d.as_of, TELEMETRY, CERTAIN)
            if stock.beats(belief.stock_l):
                belief.stock_l = stock
        for s in observation.substations:
            belief = self.substations.setdefault(s.id, SubstationBelief())
            belief.reading = s
            state = Estimate(s.state.value, tick, TELEMETRY, CERTAIN)
            if state.beats(belief.state):
                belief.state = state
            if s.work_left is not None:
                belief.eta_minutes = Estimate(s.work_left, tick, TELEMETRY, CERTAIN)
            elif s.state.value == "online":
                belief.eta_minutes = Estimate(0, tick, TELEMETRY, CERTAIN)
            elif belief.eta_minutes is not None and belief.eta_minutes.source == TELEMETRY:
                belief.eta_minutes = None  # down again: the old reading says nothing now
        for segment in observation.known_blocks:
            if segment not in self._discovered:
                self._discovered[segment] = tick
                self._set_road(segment, Estimate(BLOCKED, tick, TELEMETRY, CERTAIN))
        self.confirms_left = observation.confirms_left
        self.pending_confirms = observation.confirms_pending

    def apply_facts(
        self, facts: Sequence[Fact], policy: TrustPolicy, *, report: ReportView
    ) -> list[Fact]:
        """Believe the facts ``policy`` trusts; returns the ones it took in."""
        confidence = RELIABILITY_PCT[report.source_class]
        accepted = [fact for fact in facts if policy(fact, report)]
        for fact in accepted:
            self._apply(fact, fact.value, report.tick, report.id, confidence)
        return accepted

    def apply_confirm(self, reply: ConfirmReply) -> None:
        """Verified dispatch's answer: the true value of every claim, as of the reply."""
        source = f"confirm:{reply.report_id}"
        for check in reply.checks:
            fact = check.fact
            if fact.type in (FactType.ROAD_BLOCKED, FactType.ROAD_CLEARED):
                assert isinstance(check.actual, str)
                self._set_road(fact.subject, Estimate(check.actual, reply.tick, source, CERTAIN))
            else:
                self._apply(fact, check.actual, reply.tick, source, CERTAIN)
        self.pending_confirms = tuple(p for p in self.pending_confirms if p != reply.report_id)

    def _apply(
        self, fact: Fact, value: int | str | None, as_of: int, source: str, confidence: int
    ) -> None:
        subject = fact.subject
        match fact.type:
            case FactType.ROAD_BLOCKED:
                self._set_road(subject, Estimate(BLOCKED, as_of, source, confidence))
            case FactType.ROAD_CLEARED:
                self._set_road(subject, Estimate(OPEN, as_of, source, confidence))
            case FactType.SUBSTATION_STATUS | FactType.REPAIR_ETA:
                assert value is not None
                belief = self.substations.setdefault(subject, SubstationBelief())
                estimate = Estimate(value, as_of, source, confidence)
                if fact.type is FactType.SUBSTATION_STATUS and estimate.beats(belief.state):
                    belief.state = estimate
                elif fact.type is FactType.REPAIR_ETA and estimate.beats(belief.eta_minutes):
                    belief.eta_minutes = estimate
            case FactType.HOSPITAL_FUEL | FactType.BURN_SPIKE:
                assert value is not None
                belief = self.hospitals.setdefault(subject, HospitalBelief())
                name = "fuel_l" if fact.type is FactType.HOSPITAL_FUEL else "burn_lpm"
                self._set_hospital(belief, name, Estimate(value, as_of, source, confidence))
            case FactType.DEPOT_STOCK:
                assert value is not None
                belief = self.depots.setdefault(subject, DepotBelief())
                estimate = Estimate(value, as_of, source, confidence)
                if estimate.beats(belief.stock_l):
                    belief.stock_l = estimate

    def _set_road(self, segment: EdgeId, estimate: Estimate) -> None:
        if estimate.beats(self.roads.get(segment)):
            self.roads[segment] = estimate

    @staticmethod
    def _set_hospital(belief: HospitalBelief, name: str, estimate: Estimate) -> None:
        if estimate.beats(getattr(belief, name)):
            setattr(belief, name, estimate)

    # --- reading ---------------------------------------------------------------------------

    def blocked_roads(self) -> tuple[EdgeId, ...]:
        """Road segments believed blocked, sorted."""
        return tuple(sorted(e for e, belief in self.roads.items() if belief.value == BLOCKED))

    def fuel_now(self, hospital_id: str) -> int | None:
        """Believed liters in the tank now: the last estimate, burned down since its tick."""
        belief = self.hospitals.get(hospital_id)
        if belief is None or belief.fuel_l is None:
            return None
        fuel, burn = belief.fuel_l, self.burn(hospital_id)
        assert isinstance(fuel.value, int)
        if burn is None or self.powered(hospital_id):
            return fuel.value
        return max(0, fuel.value - burn * max(0, self.tick - fuel.as_of))

    def burn(self, hospital_id: str) -> int | None:
        belief = self.hospitals.get(hospital_id)
        if belief is None or belief.burn_lpm is None:
            return None
        assert isinstance(belief.burn_lpm.value, int)
        return belief.burn_lpm.value

    def powered(self, hospital_id: str) -> bool:
        """Whether the hospital is believed to have grid power. Zone telemetry is real-time,
        so it's preferred over a (possibly delayed) hospital reading."""
        belief = self.hospitals.get(hospital_id)
        reading = None if belief is None else belief.reading
        if reading is None:
            return False
        zone = self.zones.get(reading.zone or "")
        return zone.powered if zone is not None else reading.powered

    def digest(self) -> dict[str, object]:
        """What the contestant believed, for the run log's ``dp`` records and the viewer."""
        return {
            "tick": self.tick,
            "role": self.role,
            "roads": {e: _estimate(self.roads[e]) for e in sorted(self.roads)},
            "hospitals": {
                h: {
                    "fuel_l": _estimate(self.hospitals[h].fuel_l),
                    "burn_lpm": _estimate(self.hospitals[h].burn_lpm),
                }
                for h in sorted(self.hospitals)
            },
            "substations": {
                s: {
                    "state": _estimate(self.substations[s].state),
                    "eta_minutes": _estimate(self.substations[s].eta_minutes),
                }
                for s in sorted(self.substations)
            },
            "depots": {
                d: {"stock_l": _estimate(self.depots[d].stock_l)} for d in sorted(self.depots)
            },
            "pending_confirms": list(self.pending_confirms),
            "confirms_left": self.confirms_left,
        }


def _estimate(estimate: Estimate | None) -> object:
    return None if estimate is None else plain(estimate)
