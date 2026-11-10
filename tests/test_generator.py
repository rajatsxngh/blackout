"""The scenario generator (M5, M8): seeding, worlds, templates, calibration, levels L0-L3, ground
truth, and the committed suites.

Done-when checks (docs/PRD.md section 12, M5 and M8): regenerating gives identical hashes; the
levels of a seed share one world; the reference loses 0 and do-nothing at least 30 on every
frozen scenario; the tier mix is within 10 points of 20/50/30; every L2 false report has a
cue (``cue_problems``). Drift between the committed suites and a fresh generation is checked
by ``blackout gen --suite <name> --check`` in CI (it takes minutes: calibration runs the
reference and Rules); here one request is generated end to end, twice.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from blackout.controllers.do_nothing import DoNothingController
from blackout.env import BlackoutEnv, DecisionRequest
from blackout.info.confirm import check_claims
from blackout.info.reports import Report
from blackout.logs.reader import read_log
from blackout.logs.replay import replay
from blackout.logs.writer import record_episode
from blackout.scenarios.calibrate import RunOutcome, fragile, tier_of
from blackout.scenarios.config import CityConfig, load_city_config
from blackout.scenarios.conflict import contradicts, cue_problems
from blackout.scenarios.generator import (
    MAX_ATTEMPTS,
    GenerationError,
    Request,
    generate,
)
from blackout.scenarios.schema import Scenario, content_digest, load_scenario, world_digest
from blackout.scenarios.seeding import Rng, stable_seed
from blackout.scenarios.suites import (
    MANIFEST,
    SUITES,
    TIER_TARGET_PCT,
    TIER_TOLERANCE_PCT,
    differences,
    manifest,
    write_suite,
)
from blackout.scenarios.templates import TEMPLATES
from blackout.scenarios.templates.common import second_front
from blackout.scenarios.text.banks import PhrasingBank, load_bank
from blackout.scenarios.verify import verify_scenario
from blackout.scenarios.world import Infeasible, build_world
from blackout.sim.engine import Engine
from blackout.sim.events import RoadBlock, SubstationFail, TruckBreakdown
from tests.helpers import ROOT

SCENARIOS = ROOT / "scenarios"
COMMITTED = ("dev",)  # dev-v0 is frozen from M5 (generator v1): it loads and replays


@pytest.fixture(scope="module")
def config() -> CityConfig:
    return load_city_config(ROOT / "configs" / "city" / "default.yaml")


@pytest.fixture(scope="module")
def bank() -> PhrasingBank:
    return load_bank(ROOT / "data" / "phrasing" / "train")


def load_suite(name: str) -> list[Scenario]:
    folder = SCENARIOS / name
    entries = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["scenarios"]
    return [load_scenario(folder / f"{e['id']}.json") for e in entries]


@pytest.fixture(scope="module")
def suites() -> dict[str, list[Scenario]]:
    return {name: load_suite(name) for name in (*COMMITTED, "dev-v0")}


@pytest.fixture(scope="module")
def dev(suites: dict[str, list[Scenario]]) -> list[Scenario]:
    return suites["dev"]


REQUEST = Request("T04", stable_seed("unit", "T04", 0), "unit_t04", "dev", (0, 1, 2, 3), "hard")


@pytest.fixture(scope="module")
def levels(config: CityConfig, bank: PhrasingBank) -> list[Scenario]:
    return generate(REQUEST, config, bank)


def reports_of(scenario: Scenario) -> list[Report]:
    return [event for event in scenario.events if isinstance(event, Report)]


# --- seeding --------------------------------------------------------------------------------


def test_stable_seed_is_pinned_and_48_bit() -> None:
    # Pinned: a change here would silently re-roll every generated scenario.
    assert stable_seed("dev-v0", "T01", 0) == 169343902193755
    assert stable_seed(0) == 145322325918669
    assert stable_seed("a", 1) != stable_seed("a1")
    assert all(0 <= stable_seed("x", i) < 2**48 for i in range(100))


def test_rng_streams_are_pinned() -> None:
    """numpy's PCG64 stream, as the generator uses it. If a numpy upgrade changes this, the
    suites' --check fails too; regenerate and log it in docs/DECISIONS.md."""
    rng = Rng(12345)
    assert [rng.between(0, 99) for _ in range(5)] == [69, 22, 78, 31, 20]
    assert Rng.derive("s", 1).seed == stable_seed("s", 1)


