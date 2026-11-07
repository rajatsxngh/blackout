"""Noise operators for L1 report text (docs/PRD.md section 8.4).

They run in a fixed order: typos, abbreviations, slang, hedges, bundling, chatter. Bundling
(several facts in one report) is decided when reports are planned, because L0 and L1 must
carry the same facts; here it only means joining the parts with a connector.

Work happens on *raw* parts: rendered phrasings whose first letter is still as written (a
template's lower-case opening word, or a slot like "Elm St ..."). Sentences are capitalized
once, at the end, so a hedge or a filler word never leaves "I think We've ..." behind.

No operator changes a digit or a number word, so every reported number survives intact.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from blackout.info.reports import Channel
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.banks import NoiseLexicon
from blackout.scenarios.text.slots import NUMBER_WORDS

_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_NEIGHBORS: dict[str, str] = {}
for _row in _KEYBOARD_ROWS:
    for _i, _key in enumerate(_row):
        _NEIGHBORS[_key] = _row[max(0, _i - 1) : _i] + _row[_i + 1 : _i + 2]


@dataclass(slots=True)
class NoiseLog:
    """Which operators changed a report's text (stored in its ground truth)."""

    applied: list[str] = field(default_factory=list)

    def note(self, name: str) -> None:
        if name not in self.applied:
            self.applied.append(name)


def _protected(word: str) -> bool:
    return word.lower() in NUMBER_WORDS


def _map_words(text: str, change: Callable[[str], str | None]) -> str:
    """Replace each word for which ``change`` returns a new string."""

    def one(match: re.Match[str]) -> str:
        replaced = change(match.group(0))
        return match.group(0) if replaced is None else replaced

    return _WORD.sub(one, text)


def typo(word: str, rng: Rng) -> str:
    """One keyboard slip in ``word``: a swap, a dropped or doubled letter, or a near key."""
    i = rng.between(1, len(word) - 2)
    kind = rng.pick(("swap", "drop", "double", "near"))
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    if kind == "drop":
        return word[:i] + word[i + 1 :]
    if kind == "double":
        return word[: i + 1] + word[i] + word[i + 1 :]
    near = _NEIGHBORS.get(word[i].lower(), "")
    if not near:
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    key = rng.pick(near)
    return word[:i] + (key.upper() if word[i].isupper() else key) + word[i + 1 :]


def add_typos(parts: list[str], rate_pct: int, rng: Rng, log: NoiseLog) -> list[str]:
    """Typo ``rate_pct`` percent of the words of four letters or more (rounding by chance)."""
    eligible = [
        (p, m.start(), m.group(0))
        for p, part in enumerate(parts)
        for m in _WORD.finditer(part)
        if len(m.group(0)) >= 4 and not _protected(m.group(0))
    ]
    whole, remainder = divmod(len(eligible) * rate_pct, 100)
    count = whole + (1 if rng.chance(remainder) else 0)
    if count == 0:
        return parts
    chosen = sorted(rng.sample(range(len(eligible)), count), reverse=True)
    out = list(parts)
    for index in chosen:  # right to left, so earlier offsets stay valid
        p, start, word = eligible[index]
        out[p] = out[p][:start] + typo(word, rng) + out[p][start + len(word) :]
    log.note("typos")
    return out


def substitute(
    parts: list[str],
    table: dict[str, tuple[str, ...]],
    percent: int,
    rng: Rng,
    log: NoiseLog,
    name: str,
) -> list[str]:
    """Replace words found in ``table`` (exact case), each with probability ``percent``."""

    def change(word: str) -> str | None:
        if word not in table or (_protected(word) and name == "slang") or not rng.chance(percent):
            return None
        log.note(name)
        return rng.pick(table[word])

    return [_map_words(part, change) for part in parts]


def hedge(parts: list[str], lexicon: NoiseLexicon, rng: Rng, log: NoiseLog) -> list[str]:
    """Soften the first part with a prefix or a suffix on its first sentence."""
    if not rng.chance(lexicon.rates.hedge_pct):
        return parts
    first = parts[0]
    if rng.chance(50) and _has_subject(first):
        first = rng.pick(lexicon.hedges.prefix) + first
    else:
        end = _first_sentence_end(first)
        if end is None:
            return parts
        first = first[:end] + rng.pick(lexicon.hedges.suffix) + first[end:]
    log.note("hedge")
    return [first, *parts[1:]]


_SUBJECT_WORDS = frozenset({"the", "there's", "there", "we", "we've", "we're", "our", "it's"})


def _has_subject(part: str) -> bool:
    """Whether a hedge like "I think " can go in front: the part opens with a subject (a
    name, or a word like "we" or "the"), not a clipped radio phrase like "just dipped ..."."""
    first = _WORD.match(part)
    if first is None:
        return False
    return first.group(0)[0].isupper() or first.group(0).lower() in _SUBJECT_WORDS


def _first_sentence_end(text: str) -> int | None:
    """Index of the period ending the first sentence, or None if it doesn't end with one."""
    for match in re.finditer(r"\.(\s|$)", text):
        return match.start()
    return None


def join(parts: list[str], lexicon: NoiseLexicon, rng: Rng, log: NoiseLog) -> str:
    """Bundle the parts of a multi-fact report into one message."""
    text = capitalize(parts[0])
    for part in parts[1:]:
        text += rng.pick(lexicon.chatter.connectors) + part
        log.note("bundle")
    return text


def chatter(
    body: str, channel: Channel, callsign: str, lexicon: NoiseLexicon, rng: Rng, log: NoiseLog
) -> str:
    """Radio chatter: an opener, a filler word, a sign-off."""
    rates, talk = lexicon.rates, lexicon.chatter
    if _lower_ok(body) and rng.chance(rates.filler_pct):
        body = rng.pick(talk.filler) + body[0].lower() + body[1:]
        log.note("filler")
    text = capitalize(body)
    if rng.chance(rates.open_pct):
        text = rng.pick(talk.open[channel]).format(callsign=callsign) + " " + text
        log.note("opener")
    if rng.chance(rates.close_pct):
        text = text + " " + rng.pick(talk.close[channel])
        log.note("sign-off")
    return text


def _lower_ok(body: str) -> bool:
    """A filler may go in front only if the first word isn't a name (stays capitalized)."""
    first = _WORD.match(body)
    return first is not None and first.group(0) in {
        "We've",
        "We're",
        "We",
        "There's",
        "The",
        "Just",
        "Our",
        "Can",
        "Be",
        "Heads",
    }


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def apply_noise(
    parts: list[str], channel: Channel, callsign: str, lexicon: NoiseLexicon, rng: Rng
) -> tuple[str, list[str]]:
    """Run every operator, in order, over a report's raw parts. Returns text and the log."""
    log = NoiseLog()
    rates = lexicon.rates
    parts = add_typos(parts, rng.between(*rates.typo_pct), rng, log)
    abbreviations = {word: (short,) for word, short in lexicon.abbreviations.items()}
    parts = substitute(parts, abbreviations, rates.abbreviation_pct, rng, log, "abbreviations")
    parts = substitute(parts, lexicon.slang, rates.slang_pct, rng, log, "slang")
    parts = hedge(parts, lexicon, rng, log)
    body = join(parts, lexicon, rng, log)
    return chatter(body, channel, callsign, lexicon, rng, log), log.applied
