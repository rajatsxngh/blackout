"""The M6 keyword parser (blackout.controllers.keyword_parser) and parse F1.

Built and checked against the **train** phrasing bank only: the human-written test bank
(data/phrasing/test/, M8) is never read here.
"""

from pathlib import Path

import pytest

from blackout.controllers.keyword_parser import (
    KeywordParser,
    _digits,
    _durations,
    _one_edit,
    train_vocabulary,
)
from blackout.info.reports import SUBSTATION_STATES, Channel, Fact, FactType
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.banks import PhrasingBank, load_bank
from blackout.scenarios.text.render import TimedFact, render
from blackout.scenarios.text.slots import Gazette
from blackout.scoring.parse_f1 import (
    ParseCounts,
    matches,
    parse_f1,
    scenario_gazette,
    score_parse,
)
from tests.helpers import ROOT

DEV_V0 = ROOT / "scenarios" / "dev-v0"
# Names in dev0_t01_s0: H1 Riverside Clinic, H2 Lakeview Medical Center (at A4), H3 St. Mary's
# Hospital; D1 Eastgate Depot, D2 Southfield Fuel Yard; S1 Foundry Substation (at B6), S2
# Millbrook, S3 Quarry.
SCENARIO = DEV_V0 / "dev0_t01_s0_l1.json"

CHANNELS: dict[FactType, tuple[Channel, ...]] = {
    FactType.ROAD_BLOCKED: (Channel.POLICE_RADIO, Channel.PUBLIC),
    FactType.ROAD_CLEARED: (Channel.POLICE_RADIO, Channel.PUBLIC),
    FactType.SUBSTATION_STATUS: (Channel.CREW_RADIO, Channel.PUBLIC),
    FactType.REPAIR_ETA: (Channel.CREW_RADIO,),
    FactType.HOSPITAL_FUEL: (Channel.HOSPITAL_STAFF,),
    FactType.BURN_SPIKE: (Channel.HOSPITAL_STAFF,),
    FactType.DEPOT_STOCK: (Channel.DEPOT_STAFF,),
}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return load_scenario(SCENARIO)


@pytest.fixture(scope="module")
def gazette(scenario: Scenario) -> Gazette:
    return scenario_gazette(scenario)


@pytest.fixture(scope="module")
def parser(gazette: Gazette) -> KeywordParser:
    return KeywordParser(gazette)


@pytest.fixture(scope="module")
def bank() -> PhrasingBank:
    return load_bank(ROOT / "data" / "phrasing" / "train")


def facts(*items: tuple[str, str, int | str | None]) -> tuple[Fact, ...]:
    return tuple(Fact(type=FactType(t), subject=s, value=v) for t, s, v in items)


# --- pieces ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("two-fifty", "250"),
        ("about one-thirty", "about 130"),
        ("thirty-two l", "32 l"),
        ("twenty-five hundred liters", "2500 liters"),
        ("seventeen hundred", "1700"),
        ("two hundred fifty", "250"),
        ("three thousand", "3000"),
        ("one thousand two hundred", "1200"),
        ("a hundred", "a 100"),
        ("unit 12, no numbers here", "unit 12, no numbers here"),
    ],
)
def test_number_words_become_digits(text: str, expected: str) -> None:
    assert _digits(text) == expected


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("figure 3h20 to fix it", [200]),
        ("2 hours and 20 minutes", [140]),
        ("an hour and 5 minutes", [65]),
        ("3 and a half hours", [210]),
        ("an hour and a half", [90]),
        ("2.5 hours", [150]),
        ("an hour", [60]),
        ("3 hours", [180]),
        ("200 minutes before it's back", [200]),
        ("offline as of about 5 minutes ago", []),
        ("reopened 10 minutes back", []),
    ],
)
def test_durations_skip_relative_times(text: str, minutes: list[int]) -> None:
    found, rest = _durations(text)
    assert found == minutes
    assert len(rest) == len(text)


