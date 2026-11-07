"""Report text: the train phrasing bank, slot filling through the gazetteer, noise operators."""

import re
from typing import Any

import pytest
from pydantic import ValidationError

from blackout.info.reports import Channel, FactType, close_enough
from blackout.scenarios.config import load_city_config
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.banks import TRAIN_MIN_PHRASINGS, FactBank, PhrasingBank, load_bank
from blackout.scenarios.text.noise import NoiseLog, add_typos, apply_noise, capitalize, typo
from blackout.scenarios.text.render import TimedFact, callsign, render
from blackout.scenarios.text.slots import (
    Gazette,
    duration_phrases,
    liters_phrases,
    number_words,
    rate_phrases,
    spoken_number,
    when_phrases,
)
from blackout.scenarios.world import build_world, fact
from tests.helpers import ROOT


@pytest.fixture(scope="module")
def bank() -> PhrasingBank:
    return load_bank(ROOT / "data" / "phrasing" / "train")


@pytest.fixture(scope="module")
def gazette() -> Gazette:
    config = load_city_config(ROOT / "configs" / "city" / "default.yaml")
    return build_world(config, Rng(3)).gazette()


# --- the bank -------------------------------------------------------------------------------


def test_train_bank_covers_every_fact_type(bank: PhrasingBank) -> None:
    assert bank.name == "train"
    for kind in FactType:
        assert len(bank.facts[kind].phrasings) >= TRAIN_MIN_PHRASINGS
    failed = bank.facts[FactType.SUBSTATION_STATUS].candidates(Channel.CREW_RADIO, "failed")
    assert len(failed) >= 5
    assert all(p.value == "failed" for p in failed)


def _bank(**changes: Any) -> dict[str, Any]:
    phrasings = [{"id": f"hf{i:02d}", "text": "{hospital} has {liters}."} for i in range(10)]
    return {"fact_type": "hospital_fuel", "phrasings": phrasings} | changes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b["phrasings"].append({"id": "hf99", "text": "{liters} left"}), "no {hospital}"),
        (lambda b: b["phrasings"].append({"id": "hf98", "text": "{hospital}"}), "no {liters}"),
        (
            lambda b: b["phrasings"].append({"id": "hf97", "text": "{hospital} {liters} {x}"}),
            "unknown slots",
        ),
        (
            lambda b: b["phrasings"].append({"id": "hf00", "text": "{hospital} {liters}"}),
            "duplicate",
        ),
        (
            lambda b: b["phrasings"].append(
                {"id": "hf96", "text": "{hospital} {liters}", "value": "failed"}
            ),
            "state value",
        ),
        (lambda b: b.update(phrasings=b["phrasings"][:3]), "at least 8"),
    ],
)
def test_bank_checks_its_phrasings(change: Any, message: str) -> None:
    data = _bank()
    change(data)
    with pytest.raises(ValidationError, match=re.escape(message)):
        FactBank.model_validate(data)


def test_substation_phrasings_need_a_known_state() -> None:
    phrasings = [
        {"id": f"ss{i:02d}", "text": "{substation} is out", "value": "failed"} for i in range(10)
    ]
    FactBank.model_validate({"fact_type": "substation_status", "phrasings": phrasings})
    phrasings[0] = {"id": "ss00", "text": "{substation} melted", "value": "melted"}
    with pytest.raises(ValidationError, match="unknown state"):
        FactBank.model_validate({"fact_type": "substation_status", "phrasings": phrasings})


def test_incomplete_bank_is_rejected(bank: PhrasingBank) -> None:
    facts = {k: v for k, v in bank.facts.items() if k is not FactType.BURN_SPIKE}
    with pytest.raises(ValidationError, match="burn_spike"):
        PhrasingBank(name="partial", facts=facts, noise=bank.noise)


# --- slots ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "zero"),
        (7, "seven"),
        (40, "forty"),
        (56, "fifty-six"),
        (250, "two hundred fifty"),
        (1700, "one thousand seven hundred"),
        (3005, "three thousand five"),
    ],
)
def test_number_words(n: int, words: str) -> None:
    assert number_words(n) == words


def test_number_words_range() -> None:
    with pytest.raises(ValueError, match=r"0..9999"):
        number_words(10000)


def test_spoken_numbers() -> None:
    assert spoken_number(250) == "two-fifty"
    assert spoken_number(2500) == "twenty-five hundred liters"
    assert spoken_number(300) is None
    assert spoken_number(3000) is None


_NUMBER = re.compile(r"\d[\d,.]*")