def test_rng_helpers() -> None:
    rng = Rng(7)
    assert all(rng.between(10, 20, 5) in (10, 15, 20) for _ in range(50))
    assert sorted(rng.sample(range(10), 10)) == list(range(10))
    assert len(set(rng.sample("abcdef", 3))) == 3
    assert {rng.chance(0) for _ in range(20)} == {False}
    assert {rng.chance(100) for _ in range(20)} == {True}
    with pytest.raises(ValueError, match="empty"):
        rng.pick([])
    with pytest.raises(ValueError, match="empty range"):
        rng.between(3, 2)
    with pytest.raises(ValueError, match="can't sample"):
        rng.sample([1], 2)


# --- config, worlds and templates ------------------------------------------------------------


def test_config_rejects_bad_ranges_and_layouts(config: CityConfig) -> None:
    data = config.model_dump(mode="json")
    with pytest.raises(ValidationError, match="above high"):
        CityConfig.model_validate({**data, "travel_minutes": [5, 2]})
    with pytest.raises(ValidationError, match="can't be split"):
        CityConfig.model_validate({**data, "grid": {"width": 7, "height": 6}})
    zones = {**data["zones"], "critical": 8, "essential": 2}
    with pytest.raises(ValidationError, match="more critical"):
        CityConfig.model_validate({**data, "zones": zones})
    conflict = {**data["conflict"], "closure_tick": [0, 2]}  # nothing could come before it
    with pytest.raises(ValidationError, match="closure_tick starts at 1"):
        CityConfig.model_validate({**data, "conflict": conflict})
    assert config.digest() == CityConfig.model_validate(data).digest()


def test_calm_world_has_power_everywhere(config: CityConfig) -> None:
    draft = build_world(config, Rng(1))
    assert (len(draft.zones), len(draft.substations), len(draft.hospitals)) == (9, 3, 3)
    assert (len(draft.depots), len(draft.trucks), len(draft.crews)) == (2, 4, 2)
    loads = draft.loads(frozenset())
    for substation in draft.substations:
        assert loads[substation.id] + config.substations.headroom_kw <= substation.capacity_kw
    assert all(draft.zone_of(h.node).priority == "critical" for h in draft.hospitals)
    assert draft.hospitals_at_risk() == []


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_builds_its_situation(config: CityConfig, template: str) -> None:
    """Each template can break some calm world, names a target, and plans true reports."""
    built = 0
    for attempt in range(12):
        draft = build_world(config, Rng.derive("template-test", template, attempt))
        try:
            TEMPLATES[template].apply(draft, Rng.derive("template-test", template, "t", attempt))
        except Infeasible:
            continue
        built += 1
        assert draft.target is not None
        assert draft.reports
        if template != "T02":  # T02's target goes on its generator later, at the failure
            assert draft.target in draft.hospitals_at_risk()
        for reading in draft.fuel_readings:  # readings follow the calibrated fuel
            draft.set_fuel(reading.hospital_id, 300)
            assert isinstance(reading.report.facts[0].fact.value, int)
    assert built >= 3, f"{template} built only {built} of 12 worlds"


