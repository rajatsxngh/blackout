"""The M5 scenario generator: seeding, worlds, templates, levels, ground truth, the dev-v0 suite.

Done-when checks (docs/PRD.md section 12, M5): regenerating gives identical hashes; L0 and L1
of a seed share an identical world; every dev-v0 scenario loads, runs under do-nothing and
replays cleanly. The committed suite in scenarios/dev-v0 must match a fresh generation.
"""

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
from blackout.scenarios.config import CityConfig, load_city_config
from blackout.scenarios.generator import (
    MAX_ATTEMPTS,
    GenerationError,
    Request,
    generate,
    generate_all,
)
from blackout.scenarios.schema import (
    Scenario,
    content_digest,
    load_scenario,
    world_digest,
)
from blackout.scenarios.seeding import Rng, stable_seed
from blackout.scenarios.suites import MANIFEST, SUITES, differences, manifest, write_suite
from blackout.scenarios.templates import TEMPLATES
from blackout.scenarios.text.banks import PhrasingBank, load_bank
from blackout.scenarios.world import Infeasible, build_world, fastest, path_edges
from blackout.sim.engine import Engine
from blackout.sim.events import RoadBlock, SubstationFail, TruckBreakdown
from tests.helpers import ROOT

DEV_V0 = ROOT / "scenarios" / "dev-v0"


@pytest.fixture(scope="module")
def config() -> CityConfig:
    return load_city_config(ROOT / "configs" / "city" / "default.yaml")


@pytest.fixture(scope="module")
def bank() -> PhrasingBank:
    return load_bank(ROOT / "data" / "phrasing" / "train")