@pytest.mark.parametrize("value", [32, 56, 120, 246, 333, 1500, 2700, 4000])
def test_liter_phrases_stay_within_tolerance(value: int) -> None:
    for phrase in liters_phrases(value):
        digits = _NUMBER.search(phrase)
        if digits is not None:
            assert close_enough(int(digits.group(0).replace(",", "")), value), phrase


def test_duration_and_rate_phrases() -> None:
    assert "an hour and a half" in duration_phrases(90)
    assert "two hours" in duration_phrases(120)
    assert "2.5 hours" in duration_phrases(150)
    assert "three hours and twenty minutes" in duration_phrases(200)
    assert "four liters per minute" in rate_phrases(4)
    assert "just now" in when_phrases(0)
    assert "a few minutes ago" in when_phrases(4)
    assert "about 10 minutes ago" in when_phrases(9)


def test_every_name_names_one_thing(gazette: Gazette) -> None:
    """No phrase for one subject is also a phrase for another (case-insensitive)."""
    owner: dict[str, str] = {}
    subjects = [
        *((e, gazette.road_phrases(e)) for e in gazette.city.edges),
        *((h, gazette.hospital_phrases(h)) for h in gazette.hospitals),
        *((d, gazette.depot_phrases(d)) for d in gazette.depots),
        *((s, gazette.substation_phrases(s)) for s in gazette.substations),
    ]
    for subject, phrases in subjects:
        assert phrases
        for phrase in phrases:
            assert owner.setdefault(phrase.lower(), subject) == subject, phrase


def test_road_phrases_use_street_names_and_landmarks(gazette: Gazette) -> None:
    landmark, node = next(iter(gazette.city.landmarks.items()))
    touching = [e for e in gazette.city.edges.values() if node in (e.a, e.b)]
    assert any(landmark in p for e in touching for p in gazette.road_phrases(e.id))
    assert "Elm St between 3rd and 4th" in gazette.road_phrases("E3-E4")
    assert "4th Ave between Cedar and Elm" in gazette.road_phrases("C4-E4")
    assert "Fourth Avenue between Elm and Cedar" in gazette.road_phrases("C4-E4")


# --- noise and rendering --------------------------------------------------------------------


def test_typo_changes_one_letter_and_keeps_the_first() -> None:
    rng = Rng(1)
    for _ in range(50):
        changed = typo("blocked", rng)
        assert changed[0] == "b"
        assert abs(len(changed) - len("blocked")) <= 1


def test_typos_never_touch_numbers() -> None:
    rng = Rng(2)
    text = ["we have two hundred fifty liters and 250 L, about two-fifty, at Hillcrest Hospital"]
    for _ in range(30):
        (noisy,) = add_typos(text, 100, rng, NoiseLog())
        for keep in ("two", "hundred", "fifty", "250", "two-fifty"):
            assert keep in noisy


def test_apply_noise_is_deterministic_and_keeps_numbers(bank: PhrasingBank) -> None:
    parts = ["the generator tank at Hillcrest Hospital reads 246 liters.", "we're holding 1500 L."]
    first = apply_noise(parts, Channel.HOSPITAL_STAFF, "Charge nurse Diaz", bank.noise, Rng(9))
    again = apply_noise(parts, Channel.HOSPITAL_STAFF, "Charge nurse Diaz", bank.noise, Rng(9))
    assert first == again
    text, applied = first
    assert "246" in text
    assert "1500" in text
    assert text[0].isupper()
    assert "bundle" in applied


def test_render_is_deterministic_and_names_the_subject(
    bank: PhrasingBank, gazette: Gazette
) -> None:
    hospital = next(iter(gazette.hospitals))
    facts = [TimedFact(fact(FactType.HOSPITAL_FUEL, hospital, 240), 3)]
    kwargs: dict[str, Any] = {
        "tick": 3,
        "channel": Channel.HOSPITAL_STAFF,
        "source": "Charge nurse Diaz, Somewhere",
        "gazette": gazette,
        "bank": bank,
    }
    clean = render(facts, rng=Rng(4), noise=False, **kwargs)
    assert any(name.lower() in clean.text.lower() for name in gazette.hospital_phrases(hospital))
    assert clean.noise == []
    assert clean.phrasings[0].startswith("hospital_fuel/hf")
    assert render(facts, rng=Rng(4), **kwargs) == render(facts, rng=Rng(4), **kwargs)


def test_callsigns() -> None:
    assert callsign(Channel.POLICE_RADIO, "Officer Diaz, Unit 12") == "Unit 12"
    assert callsign(Channel.HOSPITAL_STAFF, "Charge nurse Okafor, Bayside Clinic") == (
        "Charge nurse Okafor"
    )
    assert callsign(Channel.CREW_RADIO, "Grid control center") == "Grid control"
    assert capitalize("we") == "We"