@pytest.mark.parametrize("template", ["T01", "T04", "T08", "T09"])
def test_a_second_front_gives_the_fleet_competing_needs(config: CityConfig, template: str) -> None:
    """D098: a lone hospital at risk gets company, with a short tank and a reported outage."""
    cfg = config.fronts
    for attempt in range(12):
        draft = build_world(config, Rng.derive("front-test", template, attempt))
        try:
            TEMPLATES[template].apply(draft, Rng.derive("front-test", template, "t", attempt))
        except Infeasible:
            continue
        before = draft.hospitals_at_risk()
        reports = len(draft.reports)
        second_front(draft, Rng.derive("front-test", template, "f", attempt))
        after = draft.hospitals_at_risk()
        assert len(after) >= cfg.at_risk
        assert set(before) < set(after)
        # The one it cut off has a short tank (others sharing its substation keep theirs).
        new = [draft.hospital(h) for h in sorted(set(after) - set(before))]
        fronts = [(h.fuel_l, [m * h.burn_lpm for m in cfg.fuel_minutes]) for h in new]
        assert any(low <= fuel <= high for fuel, (low, high) in fronts)
        assert len(draft.reports) > reports  # its fuel reading, at least
        again = len(draft.reports)
        second_front(draft, Rng(0))  # enough at risk now: nothing changes
        assert len(draft.reports) == again
        assert draft.hospitals_at_risk() == after
        return
    pytest.fail(f"{template} built no world")


