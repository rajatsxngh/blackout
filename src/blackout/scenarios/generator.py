"""The scenario generator: seed -> world -> events -> reports (docs/PRD.md section 8.6).

For one template, seed and tier, ``generate``:

1. draws a calm world and lets the template break it (``Infeasible`` re-rolls);
2. calibrates the tight hospital's starting fuel by simulation so the world lands in the
   requested tier (blackout.scenarios.calibrate), and checks validity: do-nothing loses at
   least 30 hospital-minutes, the reference none, and the template's own check;
3. plans the L2 additions (false, stale and correcting reports, blackout.scenarios.conflict)
   and the L3 injections (blackout.scenarios.injection);
4. materializes every level: L0 structured facts, L1 free text, L2 = L1 + conflicts, L3 =
   L2 + injections (PRD 8.4), and checks that the reference loses nothing at each one.

A world that fails any check is re-rolled with the next sub-seed, up to ``MAX_ATTEMPTS`` (50).
Every random draw comes from a stream derived from the seed with blake2b
(blackout.scenarios.seeding):

- ``(seed, "world", attempt)`` for the calm world
- ``(seed, "template", attempt)`` for the template's situation and the shared reports
- ``(seed, "calibrate", attempt)`` for where in its tier the fuel lands
- ``(seed, "conflict", attempt)`` and ``(seed, "injection", attempt)`` for L2 and L3
- ``(seed, "report", key)`` for each report's wording, one stream per report. The key is
  ``Rnn`` for shared reports (numbered by tick among themselves) and ``Xnn`` for L2/L3
  additions, so a report reads the same at every level that has it.

Report IDs are numbered by tick at each level, so an addition can't be spotted by its ID.
"""

import multiprocessing
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from blackout.info.reports import Cue, Injection, Report, ReportTruth
from blackout.scenarios.calibrate import (
    Calibrator,
    RunOutcome,
    do_nothing,
    fragile,
    reference,
    tier_of,
)
from blackout.scenarios.config import CityConfig
from blackout.scenarios.conflict import plan_conflicts
from blackout.scenarios.injection import plan_injections, render_injection
from blackout.scenarios.schema import (
    GeneratorInfo,
    Scenario,
    Split,
    Tier,
    Validity,
    content_digest,
    world_digest,
)
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates import TEMPLATES, Template
from blackout.scenarios.templates.common import MIN_DO_NOTHING_LOSS, second_front
from blackout.scenarios.text.banks import PhrasingBank
from blackout.scenarios.text.render import render, report_rng
from blackout.scenarios.world import Draft, Infeasible, PlannedReport, build_world
from blackout.sim.events import BurnRateChange

GENERATOR_VERSION = 3
MAX_ATTEMPTS = 50
"""PRD 8.6 says 20 re-rolls for validity; filling a tier quota needs more (D072)."""


class GenerationError(Exception):
    """No valid world in ``MAX_ATTEMPTS`` re-rolls."""


@dataclass(frozen=True, slots=True)
class Request:
    """One seed of one template, in one tier, to be materialized at ``levels``."""

    template: str
    seed: int
    scenario_id: str  # the level is appended: <scenario_id>_l<level>
    split: Split
    levels: tuple[int, ...] = (0, 1)
    tier: Tier = "medium"


class _Rejected(Exception):  # noqa: N818 (an outcome)
    pass


def generate(request: Request, config: CityConfig, bank: PhrasingBank) -> list[Scenario]:
    """Every level of one seed, all sharing one physical world."""
    template = TEMPLATES[request.template]
    problems: list[str] = []
    for attempt in range(MAX_ATTEMPTS):
        try:
            draft = build_world(config, Rng.derive(request.seed, "world", attempt))
            template.apply(draft, Rng.derive(request.seed, "template", attempt))
            second_front(draft, Rng.derive(request.seed, "front", attempt))
            return _finish(draft, template, request, config, bank, attempt)
        except (Infeasible, _Rejected) as reason:
            problems.append(f"attempt {attempt}: {reason}")
    raise GenerationError(
        f"{request.scenario_id}: no valid {request.tier} world in {MAX_ATTEMPTS} attempts:\n  "
        + "\n  ".join(problems)
    )


