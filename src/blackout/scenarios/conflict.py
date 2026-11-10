"""L2: false, stale and correcting reports, each false one with a guaranteed cue (PRD 8.4).

``plan_conflicts`` adds reports to a finished draft at level 2 (L3 keeps them). Each one is
built from the world's own timeline, so its claim is **false whatever the contestants do**
(road status and burn rates change only through scenario events, depot stock only goes down,
a tank can't lose more than its fastest burn, a repair can't finish sooner than its work).
False reports come only from field and unverified sources (verified sources are right 98% of
the time, and the generator never makes them lie). Every one gets at least one cue:

- **report**: a true report from a *more reliable* source within 10 ticks that contradicts
  it. It may come first (a contestant that believes "the latest word" is fooled) or as a
  later correction (``corrects``).
- **telemetry**: the claim is about something the receiving roles see in telemetry (a
  hospital's fuel or burn rate, a substation's state, a depot's stock), which shows
  otherwise.

``confirm_report`` works on every report too, so it's a cue as well, but never the only one.

What gets planned (``ConflictCfg``): first the *harmful* false reports the world has room
for, timed while the tight hospital's first delivery is still on its way: a false "cleared"
on a real block just after police report it, and a false block on the fast route to the
tight hospital. These change where trucks drive; telemetry can't contradict them. Then
sometimes a *stale* one ("still blocked" after a reported reopening), then more false
reports of other kinds (all contradicted by telemetry) up to 2-3 in all.

``cue_problems`` is the automated check: it re-derives from a finished scenario that every
false report is false regardless of actions and that each of its cues holds.
"""

from collections.abc import Callable, Sequence

from blackout.info.reports import (
    CUE_WINDOW_TICKS,
    RELIABILITY_PCT,
    Channel,
    CueKind,
    Fact,
    FactType,
    Report,
    SourceClass,
    close_enough,
)
from blackout.scenarios.schema import Scenario
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.parts import nearest_depot
from blackout.scenarios.text.render import TimedFact
from blackout.scenarios.world import (
    Draft,
    PlannedCue,
    PlannedReport,
    depot_source,
    fact,
    fastest,
    police_source,
    public_source,
)
from blackout.sim.events import BurnRateChange, RoadBlock, RoadClear, SubstationFail

TRAFFIC_CENTER = "Traffic management center"
"""A verified source on the police channel: the city's traffic cameras and signal network."""

TELEMETRY_FACTS = frozenset(
    {FactType.HOSPITAL_FUEL, FactType.BURN_SPIKE, FactType.SUBSTATION_STATUS, FactType.DEPOT_STOCK}
)
"""Fact types that some role's telemetry always shows (PRD 8.4)."""

L2 = 2


# --- the world's timeline -------------------------------------------------------------------


type Events = Sequence[object]
"""Scenario events (physical ones matter; anything else is skipped)."""


def blocked_at(events: Events, segment: str, tick: int) -> bool:
    """Whether the segment is blocked at ``tick`` (events at a tick apply before it, D015)."""
    state = False
    for event in sorted(
        (e for e in events if isinstance(e, RoadBlock | RoadClear) and e.edge == segment),
        key=lambda e: e.tick,
    ):
        if event.tick > tick:
            break
        state = isinstance(event, RoadBlock)
    return state


def open_throughout(events: Events, segment: str, start: int, end: int) -> bool:
    return not any(blocked_at(events, segment, t) for t in range(max(0, start), end + 1))


def blocked_throughout(events: Events, segment: str, start: int, end: int) -> bool:
    return all(blocked_at(events, segment, t) for t in range(max(0, start), end + 1))


def burn_at(events: Events, hospital_id: str, initial: int, tick: int) -> int:
    burn = initial
    for event in sorted(
        (e for e in events if isinstance(e, BurnRateChange) and e.hospital == hospital_id),
        key=lambda e: e.tick,
    ):
        if event.tick <= tick:
            burn = event.burn_lpm
    return burn


