"""The keyword parser: L1 report text to structured facts (docs/PRD.md sections 8.4 and 8.7).

Used by C1 Rules (and C2 Solver in M7). It is deliberately simple and was built by reading the
**train** phrasing bank only (never the human-written test bank):

1. Normalize: lower case, fix one-letter typos against a vocabulary (the train bank's words,
   the gazetteer and number words), expand the abbreviations the noise operators use, and turn
   number words into digits ("two-fifty" -> 250, "an hour and a half" stays for step 4).
2. Find subjects: every way the gazetteer can name a road segment, substation, hospital or
   depot (``Gazette.*_phrases``), longest match first.
3. Split the text into one group per mention: sentences and bundling connectors ("Also, ...")
   separate facts; a sentence without a mention belongs to the mention before it.
4. Classify each group by keywords (blocked vs reopened, failed vs tripped, ...) and read its
   value (liters, minutes, liters per minute).

Parse F1 against each report's ground-truth facts is measured in blackout.scoring.parse_f1.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path

from blackout.info.observation import Observation
from blackout.info.reports import Fact, FactType
from blackout.scenarios.text.banks import PhrasingBank, load_bank
from blackout.scenarios.text.slots import (
    NUMBER_WORDS,
    Gazette,
    Place,
    duration_phrases,
    liters_phrases,
    number_words,
    rate_phrases,
    when_phrases,
)

TRAIN_BANK_DIR = Path(__file__).resolve().parents[3] / "data" / "phrasing" / "train"

_ONES = {number_words(n): n for n in range(20)}
_TENS = {number_words(n): n for n in range(20, 100, 10)}
_SCALE = {"hundred": 100, "thousand": 1000}
_NUMBER = "|".join(sorted([*_ONES, *_TENS, *_SCALE], key=len, reverse=True))
_NUMBER_RUN = re.compile(rf"\b(?:{_NUMBER})(?:[ -](?:{_NUMBER}))*\b")
_WORD = re.compile(r"[a-z][a-z']*")

# The noise operators' abbreviations and slang (train bank _noise.yaml), undone. "St" and "Ave"
# go the other way: both sides of every comparison are normalized to the short form.
_EXPAND = (
    (r"(\d) ?l/min\b", r"\1 liters per minute"),
    (r"&", " and "),
    (r"\bbtwn\b", "between"),
    (r"\babt\b", "about"),
    (r"\bmins?\b", "minutes"),
    (r"\bhosp\b", "hospital"),
    (r"\b(?:genny|gen)\b", "generator"),
    (r"\bpls\b", "please"),
    (r"\bw/", "with "),
    (r"\brigs\b", "trucks"),
    (r"\brig\b", "truck"),
    (r"\bstreet\b", "st"),
    (r"\bavenue\b", "ave"),
    (r"\blitres?\b", "liters"),
    (r"\bliter\b", "liters"),
    (r"(\d) ?l\b", r"\1 liters"),
    (r"\bliters (?:a|an|each) minute\b", "liters per minute"),
)

# Words the slot fillers (blackout.scenarios.text.slots) put into reports, besides names.
_SLOT_WORDS = frozenset(
    _WORD.findall(
        " ".join(
            phrase.lower()
            for n in (1, 5, 30, 60, 90, 125, 250, 1500)
            for phrase in (
                *duration_phrases(n),
                *liters_phrases(n),
                *rate_phrases(n),
                *when_phrases(n),
            )
        )
    )
)

_CONNECTOR = re.compile(r"\b(?:also|and another thing|second thing|1 more thing)\b")
_SENTENCE_END = re.compile(r"(?<!\bst)[.!?:;](?=\s|$)")

_CLEARED = re.compile(
    r"\b(?:open|opened|reopened|clear|cleared|passable|lifting|gone|again|moving)\b"
)
_BLOCKED = re.compile(
    r"\b(?:blocked|closed|closure|shut|impassable|jammed|no through traffic|can't get past"
    r"|turn around|detour)\b"
)
_TRIPPED = re.compile(r"(?<!not a )\b(?:trip|tripped)\b|\boverload\b")
_REPAIRING = re.compile(r"\b(?:underway|working on it|started work|on site and working)\b")
_ONLINE = re.compile(r"\b(?:back online|energized|lights are back)\b")
_FAILED = re.compile(
    r"\b(?:down|lost|offline|failed|out of service|went dark|bang|went out|smoke|died|damage)\b"
)

_DURATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d+)h(\d\d)\b"), "hm"),
    (re.compile(r"\b(\d+|an|1) hours? and (\d+) minutes\b"), "hm"),
    (re.compile(r"\b(\d+) and a half hours\b"), "h+"),
    (re.compile(r"\ban hour and a half\b"), "90"),
    (re.compile(r"\b(\d+)\.5 hours\b"), "h+"),
    (re.compile(r"\b(\d+|an) hours?\b"), "h"),
    (re.compile(r"\b(\d+) minutes\b"), "m"),
)
_AGO = re.compile(r"\s*(?:ago|back)\b")
_RATE = re.compile(r"\b(\d+) liters per minute\b")
_LITERS = re.compile(r"\b(\d+) liters\b")
_BARE = re.compile(r"(?<!unit )(?<!crew )(?<!#)\b(\d+)\b(?![.,]?\d)")


@cache
def train_vocabulary(bank_dir: Path = TRAIN_BANK_DIR) -> frozenset[str]:
    """Every word of the train phrasing bank (phrasings, slot vocab and noise lexicon)."""
    return bank_words(load_bank(bank_dir))


def bank_words(bank: PhrasingBank) -> frozenset[str]:
    texts: list[str] = []
    for fact_bank in bank.facts.values():
        texts += [p.text for p in fact_bank.phrasings]
        texts += [w for words in fact_bank.vocab.values() for w in words]
    noise = bank.noise
    texts += [*noise.abbreviations, *noise.abbreviations.values()]
    texts += [w for words in noise.slang.values() for w in words] + list(noise.slang)
    texts += [*noise.hedges.prefix, *noise.hedges.suffix, *noise.chatter.filler]
    texts += [*noise.chatter.connectors]
    texts += [line for lines in noise.chatter.open.values() for line in lines]
    texts += [line for lines in noise.chatter.close.values() for line in lines]
    return frozenset(_WORD.findall(" ".join(texts).lower()))


def gazette_of(observation: Observation) -> Gazette:
    """The public gazetteer, as a contestant can build it from its own observation."""
    return Gazette(
        city=observation.city,
        hospitals={h.id: Place(h.name, h.node) for h in observation.hospitals},
        depots={d.id: Place(d.name, d.node) for d in observation.depots},
        substations={s.id: Place(s.name, s.node) for s in observation.substations},
    )


@dataclass(frozen=True, slots=True)
class _Mention:
    kind: str  # road, substation, hospital, depot
    subject: str
    start: int
    end: int


class KeywordParser:
    """Turns report text into facts, resolving names through one scenario's gazetteer."""

    def __init__(self, gazette: Gazette, vocabulary: Iterable[str] | None = None) -> None:
        aliases: dict[str, tuple[str, str]] = {}
        named: list[tuple[str, str, list[str]]] = []
        named += [("road", e, gazette.road_phrases(e)) for e in sorted(gazette.city.edges)]
        named += [("substation", s, gazette.substation_phrases(s)) for s in gazette.substations]
        named += [("hospital", h, gazette.hospital_phrases(h)) for h in gazette.hospitals]
        named += [("depot", d, gazette.depot_phrases(d)) for d in gazette.depots]
        for kind, subject, phrases in named:
            for phrase in phrases:
                aliases.setdefault(_alias(phrase), (kind, subject))
        # The other node names of a facility ("the hospital at Birch and 6th" either way round).
        for kind, places in (
            ("substation", gazette.substations),
            ("hospital", gazette.hospitals),
            ("depot", gazette.depots),
        ):
            word = "yard" if kind == "depot" else kind
            for subject, place in places.items():
                for node in gazette.node_phrases(place.node):
                    for noun in {word, "depot"} if kind == "depot" else {word}:
                        aliases.setdefault(_alias(f"the {noun} at {node}"), (kind, subject))
        self._aliases = aliases
        ordered = sorted(aliases, key=lambda a: (-len(a), a))
        self._names = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in ordered) + r")\b")
        words = set(vocabulary if vocabulary is not None else train_vocabulary())
        words |= {w for alias in aliases for w in _WORD.findall(alias)} | _SLOT_WORDS
        self._vocabulary = frozenset(words)
        self._fixed: dict[str, str] = {}

    @classmethod
    def for_observation(cls, observation: Observation) -> "KeywordParser":
        return cls(gazette_of(observation))

    # --- normalization ---------------------------------------------------------------------

    def normalize(self, text: str) -> str:
        lowered = _WORD.sub(lambda m: self._fix(m.group(0)), text.lower())
        lowered = re.sub(r"(?<=\d),(?=\d{3}\b)", "", lowered)
        return re.sub(r"\s+", " ", _expand(_digits(lowered))).strip()

    def _fix(self, word: str) -> str:
        """``word``, or the one vocabulary word a single typo away from it."""
        if len(word) < 3 or word in self._vocabulary or word in NUMBER_WORDS:
            return word
        if word not in self._fixed:  # typos hit words of 4+ letters, so a fix has 4+ too
            near = sorted(w for w in self._vocabulary if len(w) >= 4 and _one_edit(word, w))
            if len(near) > 1:  # "minuts": minute or minutes? Keep the same first and last letter
                near = [w for w in near if w[0] == word[0] and w[-1] == word[-1]]
            self._fixed[word] = near[0] if len(near) == 1 else word
        return self._fixed[word]

    # --- parsing ---------------------------------------------------------------------------

    def parse(self, text: str) -> tuple[Fact, ...]:
        """The facts ``text`` states, in order, without duplicates."""
        normal = self.normalize(text)
        facts: list[Fact] = []
        for mention, body in self._groups(normal):
            fact = _classify(mention, body)
            if fact is not None and fact not in facts:
                facts.append(fact)
        return tuple(facts)

    def _groups(self, text: str) -> list[tuple[_Mention, str]]:
        mentions = [
            _Mention(*self._aliases[m.group(0)], m.start(), m.end())
            for m in self._names.finditer(text)
        ]
        if not mentions:
            return []
        cuts = {0, len(text)}
        cuts |= {m.end() for m in _SENTENCE_END.finditer(text)}
        cuts |= {m.start() for m in _CONNECTOR.finditer(text)}
        bounds = sorted(cuts)
        # Each mention owns the sentences from its own up to the next mention's; text before
        # the first mention (an opener, a value) belongs to the first.
        starts = [0]
        for previous, mention in pairwise(mentions):
            sentence = max(b for b in bounds if b <= mention.start)
            starts.append(max(sentence, previous.end))
        ends = [*starts[1:], len(text)]
        groups = []
        for mention, start, end in zip(mentions, starts, ends, strict=True):
            body = text[start : mention.start] + " @ " + text[mention.end : end]
            groups.append((mention, body))
        return groups