def _finish(
    draft: Draft,
    template: Template,
    request: Request,
    config: CityConfig,
    bank: PhrasingBank,
    attempt: int,
) -> list[Scenario]:
    if draft.target is None:
        raise Infeasible(f"{template.id} named no hospital to calibrate")
    target = draft.hospital(draft.target)

    def build(fuel: int) -> Scenario:
        draft.set_fuel(target.id, fuel)
        return _assemble(draft, template, request, config, bank, 0, attempt, None)

    burns = [target.burn_lpm] + [
        e.burn_lpm
        for e in draft.events
        if isinstance(e, BurnRateChange) and e.hospital == target.id
    ]
    calibrator = Calibrator(
        build, burn=max(burns), low=5 * target.burn_lpm, high=target.tank_l, cfg=config.calibration
    )
    cal = config.calibration
    span = {
        "hard": cal.hard_extra_minutes,
        "medium": cal.medium_margin_minutes,
        "easy": cal.easy_margin_minutes,
    }[request.tier]
    minutes = Rng.derive(request.seed, "calibrate", attempt).between(*span)
    fuel = calibrator.place(request.tier, minutes)
    if fuel is None or fuel > target.tank_l:
        raise _Rejected(f"can't calibrate {target.id} for a {request.tier} world")
    probe = build(fuel)
    nothing = do_nothing(probe)
    validity = Validity(
        do_nothing_critical_downtime=nothing.critical_downtime,
        do_nothing_trips=nothing.trips,
    )
    if (problem := template.problem(validity)) is not None:
        raise _Rejected(problem)
    best, naive = calibrator.run("reference", fuel), calibrator.run("rules", fuel)
    if best.critical_downtime > 0:
        raise _Rejected(f"the reference loses {best.critical_downtime} min at L0")
    tier = tier_of(naive, best, cal)
    if tier != request.tier:
        raise _Rejected(f"asked for a {request.tier} world, got {tier} at {fuel} L")
    if tier == "hard" and naive.critical_downtime < cal.hard_min_rules_loss:
        raise _Rejected(f"Rules loses only {naive.critical_downtime} min, too close to a tie")
    if (why := fragile(probe, cal)) is not None:
        raise _Rejected(f"{why} at L0")
    draft.story.append(f"{target.id} starts with {fuel} L ({request.tier}).")

    if max(request.levels) >= 2:
        plan_conflicts(draft, Rng.derive(request.seed, "conflict", attempt))
    if max(request.levels) >= 3:
        plan_injections(draft, Rng.derive(request.seed, "injection", attempt))
    scenarios = []
    for level in request.levels:
        draft_level = _assemble(draft, template, request, config, bank, level, attempt, None)
        best_here = best if level == 0 else reference(draft_level)
        if best_here.critical_downtime > 0:
            raise _Rejected(f"the reference loses {best_here.critical_downtime} min at L{level}")
        if level > 0 and (why := fragile(draft_level, cal)) is not None:
            raise _Rejected(f"{why} at L{level}")
        scenarios.append(
            _assemble(
                draft,
                template,
                request,
                config,
                bank,
                level,
                attempt,
                _validity(nothing, best_here, naive),
                tier=tier,
            )
        )
    return scenarios


def _validity(nothing: RunOutcome, best: RunOutcome, naive: RunOutcome) -> Validity:
    assert nothing.critical_downtime >= MIN_DO_NOTHING_LOSS
    return Validity(
        do_nothing_critical_downtime=nothing.critical_downtime,
        do_nothing_trips=nothing.trips,
        reference_critical_downtime=best.critical_downtime,
        reference_min_slack=best.min_slack,
        rules_l0_critical_downtime=naive.critical_downtime,
    )


def _keys(planned: list[PlannedReport]) -> dict[int, str]:
    """Each planned report's wording key (by identity): Rnn for shared, Xnn for additions."""
    keys: dict[int, str] = {}
    shared = [r for r in planned if r.level == 0]
    extra = [r for r in planned if r.level > 0]
    for prefix, group in (("R", shared), ("X", extra)):
        for number, report in enumerate(group, start=1):
            keys[id(report)] = f"{prefix}{number:02d}"
    return keys