def failures(initial: dict[str, int | None], events: Events) -> dict[str, list[tuple[int, int]]]:
    """Each substation's failures: (tick it failed, crew minutes the repair needs)."""
    out: dict[str, list[tuple[int, int]]] = {}
    for substation_id, repair in initial.items():
        if repair is not None:
            out.setdefault(substation_id, []).append((0, repair))
    for event in events:
        if isinstance(event, SubstationFail):
            out.setdefault(event.substation, []).append((event.tick, event.repair_minutes))
    return out


def least_work_left(
    failed: dict[str, list[tuple[int, int]]], substation_id: str, tick: int
) -> int | None:
    """The fewest crew minutes a substation can still need at ``tick`` (a crew can't have
    worked longer than the time since the failure); None if it can't be down then."""
    best = None
    for at, repair in failed.get(substation_id, []):
        if at <= tick:
            left = repair - (tick - at)
            if left > 0:
                best = left if best is None else min(best, left)
    return best


# --- planning --------------------------------------------------------------------------------


def plan_conflicts(draft: Draft, rng: Rng) -> None:
    """Add L2's false, stale and correcting reports to ``draft``."""
    cfg = draft.config.conflict
    wanted = rng.between(*cfg.false_reports)
    shared = [r for r in draft.reports if r.level == 0]
    made = 0
    for hospital_id in closure_targets(draft)[: cfg.closures]:
        if made < wanted and false_block(draft, rng, shared, hospital_id):
            made += 1
    if made < wanted and false_clear(draft, rng, shared):  # these change where trucks go
        made += 1
    if rng.chance(cfg.stale_pct):
        stale_block(draft, rng, shared)
    others: list[Callable[[Draft, Rng, list[PlannedReport]], bool]] = [
        false_status,
        false_eta,
        false_fuel,
        false_stock,
        false_spike,
    ]
    for maker in rng.shuffled(others):
        if made >= wanted:
            break
        if maker(draft, rng, shared):
            made += 1


def _add(draft: Draft, report: PlannedReport) -> PlannedReport:
    report.level = L2
    draft.reports.append(report)
    return report


def _false(
    draft: Draft,
    tick: int,
    channel: Channel,
    source_class: SourceClass,
    source: str,
    claim: Fact,
    *cues: PlannedCue,
) -> PlannedReport:
    report = PlannedReport(
        tick, channel, source_class, source, [TimedFact(claim, tick)], is_true=False
    )
    report.cues.extend(cues)
    return _add(draft, report)


def _true(
    draft: Draft, tick: int, channel: Channel, source_class: SourceClass, source: str, claim: Fact
) -> PlannedReport:
    return _add(draft, PlannedReport(tick, channel, source_class, source, [TimedFact(claim, tick)]))


def _road_facts(reports: list[PlannedReport], kind: FactType) -> list[tuple[PlannedReport, str]]:
    return [
        (r, t.fact.subject)
        for r in reports
        if r.source_class is not SourceClass.UNVERIFIED
        for t in r.facts
        if t.fact.type is kind
    ]


def false_clear(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """An unverified caller says a real, reported block has cleared, soon after police
    reported it. Cue: that police report (more reliable, within 10 ticks)."""
    horizon = draft.config.horizon
    options = []
    for report, segment in _road_facts(shared, FactType.ROAD_BLOCKED):
        start, end = report.tick + 1, report.tick + CUE_WINDOW_TICKS
        if end < horizon and blocked_throughout(draft.events, segment, start, end):
            options.append((report.tick, segment, report))
    if not options:
        return False
    _, segment, cue = min(options, key=lambda o: (o[0], o[1]))  # the earliest: it matters most
    tick = cue.tick + rng.between(1, 4)
    claim = fact(FactType.ROAD_CLEARED, segment)
    lie = _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        claim,
        PlannedCue(CueKind.REPORT, cue, "police reported the block shortly before"),
    )
    if rng.chance(draft.config.conflict.correction_pct):
        later = tick + rng.between(2, 8)
        if later < horizon and blocked_at(draft.events, segment, later):
            fix = _true(
                draft,
                later,
                Channel.POLICE_RADIO,
                SourceClass.FIELD,
                police_source(rng),
                fact(FactType.ROAD_BLOCKED, segment),
            )
            fix.corrects = lie
            lie.cues.append(PlannedCue(CueKind.REPORT, fix, "police correction"))
    draft.story.append(f"L2: a caller wrongly says {segment} has cleared at t={tick}.")
    return True


