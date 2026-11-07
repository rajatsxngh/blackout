"""The scenario generator: seed -> world -> events -> reports (docs/PRD.md section 8.6).

For one template and seed, ``generate`` draws a calm world, lets the template break it, and
checks the result under do-nothing; a world that fails is re-rolled with the next sub-seed,
up to ``MAX_ATTEMPTS`` times. The same physical world and the same planned reports are then
materialized once per level: structured facts at L0, free text at L1 (PRD 8.4). Every random
draw comes from a stream derived from the seed with blake2b (blackout.scenarios.seeding):

- ``(seed, "world", attempt)`` for the calm world
- ``(seed, "template", attempt)`` for the template's situation and the planned reports
- ``(seed, "report", report_id)`` for each report's wording (one stream per report)
"""

from collections.abc import Sequence
from dataclasses import dataclass

from blackout.controllers.do_nothing import DoNothingController
from blackout.env import BlackoutEnv, run_episode
from blackout.info.reports import Report, ReportTruth
from blackout.scenarios.config import CityConfig
from blackout.scenarios.schema import (
    GeneratorInfo,
    Scenario,
    Split,
    Validity,
    content_digest,
    world_digest,
)
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates import TEMPLATES, Template
from blackout.scenarios.text.banks import PhrasingBank
from blackout.scenarios.text.render import render, report_rng
from blackout.scenarios.world import Draft, Infeasible, build_world

GENERATOR_VERSION = 1
MAX_ATTEMPTS = 20


class GenerationError(Exception):
    """No valid world in ``MAX_ATTEMPTS`` re-rolls."""


@dataclass(frozen=True, slots=True)
class Request:
    """One seed of one template, to be materialized at ``levels``."""

    template: str
    seed: int
    scenario_id: str  # the level is appended: <scenario_id>_l<level>
    split: Split
    levels: tuple[int, ...] = (0, 1)


def generate(request: Request, config: CityConfig, bank: PhrasingBank) -> list[Scenario]:
    """Every level of one seed, all sharing one physical world."""
    template = TEMPLATES[request.template]
    problems: list[str] = []
    for attempt in range(MAX_ATTEMPTS):
        try:
            draft = build_world(config, Rng.derive(request.seed, "world", attempt))
            template.apply(draft, Rng.derive(request.seed, "template", attempt))
        except Infeasible as reason:
            problems.append(f"attempt {attempt}: {reason}")
            continue
        probe = _assemble(draft, template, request, config, bank, 0, attempt, None)
        validity = measure(probe)
        problem = template.problem(validity)
        if problem is not None:
            problems.append(f"attempt {attempt}: {problem}")
            continue
        return [
            _assemble(draft, template, request, config, bank, level, attempt, validity)
            for level in request.levels
        ]
    raise GenerationError(
        f"{request.scenario_id}: no valid world in {MAX_ATTEMPTS} attempts:\n  "
        + "\n  ".join(problems)
    )


def measure(scenario: Scenario) -> Validity:
    """Run do-nothing on the world (reports don't change physics, so any level will do)."""
    env = BlackoutEnv(scenario)
    run_episode(env, DoNothingController())
    metrics = env.engine.metrics
    return Validity(
        do_nothing_critical_downtime=metrics.critical_downtime,
        do_nothing_trips=metrics.trips,
    )


def _assemble(
    draft: Draft,
    template: Template,
    request: Request,
    config: CityConfig,
    bank: PhrasingBank,
    level: int,
    attempt: int,
    validity: Validity | None,
) -> Scenario:
    gazette = draft.gazette()
    planned = sorted(draft.reports, key=lambda r: r.tick)  # stable: plan order breaks ties
    fact_ids: dict[tuple[str, str, object], str] = {}
    reports: list[Report] = []
    for number, plan in enumerate(planned, start=1):
        report_id = f"R{number:02d}"
        ids = []
        for timed in plan.facts:
            key = (timed.fact.type.value, timed.fact.subject, timed.fact.value)
            ids.append(fact_ids.setdefault(key, f"F{len(fact_ids) + 1:02d}"))
        text = phrasings = noise = None
        if level > 0:
            rendered = render(
                plan.facts,
                tick=plan.tick,
                channel=plan.channel,
                source=plan.source,
                gazette=gazette,
                bank=bank,
                rng=report_rng(request.seed, report_id),
            )
            text, phrasings, noise = rendered.text, rendered.phrasings, rendered.noise
        reports.append(
            Report(
                tick=plan.tick,
                id=report_id,
                channel=plan.channel,
                source_class=plan.source_class,
                source=plan.source,
                facts=[timed.fact for timed in plan.facts],
                text=text,
                truth=ReportTruth(
                    fact_ids=ids,
                    bank=None if text is None else bank.name,
                    phrasings=phrasings or [],
                    noise=noise or [],
                ),
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
    requests: Sequence[Request], config: CityConfig, bank: PhrasingBank
) -> list[Scenario]:
    return [scenario for request in requests for scenario in generate(request, config, bank)]