def _assemble(
    draft: Draft,
    template: Template,
    request: Request,
    config: CityConfig,
    bank: PhrasingBank,
    level: int,
    attempt: int,
    validity: Validity | None,
    *,
    tier: Tier | None = None,
) -> Scenario:
    gazette = draft.gazette()
    planned = sorted(draft.reports, key=lambda r: r.tick)  # stable: plan order breaks ties
    keys = _keys(planned)
    here = [r for r in planned if r.level <= level]
    ids = {id(r): f"R{n:02d}" for n, r in enumerate(here, start=1)}
    # World fact IDs: shared reports first, so they're numbered alike at every level.
    fact_ids: dict[tuple[str, str, object], str] = {}
    for report in sorted(here, key=lambda r: r.level > 0):
        if report.is_true:
            for timed in report.facts:
                key = (timed.fact.type.value, timed.fact.subject, timed.fact.value)
                fact_ids.setdefault(key, f"F{len(fact_ids) + 1:02d}")
    zone_names = {z.id: z.name for z in draft.zones}
    reports: list[Report] = []
    for plan in here:
        text = phrasings = noise = None
        rng = report_rng(request.seed, keys[id(plan)])
        if level > 0 and plan.injection is not None:
            rendered = render_injection(plan, gazette=gazette, zones=zone_names, bank=bank, rng=rng)
            text, phrasings, noise = rendered.text, rendered.phrasings, rendered.noise
        elif level > 0:
            rendered = render(
                plan.facts,
                tick=plan.tick,
                channel=plan.channel,
                source=plan.source,
                gazette=gazette,
                bank=bank,
                rng=rng,
            )
            text, phrasings, noise = rendered.text, rendered.phrasings, rendered.noise
        stale = None
        if plan.stale_of is not None:
            fact = plan.stale_of
            stale = fact_ids[(fact.type.value, fact.subject, fact.value)]
        injection = None
        if plan.injection is not None:
            i = plan.injection
            injection = Injection(
                kind=i.kind, target=i.target, start=i.start, end=i.end, authority=i.authority
            )
        truth = ReportTruth(
            is_true=plan.is_true,
            from_level=plan.level,
            cues=[
                Cue(kind=c.kind, report=None if c.ref is None else ids[id(c.ref)], note=c.note)
                for c in plan.cues
            ],
            stale_of=stale,
            corrects=None if plan.corrects is None else ids[id(plan.corrects)],
            injection=injection,
            fact_ids=[
                fact_ids[(t.fact.type.value, t.fact.subject, t.fact.value)] for t in plan.facts
            ]
            if plan.is_true
            else [],
            bank=None if text is None else bank.name,
            phrasings=phrasings or [],
            noise=noise or [],
        )
        reports.append(
            Report(
                tick=plan.tick,
                id=ids[id(plan)],
                channel=plan.channel,
                source_class=plan.source_class,
                source=plan.source,
                facts=[timed.fact for timed in plan.facts],
                text=text,
                truth=truth,
            )
        )
    physical = sorted(draft.events, key=lambda e: e.tick)
    spec = Scenario(
        id=f"{request.scenario_id}_l{level}",
        description=f"{template.id} {template.title}. " + " ".join(draft.story),
        template=template.id,
        seed=request.seed,
        level=level,
        split=request.split,
        tier=tier,
        horizon=config.horizon,
        city=draft.city,
        hospitals=draft.hospitals,
        depots=draft.depots,
        trucks=draft.trucks,
        crews=draft.crews,
        substations=draft.substations,
        zones=draft.zones,
        events=[*physical, *reports],
        generator=GeneratorInfo(
            version=GENERATOR_VERSION,
            config_digest=config.digest(),
            attempt=attempt,
            world_hash="",
        ),
        validity=validity,
    )
    assert spec.generator is not None
    spec = spec.model_copy(
        update={"generator": spec.generator.model_copy(update={"world_hash": world_digest(spec)})}
    )
    spec = spec.model_copy(update={"content_hash": content_digest(spec)})
    return Scenario.model_validate(spec.model_dump(mode="json"))  # re-check everything


def generate_all(
    requests: Sequence[Request],
    config: CityConfig,
    bank: PhrasingBank,
    *,
    workers: int = 1,
) -> list[Scenario]:
    """Every request's scenarios, in request order. With ``workers`` > 1 the requests run in
    a ``spawn`` process pool (each one is independent and deterministic, so the result is the
    same); entry points need the Windows spawn guard."""
    if workers <= 1 or len(requests) <= 1:
        return [scenario for request in requests for scenario in generate(request, config, bank)]
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = [pool.submit(generate, request, config, bank) for request in requests]
        return [scenario for future in futures for scenario in future.result()]