def closure_targets(draft: Draft) -> list[str]:
    """Hospitals a false closure goes around: the tight one first, then the others on their
    generators from the start."""
    if draft.target is None:
        return []
    return [draft.target, *(h for h in draft.hospitals_at_risk() if h != draft.target)]


def first_responder(draft: Draft, hospital_id: str | None = None) -> str:
    """Where the first fuel for a hospital (by default the tight one) most likely comes
    from: the loaded truck with the fastest drive to it, or else its nearest depot (PRD
    8.7's greedy rule)."""
    hospital_id = hospital_id or draft.target
    assert hospital_id is not None
    city = draft.built_city()
    node = draft.hospital(hospital_id).node
    loaded = []
    for truck in draft.trucks:
        found = fastest(city, truck.node, node) if truck.load_l > 0 else None
        if found is not None:
            loaded.append((found[1], truck.id, truck.node))
    if loaded:
        return min(loaded)[2]
    return nearest_depot(draft, hospital_id)[1][0]


def false_block(
    draft: Draft, rng: Rng, shared: list[PlannedReport], hospital_id: str | None = None
) -> bool:
    """A false closure of the roads into a hospital on its generator (by default the tight
    one), early on. The roads it names
    are open throughout. Cue: one report from a more reliable source that they're open,
    either

    - just before the lie (``closure.correction_pct`` of the time it isn't): then "believe
      the latest word" is fooled for good, and the lie names *every* free road in, so the
      hospital looks unreachable and a believer stops sending fuel; or
    - as a correction a few minutes after: then the lie leaves the slowest approach open,
      so the trucks already on their way are sent the long way round.
    """
    hospital_id = hospital_id or draft.target
    if hospital_id is None:
        return False
    cfg = draft.config.conflict
    city = draft.built_city()
    target = draft.hospital(hospital_id)
    origin = first_responder(draft, hospital_id)
    taken = {t.fact.subject for r in draft.reports for t in r.facts}
    correct = rng.chance(cfg.closure_correction_pct)
    tick = rng.between(*cfg.closure_tick)
    window = (tick - CUE_WINDOW_TICKS, tick + CUE_WINDOW_TICKS)
    found = fastest(city, origin, target.node)
    if found is None:
        return False
    into = sorted(e for e in city.edges if target.node in e.split("-"))
    free = [e for e in into if e not in taken and open_throughout(draft.events, e, *window)]
    if not free:
        return False
    claimed, left = tuple(free), "nothing"
    if correct:
        options = []  # (extra minutes, the approach left open, the roads claimed blocked)
        for kept in into:
            rest = tuple(e for e in free if e != kept)
            detour = fastest(city, origin, target.node, frozenset(rest)) if rest else None
            if detour is not None and detour[1] > found[1]:
                options.append((detour[1] - found[1], kept, rest))
        if not options:
            return False
        extra, kept, claimed = max(options, key=lambda o: (o[0], len(o[2]), o[1]))
        left = f"{kept}, {extra} min longer"
    if rng.chance(cfg.field_false_pct):
        liar = (Channel.POLICE_RADIO, SourceClass.FIELD, police_source(rng))
        honest = (Channel.POLICE_RADIO, SourceClass.VERIFIED, TRAFFIC_CENTER)
    else:
        liar = (Channel.PUBLIC, SourceClass.UNVERIFIED, public_source(rng))
        honest = (
            (Channel.POLICE_RADIO, SourceClass.VERIFIED, TRAFFIC_CENTER)
            if rng.chance(50)
            else (Channel.POLICE_RADIO, SourceClass.FIELD, police_source(rng))
        )
    when = tick + rng.between(2, 8) if correct else tick - rng.between(1, tick)
    truth = _add(
        draft,
        PlannedReport(
            when, *honest, [TimedFact(fact(FactType.ROAD_CLEARED, e), when) for e in claimed]
        ),
    )
    note = "correction from a more reliable source" if correct else "a more reliable source"
    lie = _add(
        draft,
        PlannedReport(
            tick,
            *liar,
            [TimedFact(fact(FactType.ROAD_BLOCKED, e), tick) for e in claimed],
            is_true=False,
        ),
    )
    lie.cues.append(PlannedCue(CueKind.REPORT, truth, note + " says they're open"))
    if correct:
        truth.corrects = lie
    draft.story.append(
        f"L2: a false closure of {', '.join(claimed)} around {target.id} at t={tick}"
        f" ({liar[1].value}, {'corrected' if correct else 'contradicted first'}; open:"
        f" {left})."
    )
    return True