@pytest.mark.parametrize(
    ("a", "b", "close"),
    [
        ("blockde", "blocked", True),  # swap
        ("closre", "closure", True),  # drop
        ("Ashforrd".lower(), "ashford", True),  # double
        ("shoing", "showing", True),
        ("lokos", "looks", True),
        ("bkocked", "blocked", True),  # near key
        ("blocked", "blocked", False),  # identical isn't a typo
        ("blk", "blocked", False),
        ("abcd", "badc", False),
    ],
)
def test_one_edit(a: str, b: str, close: bool) -> None:
    assert _one_edit(a, b) is close


def test_vocabulary_comes_from_the_train_bank() -> None:
    words = train_vocabulary()
    assert {"blocked", "substation", "generator", "dispatch", "genny"} <= words
    assert all(word == word.lower() for word in words)


# --- whole reports --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Unit 23 to dispatch. Traffic unit reporting 4th Ave between Alder & Birch blockde a"
            " few minutes ago, downed power lines. Detour your vehicles.",
            facts(("road_blocked", "A4-B4", None)),
        ),
        (
            "Lifting the closure on 4th Ave from Alder over to Birch, all lanes are open. Out.",
            facts(("road_cleared", "A4-B4", None)),
        ),
        (
            "Rough ETA on the substation at Birch & 6th is three and a half hours once we start.",
            facts(("repair_eta", "S1", 210)),
        ),
        (
            "Grid control here. Foundry Substation has failed. It's not a trip, it's real damage.",
            facts(("substation_status", "S1", "failed")),
        ),
        (
            "Foundry sub tripped on overload a few minutes ago. It needs a reset.",
            facts(("substation_status", "S1", "tripped")),
        ),
        (
            "Good news, the Foundry substation is energized again as of just now.",
            facts(("substation_status", "S1", "online")),
        ),
        (
            "Repairs are underway at Foundry.",
            facts(("substation_status", "S1", "repairing")),
        ),
        (
            "Nurse supervisor Murphy here. The gen tank at Lakeview Medical Center reads thirty-two"
            " L.",
            facts(("hospital_fuel", "H2", 32)),
        ),
        (
            "Just dipped the tank at Lakeview, one-thirty remaining, don't quote me.",
            facts(("hospital_fuel", "H2", 130)),
        ),
        (
            "We have 60 litres of diesel left at the hosp at Alder & 4th. Please send a rig.",
            facts(("hospital_fuel", "H2", 60)),
        ),
        (
            "Heads up, fuel use at Lakeview Medical Center jumped to 12 L/min.",
            facts(("burn_spike", "H2", 12)),
        ),
        (
            "Send rigs to Eastgate if you need it, theres 1,500 litres here, far as we can tell.",
            facts(("depot_stock", "D1", 1500)),
        ),
        (
            "Line crew 1. Confirming the Foundry substation is down as of abt 3 mins ago. Also,"
            " damage assessment on Foundry: figure 3h20 to fix it.",
            facts(("substation_status", "S1", "failed"), ("repair_eta", "S1", 200)),
        ),
        ("Hi there, the power's out all around here. Somebody should look at it.", ()),
        ("4th Ave between Alder and Birch, nothing to report.", ()),  # no keyword
        ("Eastgate Depot here, the pumps are working.", ()),  # no number
    ],
)
def test_parses_report_text(parser: KeywordParser, text: str, expected: tuple[Fact, ...]) -> None:
    assert parser.parse(text) == expected


def test_every_train_phrasing_parses_without_noise(
    gazette: Gazette, parser: KeywordParser, bank: PhrasingBank
) -> None:
    """Each phrasing of each fact type, filled with random subjects and values, parses back
    exactly when no noise is added."""
    total = ParseCounts()
    for kind in FactType:
        for k, phrasing in enumerate(bank.facts[kind].phrasings):
            for trial in range(3):
                rng = Rng.derive(11, "phrasing", phrasing.id, trial)
                fact = random_fact(kind, gazette, rng, state=phrasing.value)
                channel = phrasing.channels[0] if phrasing.channels else CHANNELS[kind][0]
                text = render(
                    [TimedFact(fact, 15)],
                    tick=20,
                    channel=channel,
                    source="Officer Diaz, Unit 12",
                    gazette=gazette,
                    bank=bank,
                    rng=rng,
                    noise=False,
                ).text
                counts = score_parse(parser.parse(text), [fact])
                assert counts == ParseCounts(1, 0, 0), (k, text, fact)
                total += counts
    assert total.f1 == 1.0