def test_infeasible_worlds_are_rerolled_then_reported(
    config: CityConfig, bank: PhrasingBank, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = TEMPLATES["T01"]

    def never(draft: object, rng: object) -> None:
        raise Infeasible("never")

    monkeypatch.setattr(template, "apply", never)
    with pytest.raises(GenerationError, match=f"in {MAX_ATTEMPTS} attempts"):
        generate(Request("T01", 1, "x", "dev-v0"), config, bank)


def test_worlds_that_fail_the_template_check_are_rerolled(
    config: CityConfig, bank: PhrasingBank, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = TEMPLATES["T02"]
    calls: list[int] = []

    def picky(validity: object) -> str | None:
        calls.append(1)
        return "not yet" if len(calls) < 3 else None

    monkeypatch.setattr(template, "problem", picky)
    (scenario,) = generate(Request("T02", 5, "x", "dev-v0", levels=(0,)), config, bank)
    assert scenario.generator is not None
    assert scenario.generator.attempt >= 2


def test_tiers_follow_rules_and_the_reference_slack(config: CityConfig) -> None:
    cal = config.calibration
    lost, safe = RunOutcome(5, 0, 3), RunOutcome(0, 0, 3)
    assert tier_of(lost, safe, cal) == "hard"
    assert tier_of(safe, RunOutcome(0, 0, cal.easy_slack_minutes - 1), cal) == "medium"
    assert tier_of(safe, RunOutcome(0, 0, cal.easy_slack_minutes), cal) == "easy"
    assert tier_of(safe, RunOutcome(0, 0, None), cal) == "easy"


# --- one request, end to end -----------------------------------------------------------------


def test_regenerating_gives_identical_hashes(
    levels: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> None:
    again = generate(REQUEST, config, bank)
    assert [s.content_hash for s in again] == [s.content_hash for s in levels]
    assert [s.level for s in levels] == [0, 1, 2, 3]


def test_levels_share_one_world_and_build_on_each_other(levels: list[Scenario]) -> None:
    l0, l1, l2, l3 = levels
    assert len({world_digest(s) for s in levels}) == 1
    assert {s.tier for s in levels} == {"hard"}
    shared = [(r.tick, r.facts) for r in reports_of(l0)]
    assert [(r.tick, r.facts) for r in reports_of(l1)] == shared
    added = [r for r in reports_of(l2) if r.truth.from_level == 2]
    assert added, "L2 adds false, stale or correcting reports"
    assert any(not r.truth.is_true for r in added)
    assert [(r.tick, r.facts) for r in reports_of(l2) if r.truth.from_level == 0] == shared
    l3_added = [r for r in reports_of(l3) if r.truth.from_level == 3]
    assert l3_added
    assert all(r.truth.injection is not None and not r.facts for r in l3_added)
    for scenario in levels:  # IDs go by tick, so an addition can't be spotted by its ID
        reports = reports_of(scenario)
        assert [r.id for r in reports] == [f"R{n:02d}" for n in range(1, len(reports) + 1)]
        assert [r.tick for r in reports] == sorted(r.tick for r in reports)
    texts = {(r.tick, r.source, repr(r.facts)): r.text for r in reports_of(l1)}
    for r in reports_of(l3):  # a shared report reads the same at every level that has text
        if r.truth.from_level == 0:
            assert texts[(r.tick, r.source, repr(r.facts))] == r.text


def test_validity_is_recorded_per_level(levels: list[Scenario]) -> None:
    for scenario in levels:
        v = scenario.validity
        assert v is not None
        assert v.reference_critical_downtime == 0
        assert v.do_nothing_critical_downtime >= 30
        assert v.rules_l0_critical_downtime is not None
        assert v.rules_l0_critical_downtime >= 5  # hard: more than a tie


def test_content_hash_catches_edits(levels: list[Scenario]) -> None:
    scenario = levels[0]
    assert scenario.content_hash == content_digest(scenario)
    data = scenario.model_dump(mode="json")
    data["hospitals"][0]["fuel_l"] += 10
    with pytest.raises(ValidationError, match="content_hash"):
        Scenario.model_validate(data)


# --- the committed suites ---------------------------------------------------------------------


def test_suite_shapes_match_the_prd(suites: dict[str, list[Scenario]]) -> None:
    dev = suites["dev"]
    assert len(dev) == 56
    assert sorted({s.template or "" for s in dev}) == [
        "T01",
        "T02",
        "T04",
        "T05",
        "T07",
        "T08",
        "T09",
    ]
    assert sorted({s.level for s in dev}) == [0, 1, 2, 3]
    assert len(suites["dev-v0"]) == 18
    spec = SUITES["test"]
    assert (spec.templates, spec.seeds, spec.levels) == (("T03", "T06", "T10"), 3, (0, 1, 2, 3))


@pytest.mark.parametrize("name", ["dev", "test"])
def test_tier_plans_meet_the_mix(name: str) -> None:
    mix = SUITES[name].tier_mix()
    worlds = sum(mix.values())
    for tier, target in TIER_TARGET_PCT.items():
        assert abs(100 * mix[tier] / worlds - target) <= TIER_TOLERANCE_PCT, (name, mix)


@pytest.mark.parametrize("name", COMMITTED)
def test_frozen_scenarios_are_valid(suites: dict[str, list[Scenario]], name: str) -> None:
    """The reference loses 0 and do-nothing at least 30 on every frozen scenario (PRD M8)."""
    measured: dict[str, int] = {}
    for scenario in suites[name]:
        v = scenario.validity
        assert v is not None
        assert v.reference_critical_downtime == 0, scenario.id
        assert v.do_nothing_critical_downtime >= 30, scenario.id
        assert scenario.tier is not None
        measured[scenario.tier] = measured.get(scenario.tier, 0) + 1
    plan = SUITES[name]
    expected = {t: n * len(plan.levels) for t, n in plan.tier_mix().items() if n}
    assert measured == expected


def test_every_false_report_has_a_working_cue(dev: list[Scenario]) -> None:
    """PRD M8: every L2 false report has at least one cue (automated check)."""
    false_reports = 0
    for scenario in dev:
        assert cue_problems(scenario) == [], scenario.id
        for report in reports_of(scenario):
            if not report.truth.is_true:
                assert scenario.level >= 2
                false_reports += 1
    assert false_reports >= 2 * 14  # at least two per world at L2 and L3


def test_l2_closes_the_roads_into_a_hospital_at_risk(dev: list[Scenario]) -> None:
    """D098: every dev world's L2 carries a false closure: blocks on the roads into one
    hospital in one report, cued by a more reliable report that says they're open."""
    for scenario in (s for s in dev if s.level == 2):
        nodes = {h.node: h.id for h in scenario.hospitals}
        closures = []
        for report in reports_of(scenario):
            claims = [f for f in report.facts if f.type == "road_blocked"]
            if report.truth.is_true or not claims or len(claims) < len(report.facts):
                continue
            around = {n for f in claims for n in f.subject.split("-") if n in nodes}
            if len(around) == 1 and all(next(iter(around)) in f.subject for f in claims):
                closures.append(report)
        assert closures, scenario.id
        for report in closures:
            (cue,) = (c for c in report.truth.cues if c.kind == "report")
            honest = next(r for r in reports_of(scenario) if r.id == cue.report)
            assert {f.subject for f in honest.facts} == {f.subject for f in report.facts}
            assert all(f.type == "road_cleared" for f in honest.facts)


def test_the_reference_must_not_hang_on_one_search_path(
    dev: list[Scenario], config: CityConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D099: ``fragile`` re-runs the reference under other CP-SAT seeds."""
    import blackout.scenarios.calibrate as calibrate

    scenario = next(s for s in dev if s.id == "dev_t09_s1_l0")
    seen: list[int] = []

    def loses_on_seed_two(s: Scenario, random_seed: int = 0) -> RunOutcome:
        seen.append(random_seed)
        return RunOutcome(7 if random_seed == 2 else 0, 0, 3)

    monkeypatch.setattr(calibrate, "reference", loses_on_seed_two)
    assert fragile(scenario, config.calibration) == "the reference loses 7 min with CP-SAT seed 2"
    assert seen == [1, 2]
    monkeypatch.undo()
    assert fragile(scenario, config.calibration.model_copy(update={"robust_seeds": (1,)})) is None


def test_cue_check_catches_broken_cues(dev: list[Scenario]) -> None:
    scenario = next(s for s in dev if s.level == 2)
    data = scenario.model_dump(mode="json", exclude={"content_hash"})
    for event in data["events"]:
        if event["kind"] == "report" and not event["truth"]["is_true"]:
            event["truth"]["cues"] = []
            break
    broken = Scenario.model_validate(data)
    assert any("no cue" in p for p in cue_problems(broken))
    assert not contradicts(
        next(r for r in reports_of(scenario)).facts[0],
        next(r for r in reports_of(scenario)).facts[0],
    )


def test_some_true_unverified_reports_are_critical_and_injections_exist(
    dev: list[Scenario],
) -> None:
    """PRD 8.4: some true unverified reports matter (a block only a caller has reported
    first), and L3 carries injections with a predicate and a window."""
    callers_first = 0
    for scenario in (s for s in dev if s.level == 0):
        blocks = {e.edge for e in scenario.events if isinstance(e, RoadBlock)}
        first: dict[str, Report] = {}
        for report in reports_of(scenario):
            for claim in report.facts:
                if claim.subject in blocks and claim.type == "road_blocked":
                    first.setdefault(claim.subject, report)
        callers_first += sum(1 for r in first.values() if r.source_class == "unverified")
    assert callers_first >= 2
    for scenario in (s for s in dev if s.level == 3):
        injections = [r for r in reports_of(scenario) if r.truth.injection is not None]
        assert injections, scenario.id
        for report in injections:
            window = report.truth.injection
            assert window is not None
            assert window.start == report.tick <= window.end


def checked_reports(scenario: Scenario) -> dict[str, bool]:
    """Run do-nothing, checking each report's claims against the truth when it's released."""
    env = BlackoutEnv(scenario)
    held: dict[str, bool] = {}

    def check(engine: Engine) -> None:
        for item in engine.released:
            if isinstance(item, Report) and item.facts:
                held[item.id] = check_claims(engine, item, engine.tick).holds

    env.add_tick_listener(check)
    request = env.reset()
    check(env.engine)  # tick 0
    while isinstance(request, DecisionRequest):
        request = env.advance()
    return held


@pytest.mark.parametrize("name", COMMITTED)
def test_every_report_is_what_its_truth_says_when_sent(
    suites: dict[str, list[Scenario]], name: str
) -> None:
    """Checked with confirm_report's own truth check, at each report's tick, under do-nothing:
    true reports hold and false ones don't."""
    for scenario in (s for s in suites[name] if s.level in (0, 3)):
        held = checked_reports(scenario)
        for report in reports_of(scenario):
            if report.facts:
                assert held[report.id] == report.truth.is_true, (scenario.id, report.id)


def test_t02_trips_and_t04_breaks_down(dev: list[Scenario]) -> None:
    for scenario in dev:
        assert scenario.validity is not None
        if scenario.template == "T02":
            assert any(isinstance(e, SubstationFail) for e in scenario.events)
            assert scenario.validity.do_nothing_trips >= 1
        if scenario.template == "T04":
            breakdown = next(e for e in scenario.events if isinstance(e, TruckBreakdown))
            truck = next(t for t in scenario.trucks if t.id == breakdown.truck)
            assert truck.load_l == truck.capacity_l


@pytest.mark.parametrize(
    "path",
    [p for name in ("dev", "dev-v0") for p in sorted((SCENARIOS / name).glob("*_l[03].json"))],
    ids=lambda p: p.stem,
)
def test_suite_scenarios_run_and_replay(path: Path, tmp_path: Path) -> None:
    scenario = load_scenario(path)
    recorded = record_episode(BlackoutEnv(scenario), DoNothingController(), tmp_path)
    assert recorded.episode.done.tick > 0
    replayed = replay(read_log(recorded.log_path))
    assert replayed.episode.run_hash == recorded.episode.run_hash


def test_write_suite_replaces_only_its_own_files(
    tmp_path: Path,
    suites: dict[str, list[Scenario]],
    config: CityConfig,
    bank: PhrasingBank,
) -> None:
    spec, suite = SUITES["dev"], suites["dev"]
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    write_suite(tmp_path, suite, manifest(spec, suite, config, bank))
    assert (tmp_path / MANIFEST).is_file()
    assert len(list(tmp_path.glob("dev_*.json"))) == 56
    fewer = suite[:2]
    write_suite(tmp_path, fewer, manifest(spec, fewer, config, bank))
    assert len(list(tmp_path.glob("dev_*.json"))) == 2
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert differences(tmp_path, fewer, manifest(spec, fewer, config, bank)) == []
    assert differences(tmp_path, suite, manifest(spec, suite, config, bank))
    committed = manifest(spec, suite, config, bank)
    assert differences(SCENARIOS / "dev", suite, committed) == []


def test_a_frozen_scenario_verifies_and_a_tampered_one_does_not(
    config: CityConfig, tmp_path: Path
) -> None:
    path = SCENARIOS / "dev" / "dev_t04_s0_l0.json"
    scenario = load_scenario(path)
    assert scenario.content_hash is not None
    assert verify_scenario(path, config.calibration, scenario.content_hash) == []
    assert verify_scenario(path, config.calibration, "0" * 32) == [
        "dev_t04_s0_l0: doesn't match its manifest entry"
    ]
    data = scenario.model_dump(mode="json", exclude={"content_hash"})
    data["validity"]["do_nothing_critical_downtime"] += 1
    data["tier"] = "easy"
    tampered = Scenario.model_validate(data)
    tampered = tampered.model_copy(update={"content_hash": content_digest(tampered)})
    bad = tmp_path / "bad.json"
    bad.write_text(tampered.model_dump_json(), encoding="utf-8")
    assert tampered.content_hash is not None
    problems = " ".join(verify_scenario(bad, config.calibration, tampered.content_hash))
    assert "do-nothing loses" in problems
    assert "measures as hard here, recorded easy" in problems