def stale_block(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """After police report a road reopened, a caller says it's still blocked. Cue: the
    police report. The claim restates the old block (``stale_of``)."""
    horizon = draft.config.horizon
    blocks = {
        t.fact.subject: t.fact
        for r in shared
        for t in r.facts
        if t.fact.type is FactType.ROAD_BLOCKED
    }
    options = []
    for report, segment in _road_facts(shared, FactType.ROAD_CLEARED):
        end = report.tick + CUE_WINDOW_TICKS
        fresh = segment in blocks and end < horizon
        if fresh and open_throughout(draft.events, segment, report.tick, end):
            options.append((report.tick, segment, report))
    if not options:
        return False
    _, segment, cue = rng.pick(sorted(options, key=lambda o: (o[0], o[1])))
    tick = cue.tick + rng.between(1, CUE_WINDOW_TICKS - 2)
    lie = _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        blocks[segment],
        PlannedCue(CueKind.REPORT, cue, "police reported it open shortly before"),
    )
    lie.stale_of = blocks[segment]
    draft.story.append(f"L2: a stale 'still blocked' on {segment} at t={tick}.")
    return True


def _substation_failures(draft: Draft) -> dict[str, list[tuple[int, int]]]:
    initial = {s.id: s.repair_minutes for s in draft.substations}
    return failures(initial, draft.events)


def false_status(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """A caller says a failed substation is back online before any crew could have fixed
    it. Cue: substation telemetry shows it down."""
    failed = _substation_failures(draft)
    options = []
    for substation_id in sorted(failed):
        for at, repair in failed[substation_id]:
            low, high = at + 3, min(at + repair - 1, at + 40, draft.config.horizon - 1)
            if low <= high:
                options.append((substation_id, low, high))
    if not options:
        return False
    substation_id, low, high = rng.pick(options)
    tick = rng.between(low, high)
    _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        fact(FactType.SUBSTATION_STATUS, substation_id, "online"),
        PlannedCue(CueKind.TELEMETRY, None, "substation telemetry shows it down"),
    )
    draft.story.append(f"L2: a caller wrongly says {substation_id} is back at t={tick}.")
    return True