def test_noisy_train_reports_parse_with_high_f1(
    gazette: Gazette, parser: KeywordParser, bank: PhrasingBank
) -> None:
    """Rendered with every noise operator, 1 or 2 facts per report: F1 stays above 0.97."""
    total = ParseCounts()
    for trial in range(400):
        rng = Rng.derive(5, "noisy", trial)
        kinds = [rng.pick(tuple(FactType)) for _ in range(rng.pick((1, 1, 2)))]
        claims = [random_fact(kind, gazette, rng) for kind in kinds]
        text = render(
            [TimedFact(fact, 10) for fact in claims],
            tick=20,
            channel=rng.pick(CHANNELS[kinds[0]]),
            source="Officer Diaz, Unit 12",
            gazette=gazette,
            bank=bank,
            rng=rng,
        ).text
        total += score_parse(parser.parse(text), claims)
    assert total.f1 is not None
    assert total.f1 > 0.97, total


def random_fact(kind: FactType, gazette: Gazette, rng: Rng, state: str | None = None) -> Fact:
    if kind in (FactType.ROAD_BLOCKED, FactType.ROAD_CLEARED):
        return Fact(type=kind, subject=rng.pick(sorted(gazette.city.edges)))
    if kind is FactType.SUBSTATION_STATUS:
        value = state or rng.pick(SUBSTATION_STATES)
        return Fact(type=kind, subject=rng.pick(sorted(gazette.substations)), value=value)
    if kind is FactType.REPAIR_ETA:
        return Fact(
            type=kind, subject=rng.pick(sorted(gazette.substations)), value=rng.between(5, 300)
        )
    if kind is FactType.BURN_SPIKE:
        return Fact(
            type=kind, subject=rng.pick(sorted(gazette.hospitals)), value=rng.between(1, 30)
        )
    if kind is FactType.HOSPITAL_FUEL:
        return Fact(
            type=kind, subject=rng.pick(sorted(gazette.hospitals)), value=rng.between(10, 600)
        )
    return Fact(type=kind, subject=rng.pick(sorted(gazette.depots)), value=rng.between(100, 5000))


# --- parse F1 -------------------------------------------------------------------------------


def test_parse_f1_on_dev_v0_per_level() -> None:
    scenarios = [load_scenario(path) for path in sorted(DEV_V0.glob("dev0_*.json"))]
    by_level = parse_f1(scenarios)
    assert set(by_level) == {0, 1}
    assert by_level[0] == ParseCounts(45, 0, 0)  # the facts themselves
    assert by_level[1].f1 == 1.0, by_level[1]


def test_matching_rules() -> None:
    fuel = Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=100)
    assert matches(Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=109), fuel)
    assert not matches(Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=111), fuel)
    assert not matches(Fact(type=FactType.HOSPITAL_FUEL, subject="H2", value=100), fuel)
    assert not matches(Fact(type=FactType.DEPOT_STOCK, subject="H1", value=100), fuel)
    state = Fact(type=FactType.SUBSTATION_STATUS, subject="S1", value="failed")
    assert matches(state, state)
    assert not matches(state.model_copy(update={"value": "tripped"}), state)


def test_score_parse_counts_each_true_fact_once() -> None:
    fuel = Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=100)
    road = Fact(type=FactType.ROAD_BLOCKED, subject="A1-B1")
    assert score_parse([fuel, fuel], [fuel]) == ParseCounts(1, 1, 0)
    assert score_parse([], [fuel, road]) == ParseCounts(0, 0, 2)
    empty = ParseCounts()
    assert (empty.precision, empty.recall, empty.f1) == (None, None, None)
    assert ParseCounts(1, 1, 0).precision == 0.5


def test_parser_reads_only_the_train_bank() -> None:
    """Guard for the CLAUDE.md rule: nothing in the parser points at the test bank."""
    source = (ROOT / "src" / "blackout" / "controllers" / "keyword_parser.py").read_text("utf-8")
    assert "phrasing/test" not in source
    assert '"test"' not in source
    assert Path("data", "phrasing", "train").as_posix() in source.replace('" / "', "/")