def _expand(text: str) -> str:
    for pattern, replacement in _EXPAND:
        text = re.sub(pattern, replacement, text)
    return text


def _alias(phrase: str) -> str:
    """A gazetteer phrase, normalized the way report text is (without typo fixing)."""
    return re.sub(r"\s+", " ", _expand(_digits(phrase.lower()))).strip()


def _classify(mention: _Mention, body: str) -> Fact | None:
    subject = mention.subject
    if mention.kind == "road":
        if _CLEARED.search(body):
            return Fact(type=FactType.ROAD_CLEARED, subject=subject)
        if _BLOCKED.search(body):
            return Fact(type=FactType.ROAD_BLOCKED, subject=subject)
        return None
    durations, rest = _durations(body)
    if mention.kind == "substation":
        if durations:
            return Fact(type=FactType.REPAIR_ETA, subject=subject, value=durations[0])
        for pattern, state in (
            (_TRIPPED, "tripped"),
            (_REPAIRING, "repairing"),
            (_ONLINE, "online"),
            (_FAILED, "failed"),
        ):
            if pattern.search(body):
                return Fact(type=FactType.SUBSTATION_STATUS, subject=subject, value=state)
        return None
    rate = _RATE.search(rest)
    if mention.kind == "hospital" and rate is not None:
        return Fact(type=FactType.BURN_SPIKE, subject=subject, value=int(rate.group(1)))
    liters = _LITERS.search(rest) or _BARE.search(rest)
    if liters is None:
        return None
    kind = FactType.HOSPITAL_FUEL if mention.kind == "hospital" else FactType.DEPOT_STOCK
    return Fact(type=kind, subject=subject, value=int(liters.group(1)))