def false_eta(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """A caller passes on a far too hopeful repair estimate, soon after the crew's own.
    Cue: the crew's estimate (a field report, more reliable)."""
    failed = _substation_failures(draft)
    options = []
    for report in shared:
        if report.source_class is SourceClass.UNVERIFIED:
            continue
        for timed in report.facts:
            if timed.fact.type is FactType.REPAIR_ETA:
                options.append((report.tick, timed.fact.subject, report))
    if not options:
        return False
    _, substation_id, cue = rng.pick(sorted(options, key=lambda o: (o[0], o[1])))
    tick = cue.tick + rng.between(1, CUE_WINDOW_TICKS - 2)
    least = least_work_left(failed, substation_id, tick)
    if least is None or least < 30:
        return False
    claim = max(5, least // 4)
    _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        fact(FactType.REPAIR_ETA, substation_id, claim),
        PlannedCue(CueKind.REPORT, cue, "the crew's own estimate is far longer"),
    )
    draft.story.append(f"L2: a caller says {substation_id} is back in {claim} min.")
    return True


def false_fuel(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """A caller says a hospital is nearly out when its tank can't be that low yet. Cue:
    hospital telemetry."""
    tick = rng.between(3, 30)
    options = []
    for hospital in draft.hospitals:
        fastest_burn = max(
            [hospital.burn_lpm]
            + [
                e.burn_lpm
                for e in draft.events
                if isinstance(e, BurnRateChange) and e.hospital == hospital.id
            ]
        )
        least = hospital.fuel_l - fastest_burn * tick
        if least >= 60:
            options.append((hospital.id, least))
    if not options:
        return False
    hospital_id, least = rng.pick(options)
    claim = least // 4
    _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        fact(FactType.HOSPITAL_FUEL, hospital_id, claim),
        PlannedCue(CueKind.TELEMETRY, None, "hospital telemetry shows far more fuel"),
    )
    draft.story.append(f"L2: a caller says {hospital_id} is down to {claim} L at t={tick}.")
    return True


def false_stock(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """Someone claims a depot holds far more than it ever had. Cue: depot telemetry."""
    depot = rng.pick(draft.depots)
    tick = rng.between(2, 30)
    step = draft.config.depots.stock_step_l
    claim = -(-max(depot.stock_l * 2, depot.stock_l + 1000) // step) * step
    if rng.chance(draft.config.conflict.field_false_pct):
        who = (Channel.DEPOT_STAFF, SourceClass.FIELD, depot_source(rng, depot))
    else:
        who = (Channel.PUBLIC, SourceClass.UNVERIFIED, public_source(rng))
    _false(
        draft,
        tick,
        *who,
        fact(FactType.DEPOT_STOCK, depot.id, claim),
        PlannedCue(CueKind.TELEMETRY, None, "depot telemetry shows far less"),
    )
    draft.story.append(f"L2: a false claim that {depot.id} holds {claim} L at t={tick}.")
    return True


def false_spike(draft: Draft, rng: Rng, shared: list[PlannedReport]) -> bool:
    """A caller says a hospital's generator is burning fast when it isn't. Cue: hospital
    telemetry shows the real burn rate."""
    tick = rng.between(5, 40)
    options = []
    for hospital in draft.hospitals:
        actual = burn_at(draft.events, hospital.id, hospital.burn_lpm, tick)
        claim = 5 if actual < 4 else 2
        if not close_enough(claim, actual):
            options.append((hospital.id, claim))
    if not options:
        return False
    hospital_id, claim = rng.pick(options)
    _false(
        draft,
        tick,
        Channel.PUBLIC,
        SourceClass.UNVERIFIED,
        public_source(rng),
        fact(FactType.BURN_SPIKE, hospital_id, claim),
        PlannedCue(CueKind.TELEMETRY, None, "hospital telemetry shows the real burn rate"),
    )
    draft.story.append(f"L2: a caller says {hospital_id} burns {claim} L/min at t={tick}.")
    return True


# --- the automated check ----------------------------------------------------------------------


def cue_problems(scenario: Scenario) -> list[str]:
    """Why some false report in ``scenario`` isn't provably false or lacks a working cue
    (empty if all is well)."""
    reports = {e.id: e for e in scenario.events if isinstance(e, Report)}
    physical: Events = [e for e in scenario.events if not isinstance(e, Report)]
    problems: list[str] = []
    for report in reports.values():
        truth = report.truth
        if truth.is_true:
            if truth.cues:
                problems.append(f"{report.id}: a true report with cues")
            continue
        if report.source_class is SourceClass.VERIFIED:
            problems.append(f"{report.id}: a false report from a verified source")
        if not truth.cues:
            problems.append(f"{report.id}: a false report with no cue")
        for claim in report.facts:
            if not false_regardless(scenario, physical, claim, report.tick):
                problems.append(f"{report.id}: {claim.describe()} may hold at t={report.tick}")
        for cue in truth.cues:
            problems += [f"{report.id}: {p}" for p in _cue_problems(report, cue, reports)]
    return problems


def _cue_problems(report: Report, cue: object, reports: dict[str, Report]) -> list[str]:
    from blackout.info.reports import Cue

    assert isinstance(cue, Cue)
    if cue.kind is CueKind.TELEMETRY:
        bad = [c for c in report.facts if c.type not in TELEMETRY_FACTS]
        return [f"telemetry doesn't show {c.type}" for c in bad]
    if cue.kind is CueKind.IMPOSSIBLE:
        return []  # not generated; kept in the schema for hand-made scenarios
    other = reports.get(cue.report or "")
    if other is None:
        return [f"cue report {cue.report!r} isn't in the scenario"]
    problems = []
    if not other.truth.is_true:
        problems.append(f"cue report {other.id} is itself false")
    if RELIABILITY_PCT[other.source_class] <= RELIABILITY_PCT[report.source_class]:
        problems.append(f"cue report {other.id} isn't from a more reliable source")
    if abs(other.tick - report.tick) > CUE_WINDOW_TICKS:
        problems.append(f"cue report {other.id} is more than {CUE_WINDOW_TICKS} ticks away")
    if not any(contradicts(a, b) for a in report.facts for b in other.facts):
        problems.append(f"cue report {other.id} doesn't contradict it")
    return problems


def contradicts(a: Fact, b: Fact) -> bool:
    """Whether two claims can't both be true."""
    roads = {FactType.ROAD_BLOCKED, FactType.ROAD_CLEARED}
    if a.subject != b.subject:
        return False
    if a.type in roads and b.type in roads:
        return a.type is not b.type
    if a.type is not b.type:
        return False
    if isinstance(a.value, int) and isinstance(b.value, int):
        return not (close_enough(a.value, b.value) or close_enough(b.value, a.value))
    return a.value != b.value


def false_regardless(scenario: Scenario, events: Events, claim: Fact, tick: int) -> bool:
    """Whether ``claim`` is false at ``tick`` under every possible sequence of actions."""
    value = claim.value
    match claim.type:
        case FactType.ROAD_BLOCKED:
            return not blocked_at(events, claim.subject, tick)
        case FactType.ROAD_CLEARED:
            return blocked_at(events, claim.subject, tick)
        case FactType.BURN_SPIKE:
            hospital = next(h for h in scenario.hospitals if h.id == claim.subject)
            actual = burn_at(events, hospital.id, hospital.burn_lpm, tick)
            return isinstance(value, int) and not close_enough(value, actual)
        case FactType.HOSPITAL_FUEL:
            hospital = next(h for h in scenario.hospitals if h.id == claim.subject)
            burns = [hospital.burn_lpm] + [
                e.burn_lpm
                for e in events
                if isinstance(e, BurnRateChange) and e.hospital == hospital.id
            ]
            least = hospital.fuel_l - max(burns) * tick
            return isinstance(value, int) and value * 100 < 90 * least
        case FactType.DEPOT_STOCK:
            depot = next(d for d in scenario.depots if d.id == claim.subject)
            return isinstance(value, int) and value * 100 > 110 * depot.stock_l
        case FactType.SUBSTATION_STATUS:
            initial = {s.id: s.repair_minutes for s in scenario.substations}
            left = least_work_left(failures(initial, events), claim.subject, tick)
            return value == "online" and left is not None
        case FactType.REPAIR_ETA:
            initial = {s.id: s.repair_minutes for s in scenario.substations}
            least = least_work_left(failures(initial, events), claim.subject, tick)
            return isinstance(value, int) and least is not None and value * 100 < 90 * least