@pytest.fixture(scope="module")
def suite(config: CityConfig, bank: PhrasingBank) -> list[Scenario]:
    return generate_all(SUITES["dev-v0"].requests(), config, bank)


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
    dev-v0 regeneration test fails too; regenerate and log it in docs/DECISIONS.md."""
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


# --- config ---------------------------------------------------------------------------------


def test_config_rejects_bad_ranges_and_layouts(config: CityConfig) -> None:
    data = config.model_dump(mode="json")
    with pytest.raises(ValidationError, match="above high"):
        CityConfig.model_validate({**data, "travel_minutes": [5, 2]})
    with pytest.raises(ValidationError, match="can't be split"):
        CityConfig.model_validate({**data, "grid": {"width": 7, "height": 6}})
    zones = {**data["zones"], "critical": 8, "essential": 2}
    with pytest.raises(ValidationError, match="more critical"):
        CityConfig.model_validate({**data, "zones": zones})
    assert config.digest() == CityConfig.model_validate(data).digest()


# --- worlds and templates -------------------------------------------------------------------


def test_calm_world_has_power_everywhere(config: CityConfig) -> None:
    draft = build_world(config, Rng(1))
    assert (len(draft.zones), len(draft.substations), len(draft.hospitals)) == (9, 3, 3)
    assert (len(draft.depots), len(draft.trucks), len(draft.crews)) == (2, 4, 2)
    loads = draft.loads(frozenset())
    for substation in draft.substations:
        assert loads[substation.id] + config.substations.headroom_kw <= substation.capacity_kw
    hospital_zones = {draft.zone_of(h.node).id for h in draft.hospitals}
    assert all(draft.zone_of(h.node).priority == "critical" for h in draft.hospitals)
    assert len(hospital_zones) == 3


def test_t01_blocks_the_fast_route_and_is_reported(suite: list[Scenario]) -> None:
    for scenario in (s for s in suite if s.template == "T01"):
        block = next(e for e in scenario.events if isinstance(e, RoadBlock))
        engine = Engine(scenario)
        hospital = next(
            h
            for h in scenario.hospitals
            if engine.zones[engine.hospitals[h.id].zone or ""].alternate is None
        )
        routes = [fastest(engine.city, d.node, hospital.node) for d in scenario.depots]
        best = min((r for r in routes if r is not None), key=lambda r: r[1])
        assert block.edge in path_edges(best[0])
        first_word = min(r.tick for r in reports_of(scenario) if r.facts[0].subject == block.edge)
        assert first_word <= 4, "the block is reported before a loaded truck can leave"


def test_t02_trips_and_t04_breaks_down(suite: list[Scenario]) -> None:
    for scenario in suite:
        assert scenario.validity is not None
        if scenario.template == "T02":
            assert any(isinstance(e, SubstationFail) for e in scenario.events)
            assert scenario.validity.do_nothing_trips >= 1
        if scenario.template == "T04":
            breakdown = next(e for e in scenario.events if isinstance(e, TruckBreakdown))
            truck = next(t for t in scenario.trucks if t.id == breakdown.truck)
            assert truck.load_l == truck.capacity_l
        assert scenario.validity.do_nothing_critical_downtime >= 30


def test_infeasible_worlds_are_rerolled_then_reported(
    config: CityConfig, bank: PhrasingBank, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = TEMPLATES["T01"]

    def never(draft: object, rng: object) -> None:
        raise Infeasible("never")

    monkeypatch.setattr(template, "apply", never)
    with pytest.raises(GenerationError, match=f"no valid world in {MAX_ATTEMPTS} attempts"):
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


# --- levels, hashes and ground truth --------------------------------------------------------


def test_regenerating_gives_identical_hashes(
    suite: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> None:
    again = generate_all(SUITES["dev-v0"].requests(), config, bank)
    assert [s.content_hash for s in again] == [s.content_hash for s in suite]
    assert len(suite) == 18
    assert len({s.id for s in suite}) == 18


def test_levels_of_a_seed_share_one_world(suite: list[Scenario]) -> None:
    by_seed: dict[int | None, list[Scenario]] = {}
    for scenario in suite:
        by_seed.setdefault(scenario.seed, []).append(scenario)
    assert len(by_seed) == 9
    worlds = set()
    for pair in by_seed.values():
        l0, l1 = sorted(pair, key=lambda s: s.level)
        assert (l0.level, l1.level) == (0, 1)
        assert world_digest(l0) == world_digest(l1)
        assert l0.generator is not None
        assert l1.generator is not None
        assert l0.generator.world_hash == l1.generator.world_hash == world_digest(l0)
        assert l0.content_hash != l1.content_hash
        assert [(r.id, r.tick, r.facts) for r in reports_of(l0)] == [
            (r.id, r.tick, r.facts) for r in reports_of(l1)
        ]
        worlds.add(world_digest(l0))
    assert len(worlds) == 9


def test_l0_has_facts_only_and_l1_has_text_and_ground_truth(suite: list[Scenario]) -> None:
    for scenario in suite:
        reports = reports_of(scenario)
        assert len(reports) >= 2
        for report in reports:
            truth = report.truth
            assert truth.is_true
            assert len(truth.fact_ids) == len(report.facts)
            if scenario.level == 0:
                assert report.text is None
                assert truth.bank is None
                assert not truth.phrasings
            else:
                assert report.text
                assert truth.bank == "train"
                assert len(truth.phrasings) == len(report.facts)


def test_repeated_claims_keep_their_fact_id(suite: list[Scenario]) -> None:
    for scenario in suite:
        seen: dict[str, tuple[str, str, object]] = {}
        for report in reports_of(scenario):
            for fact_id, fact in zip(report.truth.fact_ids, report.facts, strict=True):
                key = (fact.type.value, fact.subject, fact.value)
                assert seen.setdefault(fact_id, key) == key


def checked_reports(scenario: Scenario) -> list[str]:
    """Run do-nothing, checking each report's claims against the truth when it's released."""
    env = BlackoutEnv(scenario)
    checked: list[str] = []

    def check(engine: Engine) -> None:
        for item in engine.released:
            if isinstance(item, Report):
                reply = check_claims(engine, item, engine.tick)
                assert reply.holds, (scenario.id, item.id, reply.checks)
                checked.append(item.id)

    env.add_tick_listener(check)
    request = env.reset()
    check(env.engine)  # tick 0
    while isinstance(request, DecisionRequest):
        request = env.advance()
    return checked


def test_every_report_is_true_when_it_is_sent(suite: list[Scenario]) -> None:
    """Checked with confirm_report's own truth check, at each report's tick, under do-nothing."""
    for scenario in (s for s in suite if s.level == 0):
        checked = checked_reports(scenario)
        assert sorted(set(checked)) == sorted(r.id for r in reports_of(scenario))


def test_content_hash_catches_edits(suite: list[Scenario]) -> None:
    scenario = suite[0]
    assert scenario.content_hash == content_digest(scenario)
    data = scenario.model_dump(mode="json")
    data["hospitals"][0]["fuel_l"] += 10
    with pytest.raises(ValidationError, match="content_hash"):
        Scenario.model_validate(data)


# --- the committed dev-v0 suite -------------------------------------------------------------


def test_committed_dev_v0_matches_a_fresh_generation(
    suite: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> None:
    data = manifest(SUITES["dev-v0"], suite, config, bank)
    problems = differences(DEV_V0, suite, data)
    assert not problems, "regenerate with: uv run blackout gen --suite dev-v0\n" + "\n".join(
        problems
    )


@pytest.mark.parametrize("path", sorted(DEV_V0.glob("dev0_*.json")), ids=lambda p: p.stem)
def test_dev_v0_runs_and_replays(path: Path, tmp_path: Path) -> None:
    scenario = load_scenario(path)
    recorded = record_episode(BlackoutEnv(scenario), DoNothingController(), tmp_path)
    assert scenario.validity is not None
    assert recorded.episode.done.tick > 0
    replayed = replay(read_log(recorded.log_path))
    assert replayed.episode.run_hash == recorded.episode.run_hash


def test_write_suite_replaces_only_its_own_files(
    tmp_path: Path, suite: list[Scenario], config: CityConfig, bank: PhrasingBank
) -> None:
    spec = SUITES["dev-v0"]
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    write_suite(tmp_path, suite, manifest(spec, suite, config, bank))
    assert (tmp_path / MANIFEST).is_file()
    assert len(list(tmp_path.glob("dev0_*.json"))) == 18
    fewer = suite[:2]
    write_suite(tmp_path, fewer, manifest(spec, fewer, config, bank))
    assert len(list(tmp_path.glob("dev0_*.json"))) == 2
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert differences(tmp_path, fewer, manifest(spec, fewer, config, bank)) == []
    assert differences(tmp_path, suite, manifest(spec, suite, config, bank))
