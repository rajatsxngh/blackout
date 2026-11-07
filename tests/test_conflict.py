"""M8 L2/L3 planning on drafts: every kind of false report the conflict planner can make is
provably false and has a working cue, and injections get a predicate and a window. Built
straight from template drafts (no calibration), so it runs in seconds and reaches the kinds a
small suite might not draw."""

import pytest

from blackout.info.reports import CueKind, FactType, InjectionKind
from blackout.scenarios import conflict
from blackout.scenarios.config import CityConfig, load_city_config
from blackout.scenarios.generator import Request, _assemble  # pyright: ignore[reportPrivateUsage]
from blackout.scenarios.injection import plan_injections
from blackout.scenarios.schema import Scenario
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates import TEMPLATES
from blackout.scenarios.text.banks import PhrasingBank, load_bank
from blackout.scenarios.world import Draft, Infeasible, build_world
from tests.helpers import ROOT

MAKERS = [
    conflict.false_clear,
    conflict.false_block,
    conflict.stale_block,
    conflict.false_status,
    conflict.false_eta,
    conflict.false_fuel,
    conflict.false_stock,
    conflict.false_spike,
]


@pytest.fixture(scope="module")
def config() -> CityConfig:
    return load_city_config(ROOT / "configs" / "city" / "default.yaml")


@pytest.fixture(scope="module")
def bank() -> PhrasingBank:
    return load_bank(ROOT / "data" / "phrasing" / "train")


def drafts(config: CityConfig, template: str, count: int) -> list[Draft]:
    made = []
    for attempt in range(40):
        draft = build_world(config, Rng.derive("conflict-test", template, attempt))
        try:
            TEMPLATES[template].apply(draft, Rng.derive("conflict-test", template, "t", attempt))
        except Infeasible:
            continue
        assert draft.target is not None
        draft.set_fuel(draft.target, 120)
        made.append(draft)
        if len(made) == count:
            break
    return made


def assemble(draft: Draft, template: str, config: CityConfig, bank: PhrasingBank) -> Scenario:
    request = Request(template, 1, "unit", "dev", (3,))
    return _assemble(draft, TEMPLATES[template], request, config, bank, 3, 0, None)


@pytest.mark.parametrize("template", ["T01", "T02", "T05", "T07", "T08", "T09"])
def test_every_maker_gives_provably_false_cued_reports(
    config: CityConfig, bank: PhrasingBank, template: str
) -> None:
    made: dict[str, int] = {}
    for index, draft in enumerate(drafts(config, template, 3)):
        rng = Rng.derive("makers", template, index)
        shared = [r for r in draft.reports if r.level == 0]
        for maker in MAKERS:
            if maker(draft, rng, shared):
                made[maker.__name__] = made.get(maker.__name__, 0) + 1
        plan_injections(draft, rng)
        scenario = assemble(draft, template, config, bank)
        assert conflict.cue_problems(scenario) == []
        for report in scenario.events:
            if report.kind == "report" and not report.truth.is_true:
                assert report.truth.cues
                assert report.truth.from_level == 2
    assert made, f"{template}: no maker found anything to lie about"


def test_all_kinds_of_lie_and_injection_occur(config: CityConfig, bank: PhrasingBank) -> None:
    kinds: set[FactType] = set()
    cues: set[CueKind] = set()
    injections: set[InjectionKind] = set()
    for template in ("T01", "T02", "T05", "T07", "T08"):
        for index, draft in enumerate(drafts(config, template, 3)):
            rng = Rng.derive("kinds", template, index)
            shared = [r for r in draft.reports if r.level == 0]
            for maker in MAKERS:
                maker(draft, rng, shared)
            plan_injections(draft, rng)
            scenario = assemble(draft, template, config, bank)
            for report in scenario.events:
                if report.kind != "report":
                    continue
                if not report.truth.is_true:
                    kinds.update(f.type for f in report.facts)
                    cues.update(c.kind for c in report.truth.cues)
                if report.truth.injection is not None:
                    injections.add(report.truth.injection.kind)
                    assert report.text
    assert kinds >= {
        FactType.ROAD_BLOCKED,
        FactType.ROAD_CLEARED,
        FactType.SUBSTATION_STATUS,
        FactType.REPAIR_ETA,
        FactType.HOSPITAL_FUEL,
        FactType.DEPOT_STOCK,
        FactType.BURN_SPIKE,
    }
    assert cues == {CueKind.REPORT, CueKind.TELEMETRY}
    assert injections == set(InjectionKind)


def test_cue_check_rejects_bad_labels(config: CityConfig, bank: PhrasingBank) -> None:
    (draft,) = drafts(config, "T01", 1)
    rng = Rng(3)
    shared = [r for r in draft.reports if r.level == 0]
    assert conflict.false_block(draft, rng, shared)
    scenario = assemble(draft, "T01", config, bank)
    data = scenario.model_dump(mode="json", exclude={"content_hash"})
    lie = next(e for e in data["events"] if e["kind"] == "report" and not e["truth"]["is_true"])
    lie["source_class"] = "verified"
    lie["truth"]["cues"] = [
        {"kind": "report", "report": "R99", "note": ""},
        {"kind": "telemetry", "report": None, "note": ""},
        {"kind": "impossible", "report": None, "note": ""},
    ]
    true_one = next(e for e in data["events"] if e["kind"] == "report" and e["truth"]["is_true"])
    true_one["truth"]["cues"] = [{"kind": "telemetry", "report": None, "note": ""}]
    problems = conflict.cue_problems(Scenario.model_validate(data))
    text = " ".join(problems)
    assert "verified source" in text
    assert "isn't in the scenario" in text
    assert "telemetry doesn't show" in text
    assert "a true report with cues" in text