def _durations(text: str) -> tuple[list[int], str]:
    """Durations in ``text`` (not relative times like "5 minutes ago"), and the text without
    any of them, so their numbers aren't read as liters."""
    found: list[tuple[int, int]] = []
    for pattern, form in _DURATIONS:
        for match in pattern.finditer(text):
            if form == "90":
                minutes = 90
            else:
                first = 1 if match.group(1) in ("an", "1") else int(match.group(1))
                minutes = {
                    "hm": first * 60 + (int(match.group(2)) if form == "hm" else 0),
                    "h+": first * 60 + 30,
                    "h": first * 60,
                    "m": first,
                }[form]
            relative = _AGO.match(text, match.end()) is not None
            found.append((match.start(), -1 if relative else minutes))
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return [minutes for _, minutes in sorted(found) if minutes >= 0], text


def _digits(text: str) -> str:
    """Number words as digits: "two-fifty" -> 250, "seventeen hundred" -> 1700."""
    return _NUMBER_RUN.sub(lambda m: str(_number(m.group(0))), text)


def _number(words: str) -> int:
    parts = re.split(r"[ -]", words)
    if len(parts) == 2 and 1 <= _ONES.get(parts[0], 0) <= 9 and "-" in words:
        tail = _ONES.get(parts[1], _TENS.get(parts[1], 0))
        if tail >= 10:  # radio style: "two-fifty", "one-thirty"
            return _ONES[parts[0]] * 100 + tail
    total = current = 0
    for part in parts:
        if part in _SCALE:
            if part == "thousand":
                total += (current or 1) * 1000
                current = 0
            else:
                current = (current or 1) * 100
        else:
            current += _ONES.get(part, _TENS.get(part, 0))
    return total + current


def _one_edit(a: str, b: str) -> bool:
    """Whether one swap, drop, insertion or substitution turns ``a`` into ``b``."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and (a[diff[1]] == b[diff[0]])
        )
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1 :]
